const assert = require('node:assert/strict');
const { response, runCollector } = require('./collector_harness');
const config = {
  archive: true, archive_run: 'test-run',
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 300,
  min_comments: null, min_favorites: null, start_page: 1,
  page_size: 500, max_pages: 1, checkpoint_pages: 1,
  cache_chunk_pages: 1, request_concurrency: 2,
  delay_min_ms: 600, delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};

async function collect(mode) {
  const fetched = [];
  const result = await runCollector({
    config,
    sinkFetch: async (url, options) => {
      const payload = JSON.parse(options.body);
      return response(payload.archive_prepare
        ? {ok: true, resumes: {'1': {next_page: mode === 'resume' ? 2 : 1, complete: mode === 'complete'}}}
        : {ok: true});
    },
    remoteFetch: async (url) => {
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      if (parsed.pathname.endsWith('/list_comments')) return response({code: 20000, data: {list: [
        {pid: 1, timestamp: 150, reply: 2, text: 'two replies'},
        {pid: 2, timestamp: 99, reply: 10, text: 'outside range'},
      ]}});
      assert.ok(parsed.pathname.endsWith('/comment/list'), 'must not fetch media');
      assert.equal(parsed.searchParams.get('pid'), '1');
      const page = Number(parsed.searchParams.get('page'));
      fetched.push(page);
      if (mode === 'malformed') return response({code: 20000, data: {}});
      return response({code: 20000, data: {list: page <= 2 || mode === 'repeat'
        ? [{cid: mode === 'repeat' ? 1 : page, text: 'comment', timestamp: 151, media_ids: 'ignored'}] : []}});
    },
  });
  return {...result, fetched};
}

async function cachedAndFiltered(cacheOnly) {
  const requests = [];
  const old = {pid: 'old', timestamp: 150, reply: 101, likenum: 51, text: 'cached'};
  const current = {pid: 'new', timestamp: 250, reply: 102, likenum: 52, text: 'new'};
  const result = await runCollector({
    config: {...config, min_comments: 100, min_favorites: 50, max_pages: 2,
      cache_chunk_pages: 5, checkpoint_pages: 100,
      archive_cached_pages: 1, archive_cache_only: cacheOnly, scan_start_timestamp: cacheOnly ? 100 : 200},
    sinkFetch: async (url, options) => {
      const payload = JSON.parse(options.body);
      if (payload.archive_source) return response({ok: true, posts: [old]});
      if (payload.archive_prepare) return response({ok: true, resumes: Object.fromEntries(
        payload.posts.map(post => [post.pid, {next_page: 1, complete: post.pid === 'old'}]))});
      return response({ok: true});
    },
    remoteFetch: async url => {
      requests.push(url);
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      if (parsed.pathname.endsWith('/list_comments')) {
        assert.equal(parsed.searchParams.get('page'), '1', 'cached pages must not shift remote page numbers');
        return response({code: 20000, data: {list: [current,
          {pid: 'equal', timestamp: 249, reply: 100, likenum: 50, text: 'not above thresholds'},
          {pid: 'outside', timestamp: 99, reply: 999, likenum: 999, text: 'outside'}]}});
      }
      assert.equal(parsed.searchParams.get('pid'), 'new', 'only uncached matching comments may be fetched');
      return response({code: 20000, data: {list: []}});
    },
  });
  assert.ok(result.sinkPayloads.at(-1).reached_start);
  const listChunks = result.sinkPayloads.filter(p => p.start_page);
  assert.equal(listChunks[0].archive_cached, true);
  assert.equal(listChunks[0].pages, 1, 'cached/network boundary must flush even below five pages');
  if (!cacheOnly) {
    assert.equal(listChunks.length, 2);
    assert.equal(listChunks[1].start_page, 2);
    assert.equal(listChunks[1].archive_cached, false);
    assert.ok(listChunks[1].rows.some(row => row.pid === 'new'), 'network rows must reach SQLite');
  }
  if (cacheOnly) assert.equal(requests.length, 0, 'complete caches need no API calls');
  else assert.equal(requests.length, 2, 'only the new head and selected comments need requests');
}

async function boundaryAwarePrefetch(longRange) {
  let active = 0;
  let peak = 0;
  let listRequests = 0;
  const result = await runCollector({
    config: {...config, max_pages: null, end_timestamp: 2000, request_concurrency: 3,
      cache_chunk_pages: 5, checkpoint_pages: 100},
    sinkFetch: async (url, options) => {
      const payload = JSON.parse(options.body);
      return response(payload.archive_prepare ? {ok: true, resumes: Object.fromEntries(
        payload.posts.map(post => [post.pid, {next_page: 1, complete: false}]))} : {ok: true});
    },
    remoteFetch: async url => {
      assert.ok(url.includes('/list_comments'), 'zero-comment posts need no extra endpoint');
      const page = Number(new URL(url, 'https://treehole.pku.edu.cn').searchParams.get('page'));
      listRequests++;
      active++;
      peak = Math.max(peak, active);
      await new Promise(resolve => setImmediate(resolve));
      active--;
      const posts = longRange
        ? [{pid: String(page), timestamp: page >= 6 ? 99 : 900 - page * 100, reply: 0, text: 'post'}]
        : [{pid: '1', timestamp: 150, reply: 0, text: 'selected'},
           {pid: '2', timestamp: 99, reply: 0, text: 'boundary'}];
      return response({code: 20000, data: {list: posts}});
    },
  });
  if (longRange) {
    assert.ok(peak > 1 && peak <= 3, 'historical scans must retain bounded parallelism after probing');
    const chunks = result.sinkPayloads.filter(p => p.start_page);
    assert.deepEqual(chunks.map(p => [p.start_page, p.end_page, p.rows.length]), [[1,5,5], [6,6,1]]);
  } else {
    assert.equal(listRequests, 1, 'a boundary in the first page must prevent speculative list requests');
    assert.equal(result.sinkPayloads.at(-1).telemetry.overfetch_pages, 0);
  }
}

async function locallyReusedCandidates(cacheOnly) {
  const requests = [];
  const result = await runCollector({
    config: {...config, archive_cached_pages: 2, archive_cache_only: cacheOnly,
      max_pages: 3, checkpoint_pages: 100, cache_chunk_pages: 1},
    sinkFetch: async (url, options) => {
      const payload = JSON.parse(options.body);
      if (payload.archive_source) return response(payload.page === 1
        ? {ok: true, posts: [], source_count: 500, oldest: 160, reused: 500}
        : {ok: true, posts: [{pid: 'pending', timestamp: 150, reply: 1, text: 'cached', observed_at: 1000}],
           source_count: 2, oldest: 140, reused: 1});
      if (payload.archive_prepare) {
        assert.deepEqual(payload.posts.map(p => p.pid), ['pending']);
        assert.equal(payload.posts[0].observed_at, 1000, 'cached observations must survive the browser roundtrip');
        return response({ok: true, resumes: {pending: {next_page: 1, complete: false}}});
      }
      return response({ok: true});
    },
    remoteFetch: async url => {
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      requests.push(parsed.pathname);
      if (parsed.pathname.endsWith('/list_comments')) {
        assert.equal(parsed.searchParams.get('page'), '1', 'local reuse must preserve network page offsets');
        return response({code: 20000, data: {list: [{pid: 'boundary', timestamp: 99, reply: 0, text: 'old'}]}});
      }
      assert.ok(parsed.pathname.endsWith('/comment/list'));
      assert.equal(parsed.searchParams.get('pid'), 'pending');
      return response({code: 20000, data: {list: []}});
    },
  });
  const chunks = result.sinkPayloads.filter(p => p.start_page);
  assert.equal(chunks.length, cacheOnly ? 2 : 3, 'an empty filtered page must not end collection');
  assert.equal(chunks[0].oldest, 160);
  assert.equal(chunks[0].terminal, false);
  assert.equal(chunks[1].oldest, 140, 'oldest date includes locally reused posts');
  assert.equal(requests.length, cacheOnly ? 1 : 2);
  assert.equal(result.sinkPayloads.filter(p => p.archive_prepare).length, 1);
  assert.ok(chunks.at(-1).reached_start);
}

async function cappedComments(extractImages, savedCount = 0, pageLength = null) {
  const fetched = [];
  const saved = Array.from({length: savedCount}, (_, i) => String(i));
  const result = await runCollector({
    config: {...config, extract_images: extractImages, comment_batch_pages: 3},
    sinkFetch: async (url, options) => {
      const payload = JSON.parse(options.body);
      return response(payload.archive_prepare ? {ok: true, resumes: {'1': {
        next_page: savedCount ? 10 : 1, complete: false,
        saved_comment_ids: saved, post_known: true,
      }}} : {ok: true});
    },
    remoteFetch: async url => {
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      if (parsed.pathname.endsWith('/list_comments')) return response({code: 20000, data: {list: [
        {pid: 1, timestamp: 150, reply: 5000, text: 'large post'},
      ]}});
      assert.ok(parsed.pathname.endsWith('/comment/list'));
      const limit = Number(parsed.searchParams.get('limit'));
      const size = pageLength || limit;
      const offset = Math.max(0, savedCount - 10) + fetched.length * size;
      fetched.push(url);
      assert.ok(fetched.length <= 150, 'collection must stop at the cap');
      return response({code: 20000, data: {list: Array.from({length: size}, (_, i) => ({cid: offset + i}))}});
    },
  });
  const chunks = result.sinkPayloads.filter(p => p.archive_comments);
  const ids = new Set([...saved, ...chunks.flatMap(p => p.comments.map(c => c.cid))]);
  assert.equal(ids.size, Math.max(1000, savedCount));
  assert.ok(chunks.at(-1).complete, 'cap must flush and complete the snapshot');
  if (savedCount >= 1000) assert.equal(fetched.length, 0);
  if (!savedCount && !pageLength) assert.equal(fetched.length, 10);
}

(async () => {
  await cappedComments(false);
  await cappedComments(true);
  await cappedComments(false, 0, 7);
  await cappedComments(true, 995);
  await cappedComments(false, 1000);
  const first = await collect('normal');
  assert.deepEqual(first.fetched, [1, 2, 3], 'short pages are not treated as exhaustion');
  const chunks = first.sinkPayloads.filter(p => p.archive_comments);
  assert.equal(chunks.length, 1, 'comment pages should share one durable batch');
  assert.equal(chunks[0].complete, true);
  assert.equal(chunks[0].comments[0].media_ids, undefined);
  assert.ok(first.sinkPayloads.at(-1).terminal, 'feed checkpoint follows comment commits');
  assert.deepEqual((await collect('resume')).fetched, [2, 3]);
  assert.deepEqual((await collect('complete')).fetched, []);
  await assert.rejects(collect('repeat'), /no progress/);
  await assert.rejects(collect('malformed'), /Invalid comment list/);
  await cachedAndFiltered(true);
  await cachedAndFiltered(false);
  await locallyReusedCandidates(true);
  await locallyReusedCandidates(false);
  await boundaryAwarePrefetch(false);
  await boundaryAwarePrefetch(true);
  console.log('archive collector tests: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
