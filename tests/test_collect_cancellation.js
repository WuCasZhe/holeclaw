const assert = require('node:assert/strict');
const { response, runCollector } = require('./collector_harness');

const config = {
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 300,
  min_comments: 0, min_favorites: null, start_page: 1, page_size: 500, max_pages: 1,
  checkpoint_pages: 100, cache_chunk_pages: 1, request_concurrency: 1,
  delay_min_ms: 600, delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
  control_url: 'http://127.0.0.1:12345/control?token=test',
};

async function cancelledWhile(mode) {
  let cancel;
  let requests = 0;
  let aborted = false;
  const started = performance.now();
  await assert.rejects(runCollector({
    config,
    onSleep: milliseconds => {
      if (mode === 'cooldown' && milliseconds >= 15000) cancel(response({cancelled: true}));
    },
    remoteFetch: async (url, options) => {
      if (url === config.control_url) return new Promise(resolve => { cancel = resolve; });
      requests++;
      if (mode === 'cooldown') return response({code: 429}, {status: 429, retryAfter: '60'});
      return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); }, {once: true});
        queueMicrotask(() => cancel(response({cancelled: true})));
      });
    },
  }), /abort|cancel/i);
  assert.equal(requests, 1, 'cancellation must prevent retries and new requests');
  if (mode === 'fetch') assert.equal(aborted, true, 'in-flight fetch must be aborted');
  assert.ok(performance.now() - started < 1000, 'cancellation must not wait for remote timeout or backoff');
}

(async () => {
  await cancelledWhile('fetch');
  await cancelledWhile('cooldown');
  console.log('collector cancellation tests: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
