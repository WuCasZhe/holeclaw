const assert = require('node:assert/strict');
const {runCollector, response} = require('./collector_harness');
const {simulate} = require('./collector_simulation');
const tick = () => new Promise(resolve => setImmediate(resolve));
const config = {
  archive: true, archive_run: 'pipeline',
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 2000,
  min_comments: null, min_favorites: null, page_size: 500, max_pages: 4,
  cache_chunk_pages: 1, checkpoint_pages: 100, request_concurrency: 2,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};
const post = (pid, timestamp = 900) => ({pid, timestamp, reply: 1, likenum: 1, text: 'synthetic'});

async function pipelineCommitsOnlyCompletedPrefix(failSecond) {
  let releaseFirst;
  const firstGate = new Promise(resolve => {releaseFirst = resolve;});
  const events = [];
  const commits = [];
  let active = 0, peak = 0;
  const task = runCollector({config,
    sinkFetch: async (_url, options) => {
      const p = JSON.parse(options.body);
      if (p.archive_prepare) return response({resumes: Object.fromEntries(p.posts.map(
        row => [row.pid, {next_page: 1, complete: false}]))});
      if (p.start_page) commits.push(p.start_page);
      if (p.archive_comments) events.push(`saved:${p.post.pid}`);
      return response({ok: true});
    },
    remoteFetch: async url => {
      active++;
      peak = Math.max(peak, active);
      try {
        const parsed = new URL(url, 'https://treehole.pku.edu.cn');
        if (parsed.pathname.endsWith('/list_comments')) {
          const page = Number(parsed.searchParams.get('page'));
          return response({code: 20000, data: {list: [post(String(page), 900 - page)]}});
        }
        const pid = parsed.searchParams.get('pid');
        assert.equal(parsed.searchParams.get('limit'), '100');
        events.push(`start:${pid}`);
        if (pid === '1') await firstGate;
        if (pid === '2') {
          assert.deepEqual(commits, [], 'a later page cannot advance past blocked page one');
          releaseFirst();
          if (failSecond) return response({}, {status: 401});
        }
        return response({code: 20000, data: {list: []}});
      } finally {active--;}
    },
  });
  // A regression to per-page scheduling must fail instead of silently leaving an unresolved promise.
  task.catch(() => {});
  for (let i = 0; i < 100 && !events.includes('start:2'); i++) await tick();
  if (!events.includes('start:2')) releaseFirst();
  if (failSecond) await assert.rejects(task, /Authentication expired/);
  else await task;
  assert.ok(events.indexOf('start:2') < events.indexOf('saved:1'), 'next-page comments use idle slots');
  assert.ok(peak <= 2);
  assert.deepEqual(commits, failSecond ? [1] : [1, 2, 3, 4]);
}

async function listProgressProtection(cycle) {
  let requests = 0;
  const run = runCollector({config: {...config, archive: false, min_comments: 0, max_pages: 4},
    remoteFetch: async url => {
      requests++;
      const page = Number(new URL(url, 'https://treehole.pku.edu.cn').searchParams.get('page'));
      const ids = cycle ? (page % 2 ? ['a', 'b'] : ['c', 'd']) : [String(page), String(page + 1)];
      return response({code: 20000, data: {list: ids.map(pid => post(pid))}});
    },
  });
  if (cycle) await assert.rejects(run, /List pagination made no progress/);
  else assert.equal((await run).sinkPayloads.length, 4, 'partial overlap remains valid');
  assert.ok(requests <= 4);
}

(async () => {
  await pipelineCommitsOnlyCompletedPrefix(false);
  await pipelineCommitsOnlyCompletedPrefix(true);
  await listProgressProtection(true);
  await listProgressProtection(false);
  for (const sinkMs of [2, 100, 2000]) await simulate('telemetry', {archive: false, pages: 24, sinkMs});
  const short = await simulate('short', {archive: false, boundary: true, pages: 24});
  assert.equal(short.requests.list, 1);
  assert.equal(short.telemetry.overfetch_pages, 0);
  const repeated = await simulate('repeat', {archive: false, repeat: true, pages: 3});
  assert.equal(repeated.committed_pages, 1);
  const parallel = await simulate('parallel');
  const serial = await simulate('serial', {serialPages: true});
  assert.equal(parallel.unique_comments, serial.unique_comments);
  assert.ok(parallel.model_wall_ms < serial.model_wall_ms);
  console.log('collector optimization regressions: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
