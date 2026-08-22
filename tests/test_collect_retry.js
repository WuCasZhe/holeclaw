#!/usr/bin/env node
const assert = require('node:assert/strict');
const { response, runCollector } = require('./collector_harness');

const config = {
  report_start_timestamp: 100,
  scan_start_timestamp: 100,
  end_timestamp: 300,
  min_comments: 0,
  min_favorites: null,
  match_mode: 'all',
  start_page: 1,
  page_size: 500,
  max_pages: 1,
  pages_before: 0,
  checkpoint_pages: 500,
  cache_chunk_pages: 1,
  request_concurrency: 1,
  delay_min_ms: 600,
  delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};

(async () => {
  const originalDateNow = Date.now;
  let now = Date.parse('2026-08-22T12:00:00Z');
  const sleeps = [];
  let attempts = 0;
  Date.now = () => now;

  try {
    const { sinkPayloads } = await runCollector({
      config,
      onSleep: (milliseconds) => {
        sleeps.push(milliseconds);
        now += milliseconds;
      },
      remoteFetch: async () => {
        attempts += 1;
        if (attempts === 1) {
          return response(
            { code: 42900 },
            {
              status: 429,
              retryAfter: new Date(now + 30_000).toUTCString(),
            },
          );
        }
        return response({
          code: 20000,
          data: {
            list: [{
              pid: 'retry-ok',
              timestamp: 150,
              reply: 1,
              likenum: 0,
              type: 'text',
              text: 'retried',
            }],
          },
        });
      },
    });

    assert.equal(attempts, 2);
    assert.ok(sleeps.some((milliseconds) => milliseconds >= 29_000));
    assert.ok(sinkPayloads[0].telemetry.retry_backoff_ms >= 29_000);
    assert.equal(sinkPayloads[0].terminal, true);
    process.stdout.write('collect Retry-After test: ok\n');
  } finally {
    Date.now = originalDateNow;
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
