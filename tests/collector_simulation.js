// Deterministic collector simulation for optimization regression tests.
// Uses synthetic responses and a virtual clock; no browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {unpackSinkMessage} = require('./sink_protocol_fixture');
const original = fs.readFileSync(path.join(__dirname, '../scripts/collect.js'), 'utf8');

async function simulate(name, options = {}) {
  const {pages = 8, matches = 1, replies = 169, concurrency = 8,
    archive = true, limit10 = false, serialPages = false, listOrder = false,
    sinkMs = 2, missingFavorites = false, repeat = false, boundary = false} = options;
  let source = original;
  if (limit10) {
    const needle = '&limit=${commentPageSize}&sort=0';
    assert.equal(source.split(needle).length, 2, 'update experiment for changed collector');
    source = source.replace(needle, '&limit=${10}&sort=0');
  }
  if (serialPages) source = source.replace('(archive ? requestConcurrency : 1)', '1');
  if (listOrder) {
    const needle = 'const workOrder = [...pageMatches].sort((a, b) => Math.min(b.reply, 1000) - Math.min(a.reply, 1000));';
    assert.equal(source.split(needle).length, 2);
    source = source.replace(needle, 'const workOrder = pageMatches;');
  }
  let now = 0, serial = 0, active = 0, peak = 0, networkMs = 0;
  const timers = new Map(), counts = {list: 0, detail: 0, comment: 0, sink: 0};
  const committed = [], saved = new Set(), telemetry = {};
  const setTimer = (fn, delay = 0) => {
    const id = ++serial;
    timers.set(id, {fn, at: now + delay});
    return id;
  };
  const delay = ms => new Promise(resolve => setTimer(resolve, ms));
  const response = body => ({status: 200, ok: true, headers: {get: () => null},
    text: async () => JSON.stringify(body)});
  const config = {
    archive, archive_run: 'offline-regression',
    report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 100000,
    min_comments: 0, min_favorites: missingFavorites ? 0 : null,
    page_size: 500, max_pages: pages, request_concurrency: concurrency,
    cache_chunk_pages: 1, checkpoint_pages: 100,
    sink_url: 'http://127.0.0.1:12345/ingest?token=offline',
  };
  const fetch = async (url, request = {}) => {
    if (url.startsWith(config.sink_url)) {
      counts.sink++;
      const payload = unpackSinkMessage(JSON.parse(request.body));
      await delay(sinkMs);
      if (payload.archive_prepare) return response({ok: true, resumes:
        Object.fromEntries(payload.posts.map(post => [post.pid, {next_page: 1, complete: false}]))});
      if (payload.archive_comments) {
        for (const comment of payload.comments) saved.add(`${payload.post.pid}/${comment.cid}`);
      }
      if (payload.start_page) {
        committed.push(payload.start_page);
      }
      if (payload.start_page || payload.telemetry_final) {
        for (const [key, value] of Object.entries(payload.telemetry)) {
          telemetry[key] = key === 'max_in_flight' ? Math.max(telemetry[key] || 0, value)
            : (telemetry[key] || 0) + value;
        }
      }
      return response({ok: true});
    }
    active++;
    peak = Math.max(peak, active);
    await delay(200); // Every remote response takes a modelled 200 ms.
    networkMs += 200;
    active--;
    const parsed = new URL(url, 'https://offline.invalid');
    const page = Number(parsed.searchParams.get('page'));
    if (parsed.pathname.endsWith('/list_comments')) {
      counts.list++;
      return response({code: 20000, data: {list: Array.from({length: 500}, (_, index) => ({
        pid: `${repeat ? 1 : page}-${index}`,
        timestamp: boundary ? 99 : 90000 - (repeat ? 1 : page) * 500 - index,
        reply: index < matches ? (Array.isArray(replies) ? replies[index] : replies) : 0,
        likenum: missingFavorites ? null : 1, type: 'text', text: 'synthetic post',
      }))}});
    }
    if (parsed.pathname.endsWith('/comment/list')) {
      counts.comment++;
      const pid = parsed.searchParams.get('pid');
      const index = Number(pid.split('-')[1]);
      const total = Array.isArray(replies) ? replies[index] : replies;
      const limit = Number(parsed.searchParams.get('limit'));
      const offset = (page - 1) * limit;
      return response({code: 20000, data: {list: Array.from(
        {length: Math.max(0, Math.min(limit, total - offset))}, (_, i) => ({
          cid: String(offset + i + 1), text: 'synthetic comment', timestamp: 90001,
        }))}});
    }
    assert.ok(parsed.pathname.endsWith('/hole/one'));
    counts.detail++;
    return response({code: 20000, data: {hole: {likenum: 1}}});
  };
  class ModelDate extends Date { static now() { return now; } }
  const context = vm.createContext({fetch, setTimeout: setTimer,
    clearTimeout: id => timers.delete(id), AbortController, AbortSignal, URL,
    performance: {now: () => now}, Date: ModelDate,
    Math: Object.assign(Object.create(Math), {random: () => 0.5})});
  const collector = vm.runInContext(source, context);
  let evaluates = 0, done = false, error;
  collector({evaluate: async (fn, arg) => ++evaluates === 1 ? config : fn(arg),
    reload: async () => {},
    waitForRequest: async () => ({allHeaders: async () => ({authorization: 'offline-placeholder'})}),
  }).then(() => {done = true;}, caught => {done = true; error = caught;});
  // setImmediate drains promise continuations before advancing the model clock.
  while (!done) {
    await new Promise(resolve => setImmediate(resolve));
    if (done) break;
    assert.ok(timers.size, 'simulation deadlocked');
    const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    timers.delete(id);
    now = timer.at;
    timer.fn();
  }
  if (repeat) assert.match(String(error), /List pagination made no progress/);
  else if (error) throw error;
  assert.ok(peak <= concurrency, 'shared concurrency limit exceeded');
  assert.deepEqual(committed, Array.from({length: committed.length}, (_, i) => i + 1));
  if (!error) {
    assert.equal(telemetry.list_requests, counts.list, 'list request counts must be conserved');
    assert.equal(telemetry.comment_requests, counts.comment, 'comment request counts must be conserved');
    assert.equal(telemetry.request_ms, networkMs, 'request time must survive concurrent callbacks');
    assert.equal(telemetry.wall_ms, now - sinkMs, 'wall time includes every data callback, before the final telemetry acknowledgment');
  }
  if (archive && !repeat && !boundary) {
    const expected = pages * (Array.isArray(replies)
      ? replies.reduce((sum, value) => sum + Math.min(value, 1000), 0)
      : matches * Math.min(replies, 1000));
    assert.equal(saved.size, expected, 'experiment must preserve comment output');
  }
  return {name, model_wall_ms: now, requests: counts, committed_pages: committed.length,
    unique_comments: saved.size, observed_peak: peak,
    average_active_network_requests: Number((networkMs / now).toFixed(3)),
    telemetry, error: error ? String(error) : null};
}

module.exports = {simulate};
