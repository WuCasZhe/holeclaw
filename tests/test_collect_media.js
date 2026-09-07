const assert = require('node:assert/strict');
const {response, runCollector} = require('./collector_harness');
const config = {
  archive: true, archive_run: 'images', extract_images: true, download_images: true,
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 200,
  min_comments: null, min_favorites: 20, page_size: 500, max_pages: 1,
  checkpoint_pages: 100, cache_chunk_pages: 1, request_concurrency: 4,
  delay_min_ms: 600, delay_max_ms: 2000,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test',
};

async function collect(mode) {
  let plans = 0;
  let cancel;
  const runConfig = mode === 'cancel_image' ? {...config, control_url: config.sink_url.replace('/ingest?', '/control?')} : config;
  const remote = [];
  const result = await runCollector({config: runConfig,
    sinkFetch: async (url, options) => {
      const p = JSON.parse(options.body);
      if (p.archive_prepare) return response({resumes: {'1': {next_page: 1,
        complete: mode === 'reuse', post_known: mode === 'reuse'}}});
      if (p.archive_media_plan) return response({images: plans++ === 0 && mode !== 'reuse'
        ? [{media_key: 'id:10', url: 'https://treehole.pku.edu.cn/chapi/api/v3/media/getMediaBinary?id=10'}] : []});
      return response({ok: true});
    },
    remoteFetch: async (url, options) => {
      if (url === runConfig.control_url) return new Promise(resolve => {cancel = resolve;});
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      remote.push(parsed.pathname);
      assert.equal(options.headers.authorization, 'test-token');
      assert.ok(options.signal instanceof AbortSignal, 'downloads must participate in cancellation');
      if (parsed.pathname.endsWith('/list_comments')) return response({code: 20000, data: {list: [
        {pid: 1, timestamp: 150, reply: 1, likenum: 21, text: 'selected'},
        {pid: 2, timestamp: 99, reply: 1, likenum: 21, text: 'boundary'},
      ]}});
      if (parsed.pathname.endsWith('/hole/one')) return response(mode === 'deleted'
        ? {code: 41001, msg: '树洞不存在'} : {code: 20000, data: {hole: {media_ids: '10,11'}}});
      if (parsed.pathname.endsWith('/comment/list')) {
        assert.equal(parsed.searchParams.get('limit'), '100');
        return response({code: 20000, data: {list: parsed.searchParams.get('page') === '1'
          ? [{cid: 9, text: 'reply', media_ids: '12'}] : []}});
      }
      assert.ok(parsed.pathname.endsWith('/getMediaBinary'));
      if (mode === 'cancel_image') return new Response(new ReadableStream({start(stream) {
        options.signal.addEventListener('abort', () => stream.error(new Error('aborted image stream')), {once: true});
        queueMicrotask(() => cancel(response({cancelled: true})));
      }}), {headers: {'content-type': 'image/png'}});
      if (mode === 'expired') return new Response('', {status: 401});
      if (mode === 'missing_file') return new Response('', {status: 404});
      return new Response(new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
        {headers: {'content-type': 'image/png'}});
    },
  });
  return {...result, remote};
}

async function nestedArchiveRequestsShareEightSlots() {
  let active = 0;
  let peak = 0;
  let commentRequests = 0;
  let imageRequests = 0;
  const planned = new Set();
  const result = await runCollector({
    config: {...config, request_concurrency: 8},
    sinkFetch: async (_url, options) => {
      const payload = JSON.parse(options.body);
      if (payload.archive_prepare) return response({resumes: Object.fromEntries(
        payload.posts.map(post => [post.pid, {next_page: 1, complete: false, post_known: true}]))});
      if (payload.archive_media_plan) {
        const pid = payload.post.pid;
        const images = planned.has(pid) ? [] : Array.from({length: 8}, (_, i) => ({
          media_key: `${pid}:${i}`, url: `https://treehole.pku.edu.cn/chapi/api/v3/media/getMediaBinary?id=${pid}-${i}`,
        }));
        planned.add(pid);
        return response({images});
      }
      return response({ok: true});
    },
    remoteFetch: async url => {
      active++;
      peak = Math.max(peak, active);
      await new Promise(resolve => setImmediate(resolve));
      active--;
      const parsed = new URL(url, 'https://treehole.pku.edu.cn');
      if (parsed.pathname.endsWith('/list_comments')) return response({code: 20000, data: {list:
        Array.from({length: 16}, (_, i) => ({pid: String(i + 1), timestamp: 150, reply: 1, likenum: 21, text: 'post'})),
      }});
      if (parsed.pathname.endsWith('/comment/list')) {
        commentRequests++;
        return response({code: 20000, data: {list: []}});
      }
      assert.ok(parsed.pathname.endsWith('/getMediaBinary'));
      imageRequests++;
      return new Response(new Uint8Array([137, 80, 78, 71]), {headers: {'content-type': 'image/png'}});
    },
  });
  assert.equal(peak, 8, 'nested per-post image pools must share eight global permits');
  assert.equal(commentRequests, 16);
  assert.equal(imageRequests, 128);
  assert.equal(result.sinkPayloads.filter(p => p.archive_media_file).length, 128);
  assert.equal(result.sinkPayloads.at(-1).telemetry.max_in_flight, 8);
}

(async () => {
  const first = await collect('normal');
  assert.deepEqual(first.sinkPayloads.find(p => p.archive_post_media).post.media_ids, ['10', '11']);
  assert.deepEqual(first.sinkPayloads.find(p => p.archive_comments).comments[0].media_ids, ['12']);
  assert.equal(first.sinkPayloads.find(p => p.archive_media_file).data, 'iVBORw0KGgo=');
  assert.equal((await collect('reuse')).remote.length, 1, 'known images and complete comments need no detail calls');
  const deleted = await collect('deleted');
  assert.ok(deleted.sinkPayloads.some(p => p.archive_media_unavailable));
  assert.equal(deleted.remote.length, 2);
  assert.equal((await collect('missing_file')).sinkPayloads.find(p => p.archive_media_file).status, 'unavailable');
  await assert.rejects(collect('expired'), /Authentication expired/);
  const started = performance.now();
  await assert.rejects(collect('cancel_image'), /abort|cancel/i);
  assert.ok(performance.now() - started < 1000, 'image body cancellation must not wait for a timeout');
  await nestedArchiveRequestsShareEightSlots();
  console.log('image collector tests: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
