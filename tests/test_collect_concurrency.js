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
  max_pages: 3,
  pages_before: 0,
  checkpoint_pages: 500,
  cache_chunk_pages: 1,
  request_concurrency: 3,
  delay_min_ms: 600,
  delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};

const completionOrder = [];
let remoteInFlight = 0;
let maxRemoteInFlight = 0;

const remoteFetch = async (url) => {
  const value = String(url);
  const pageNumber = Number(new URL(value, 'https://treehole.pku.edu.cn').searchParams.get('page'));
  remoteInFlight += 1;
  maxRemoteInFlight = Math.max(maxRemoteInFlight, remoteInFlight);
  await new Promise((resolve) => {
    if (pageNumber === 1) {
      setImmediate(() => setImmediate(resolve));
    } else {
      setImmediate(resolve);
    }
  });
  remoteInFlight -= 1;
  completionOrder.push(pageNumber);
  const posts = pageNumber === 1
    ? [{ pid: 'newer', timestamp: 180, reply: 2, likenum: 1, type: 'text', text: 'newer' }]
    : [{
        pid: `older-${pageNumber}`,
        timestamp: pageNumber === 2 ? 90 : 80,
        reply: 3,
        likenum: 2,
        type: 'text',
        text: 'older',
      }];
  return response({ code: 20000, data: { list: posts } });
};

(async () => {
    const { result, sinkPayloads } = await runCollector({ config, remoteFetch });
    assert.equal(maxRemoteInFlight, 3, 'list requests should overlap within the configured cap');
    assert.equal(completionOrder.at(-1), 1, 'the mock should complete page 1 out of order');
    assert.deepEqual(
      sinkPayloads.map((payload) => payload.start_page),
      [1, 2],
      'sink commits must remain ordered',
    );
    assert.deepEqual(
      sinkPayloads.map((payload) => payload.end_page),
      [1, 2],
      'each committed cache chunk must remain contiguous',
    );
    assert.equal(sinkPayloads.at(-1).terminal, true);
    assert.equal(sinkPayloads.at(-1).reached_start, true);
    assert.equal(result, null, 'the collector result is delivered only through the sink');
    assert.ok(sinkPayloads.every((payload) => payload.schema_version === 2));
    assert.deepEqual(sinkPayloads[0].matched_pids, ['newer']);
    assert.ok(
      sinkPayloads.every((payload) =>
        payload.rows.every((row) => !Object.hasOwn(row, 'source_page'))),
      'source_page must remain page-local collector state',
    );
    assert.equal(
      Object.hasOwn(sinkPayloads.at(-1), 'result'),
      false,
      'terminal state must not be duplicated in a nested result',
    );
    assert.equal(
      sinkPayloads.reduce(
        (total, payload) => total + payload.telemetry.overfetch_pages,
        0,
      ),
      1,
      'a fetched page beyond the time boundary must not advance the checkpoint',
    );
    assert.equal(
      Math.max(...sinkPayloads.map((payload) => payload.telemetry.max_in_flight)),
      3,
    );
    process.stdout.write('collect concurrency smoke test: ok\n');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
