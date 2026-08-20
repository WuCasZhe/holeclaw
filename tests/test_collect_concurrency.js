#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const collectorPath = path.join(__dirname, '..', 'scripts', 'collect.js');
const collector = vm.runInThisContext(fs.readFileSync(collectorPath, 'utf8'), {
  filename: collectorPath,
});

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

const sinkPayloads = [];
const completionOrder = [];
let remoteInFlight = 0;
let maxRemoteInFlight = 0;

const response = (body) => ({
  status: 200,
  ok: true,
  headers: { get: () => null },
  text: async () => JSON.stringify(body),
});

const originalFetch = global.fetch;
const originalSetTimeout = global.setTimeout;
global.setTimeout = (callback) => {
  callback();
  return 0;
};
global.fetch = async (url, options = {}) => {
  const value = String(url);
  if (value.startsWith(config.sink_url)) {
    sinkPayloads.push(JSON.parse(options.body));
    return response({ ok: true });
  }

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

let evaluateCount = 0;
const page = {
  evaluate: async (fn, argument) => {
    evaluateCount += 1;
    return evaluateCount === 1 ? config : fn(argument);
  },
  waitForRequest: async () => ({
    allHeaders: async () => ({ authorization: 'test-token' }),
  }),
  reload: async () => undefined,
};

(async () => {
  try {
    const result = await collector(page);
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
    assert.equal(result.pages, 2);
    assert.equal(result.next_page, 3);
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
  } finally {
    global.fetch = originalFetch;
    global.setTimeout = originalSetTimeout;
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
