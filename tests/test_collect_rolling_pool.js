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
  max_pages: null,
  pages_before: 0,
  checkpoint_pages: 100,
  cache_chunk_pages: 1,
  request_concurrency: 3,
  delay_min_ms: 600,
  delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};

const events = [];
let remoteInFlight = 0;
let maxRemoteInFlight = 0;
let releaseFirstPage;
const firstPageGate = new Promise((resolve) => {
  releaseFirstPage = resolve;
});

const remoteFetch = async (url) => {
  const pageNumber = Number(
    new URL(String(url), 'https://treehole.pku.edu.cn').searchParams.get('page'),
  );
  events.push(`start:${pageNumber}`);
  remoteInFlight += 1;
  maxRemoteInFlight = Math.max(maxRemoteInFlight, remoteInFlight);
  if (pageNumber === 2) {
    await firstPageGate;
  } else {
    await new Promise((resolve) => setImmediate(resolve));
  }
  if (pageNumber === 5) releaseFirstPage();
  remoteInFlight -= 1;
  events.push(`done:${pageNumber}`);
  return response({
    code: 20000,
    data: {
      list: [{
        pid: `post-${pageNumber}`,
        timestamp: pageNumber === 6 ? 90 : 200 - pageNumber,
        reply: 1,
        likenum: 0,
        type: 'text',
        text: `page ${pageNumber}`,
      }],
    },
  });
};

(async () => {
  const { sinkPayloads } = await runCollector({ config, remoteFetch });
  assert.equal(maxRemoteInFlight, 3);
  assert.ok(
    events.indexOf('start:5') < events.indexOf('done:2'),
    'after the probe, a free worker must fetch page 5 before slow page 2 completes',
  );
  assert.deepEqual(
    sinkPayloads.map((payload) => payload.start_page),
    [1, 2, 3, 4, 5, 6],
    'rolling fetch completion must still commit pages in order',
  );
  assert.equal(sinkPayloads.at(-1).terminal, true);
  assert.equal(sinkPayloads.at(-1).reached_start, true);
  process.stdout.write('collect rolling pool test: ok\n');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
