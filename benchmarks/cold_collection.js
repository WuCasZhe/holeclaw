// Offline, deterministic scheduling model using the actual collector in a VM.
// No authentication, browser, real network, cache or production source edits.
// Run: node benchmarks/cold_collection.js [output.json]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createHash} = require('node:crypto');
const original = fs.readFileSync(path.join(__dirname, '../scripts/collect.js'), 'utf8');

async function simulate(name, options = {}) {
  const {pages = 8, matches = 1, replies = 169, concurrency = 8,
    archive = true, limit10 = false, serialPages = false, longestFirst = false,
    sinkMs = 2, missingFavorites = false, repeat = false, boundary = false} = options;
  let source = original;
  if (limit10) {
    const needle = '&limit=${commentPageSize}&sort=0';
    assert.equal(source.split(needle).length, 2, 'update experiment for changed collector');
    source = source.replace(needle, '&limit=${10}&sort=0');
  }
  if (serialPages) source = source.replace('(archive ? requestConcurrency : 1)', '1');
  if (longestFirst) {
    const needle = 'await Promise.all(pageMatches.map((post) => schedulePost';
    assert.equal(source.split(needle).length, 2);
    source = source.replace(needle,
      'await Promise.all([...pageMatches].sort((a, b) => b.reply - a.reply).map((post) => schedulePost');
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
    archive, archive_run: 'offline-benchmark',
    report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 100000,
    min_comments: 0, min_favorites: missingFavorites ? 0 : null,
    page_size: 500, max_pages: pages, request_concurrency: concurrency,
    cache_chunk_pages: 1, checkpoint_pages: 100,
    sink_url: 'http://127.0.0.1:12345/ingest?token=offline',
  };
  const fetch = async (url, request = {}) => {
    if (url.startsWith(config.sink_url)) {
      counts.sink++;
      const payload = JSON.parse(request.body);
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
    clearTimeout: id => timers.delete(id), AbortController, URL,
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

async function main() {
  const cases = [
    ['list_c1', {archive: false, pages: 24, concurrency: 1}],
    ['list_c8', {archive: false, pages: 24}],
    ['sparse_text100', {}], ['sparse_text10_experiment', {limit10: true}],
    ['sparse_text100_serial_pages', {serialPages: true}],
    ['dense_text100', {pages: 2, matches: 8}],
    ['dense_text10_experiment', {pages: 2, matches: 8, limit10: true}],
    ['long_tail', {pages: 1, matches: 16, replies: [...Array(15).fill(1), 999]}],
    ['long_tail_sorted_experiment', {pages: 1, matches: 16,
      replies: [...Array(15).fill(1), 999], longestFirst: true}],
    ['missing_favorites', {archive: false, pages: 1, missingFavorites: true}],
    ['slow_sink', {archive: false, pages: 24, sinkMs: 100}],
    ['very_slow_sink', {archive: false, pages: 24, sinkMs: 2000}],
    ['repeated_list_bounded_probe', {archive: false, pages: 3, repeat: true}],
    ['short_window_overfetch', {archive: false, pages: 24, boundary: true}],
  ];
  const results = [];
  for (const [name, options] of cases) results.push(await simulate(name, options));
  const output = {method: 'OFFLINE MODEL: fixed 1300 ms pacing, 200 ms remote RTT, 2 ms sink unless specified; no retries, browser startup or CPU/serialization costs. Every list has 500 rows; only positive-reply posts match. Experimental edits exist only in VM source.',
    node: process.version, collector_sha256: createHash('sha256').update(original).digest('hex'), results};
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(output, null, 2) + '\n');
  console.table(results.map(({name, model_wall_ms, requests, unique_comments, observed_peak, telemetry}) => ({
    name, model_seconds: model_wall_ms / 1000, remote_requests: requests.list + requests.detail + requests.comment,
    comment_requests: requests.comment, unique_comments, observed_peak,
    telemetry_wall_seconds: telemetry.wall_ms / 1000,
  })));
}
module.exports = {simulate};
if (require.main === module) main().catch(error => {console.error(error); process.exitCode = 1;});
