#!/usr/bin/env node
const assert = require('node:assert/strict');
const { response, runCollector } = require('./collector_harness');

const config = {
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 300,
  min_comments: 0, min_favorites: 0, match_mode: 'all', start_page: 1,
  page_size: 500, max_pages: 5, pages_before: 0, checkpoint_pages: 100,
  cache_chunk_pages: 1, request_concurrency: 4,
  delay_min_ms: 600, delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function mixedRequestsStayBounded(cap, throttle) {
  let active = 0;
  let peak = 0;
  let throttled = false;
  let postThrottlePeak = 0;
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  try {
    const { sinkPayloads } = await runCollector({
      config: { ...config, request_concurrency: cap },
      onSleep: (ms) => { now += ms; },
      remoteFetch: async (url) => {
        const parsed = new URL(url, 'https://treehole.pku.edu.cn');
        const page = Number(parsed.searchParams.get('page'));
        active += 1;
        peak = Math.max(peak, active);
        if (throttled) postThrottlePeak = Math.max(postThrottlePeak, active);
        if (page !== 1) { await tick(); await tick(); }
        active -= 1;
        if (parsed.pathname.endsWith('/one')) {
          if (throttle && !throttled) {
            throttled = true;
            return response({ code: 42900 }, { status: 429, retryAfter: '15' });
          }
          return response({ code: 20000, data: { hole: { reply: 2, likenum: 2, text: 'detail' } } });
        }
        return response({ code: 20000, data: { list: page === 1
          ? Array.from({ length: 8 }, (_,i) => ({ pid: String(i), timestamp: 150, reply: 2, text: 'x' }))
          : [] } });
      },
    });
    assert.equal(peak, cap, 'list and detail requests must share the cap');
    assert.equal(sinkPayloads[0].matched_pids.length, 8);
    if (throttle) {
      assert.ok(postThrottlePeak <= Math.max(1, cap - 1), 'new requests must honor the reduced cap');
      assert.equal(sinkPayloads[0].telemetry.concurrency_reductions, cap > 1 ? 1 : 0);
    }
  } finally {
    Date.now = originalNow;
  }
}

async function skipDetailsOutsideWindow() {
  const details = [];
  const { sinkPayloads } = await runCollector({
    config: { ...config, max_pages: 1 },
    remoteFetch: async (url) => {
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      if (parsed.pathname.endsWith('/one')) {
        details.push(parsed.searchParams.get('pid'));
        return response({ code: 20000, data: { hole: { likenum: 2 } } });
      }
      return response({ code: 20000, data: { list: [300, 150, 100, 99].map((timestamp) => (
        { pid: String(timestamp), timestamp, reply: 2, text: 'x' }
      )) } });
    },
  });
  assert.deepEqual(details.sort(), ['100', '150']);
  assert.equal(sinkPayloads[0].rows.length, 4, 'all list rows must remain cached');
  assert.deepEqual(sinkPayloads[0].favorite_unavailable, []);
}

async function retryUsesSameBody() {
  const bodies = [];
  let releasePrefetch;
  const prefetch = new Promise((resolve) => { releasePrefetch = resolve; });
  await runCollector({
    config: { ...config, min_favorites: null },
    remoteFetch: async (url) => {
      const page = Number(new URL(url, 'https://treehole.pku.edu.cn').searchParams.get('page'));
      if (page !== 1) await prefetch;
      return response({ code: 20000, data: { list: page === 1
        ? [{ pid: '1', timestamp: 150, reply: 2, likenum: 2, text: 'x' }]
        : [] } });
    },
    sinkFetch: async (_url, options) => {
      bodies.push(options.body);
      if (bodies.length === 1) {
        releasePrefetch();
        await tick(); // Prefetch changes telemetry after the first submission.
        throw new Error('response lost after commit');
      }
      return response({ ok: true });
    },
  });
  assert.ok(bodies.length >= 3);
  assert.equal(bodies[0], bodies[1], 'retry must preserve the committed payload exactly');
}

(async () => {
  for (const cap of [1, 2, 4, 8]) await mixedRequestsStayBounded(cap, false);
  await mixedRequestsStayBounded(4, true);
  await mixedRequestsStayBounded(8, true);
  await skipDetailsOutsideWindow();
  await retryUsesSameBody();
  process.stdout.write('collect boundary regression tests: ok\n');
})().catch((error) => { console.error(error); process.exitCode = 1; });
