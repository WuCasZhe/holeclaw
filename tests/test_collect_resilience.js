const assert = require('node:assert/strict');
const {runCollector, response} = require('./collector_harness');
const config = {archive: true, archive_run: 'resilience',
  report_start_timestamp: 100, scan_start_timestamp: 100, end_timestamp: 200,
  min_comments: null, min_favorites: null, max_pages: 1, request_concurrency: 2,
  sink_url: 'http://127.0.0.1:12345/ingest?token=test'};
const post = {pid: '1', timestamp: 150, reply: 1, likenum: 0, text: 'fixture'};

async function serverWaitIsNotTruncated() {
  let now = 0, attempts = 0, first, second;
  class Clock extends Date {static now() {return now;}}
  await runCollector({config, clock: Clock, onSleep: ms => now += ms,
    remoteFetch: async () => {
      if (++attempts === 1) {first = now; return response({}, {status: 429, retryAfter: '120'});}
      second = now;
      return response({code: 20000, data: {list: []}});
    }});
  assert.equal(second - first, 120000);
}

async function deletedPostsDoNotAbort(mode) {
  const result = await runCollector({config,
    remoteFetch: async url => url.includes('list_comments')
      ? response({code: 20000, data: {list: [mode === 'detail' ? {...post, text: ''} : post]}})
      : response({code: 41001}, {status: 404}),
    sinkFetch: async (_url, options) => {
      const payload = JSON.parse(options.body);
      return response(payload.archive_prepare ? {resumes: {'1': {next_page: 1, complete: false}}} : {ok: true});
    }});
  assert.equal(result.wirePayloads.filter(p => p.kind === 'archive_post_unavailable').length, 1);
  assert.ok(result.sinkPayloads.at(-1).terminal);
}

async function timeoutRetries(mode) {
  let now = 0, attempts = 0, aborted = false;
  const bodies = [];
  class Clock extends Date {static now() {return now;}}
  const stalled = options => new Promise((_, reject) => {
    options.signal.addEventListener('abort', () => {aborted = true; reject(options.signal.reason);}, {once: true});
  });
  await runCollector({config, clock: Clock, deadlineMs: 50, onSleep: ms => now += ms,
    remoteFetch: async (_url, options) => {
      if (mode === 'remote' && ++attempts === 1) return stalled(options);
      return response({code: 20000, data: {list: []}});
    },
    wireSinkFetch: async (_url, options) => {
      if (mode === 'sink') {
        bodies.push(options.body);
        if (++attempts === 1) return stalled(options);
      }
      return response({ok: true});
    }});
  assert.ok(aborted, `${mode} must abort its expired attempt`);
  if (mode === 'sink') assert.equal(bodies[0], bodies[1], 'timeout retry preserves request identity');
}

async function imageIdleTimeoutRetries() {
  let now = 0, attempts = 0, plans = 0, aborted = false;
  class Clock extends Date {static now() {return now;}}
  const result = await runCollector({config: {...config, extract_images: true, download_images: true},
    clock: Clock, deadlineMs: 50, onSleep: ms => now += ms,
    sinkFetch: async (_url, options) => {
      const p = JSON.parse(options.body);
      if (p.archive_prepare) return response({resumes: {'1': {complete: true, post_known: true}}});
      if (p.archive_media_plan) return response({images: plans++ ? [] : [{media_key: 'id:1', url: 'https://test/image'}]});
      return response({ok: true});
    },
    remoteFetch: async (url, options) => {
      if (url.includes('list_comments')) return response({code: 20000, data: {list: [post]}});
      if (++attempts === 1) return new Response(new ReadableStream({start(stream) {
        options.signal.addEventListener('abort', () => {aborted = true; stream.error(options.signal.reason);}, {once: true});
      }}), {headers: {'content-type': 'image/png'}});
      return new Response(new Uint8Array([137,80,78,71]), {headers: {'content-type': 'image/png'}});
    }});
  assert.ok(aborted);
  assert.equal(attempts, 2);
  assert.equal(result.wirePayloads.filter(p => p.kind === 'archive_media_file').length, 1);
}

async function downloadsApplyBackpressureUntilUploadCompletes() {
  let active = 0, peak = 0, uploaded = 0;
  const planned = new Set();
  const result = await runCollector({config: {...config, request_concurrency: 8, extract_images: true, download_images: true},
    sinkFetch: async (_url, options) => {
      const p = JSON.parse(options.body);
      if (p.archive_prepare) return response({resumes: Object.fromEntries(p.posts.map(p => [p.pid, {complete:true,post_known:true}]))});
      if (p.archive_media_plan) {
        const images = planned.has(p.post.pid) ? [] : Array.from({length: 6}, (_, i) => ({media_key: `${p.post.pid}:${i}`, url: 'https://test/image'}));
        planned.add(p.post.pid);
        return response({images});
      }
      if (p.archive_media_file) {
        await new Promise(resolve => setTimeout(resolve, 5));
        active--; uploaded++;
      }
      return response({ok:true});
    }, remoteFetch: async url => {
      if (url.includes('list_comments')) return response({code:20000,data:{list:[post,{...post,pid:'2'}]}});
      active++; peak = Math.max(peak, active);
      return new Response(new Uint8Array([137,80,78,71]), {headers:{'content-type':'image/png'}});
    }});
  assert.equal(uploaded, 12);
  assert.equal(peak, 2, 'slow uploads must bound the entire image pipeline');
  for (const p of result.wirePayloads.filter(p => p.kind === 'archive_media_file')) {
    assert.equal(Object.hasOwn(p.payload, 'data'), false, 'wire metadata must not carry base64');
  }
}

(async () => {
  const keepAlive = setInterval(() => {}, 1000); // AbortSignal.timeout timers are unref'ed in Node.
  try {
    await serverWaitIsNotTruncated();
    await deletedPostsDoNotAbort('comments');
    await deletedPostsDoNotAbort('detail');
    await timeoutRetries('remote');
    await timeoutRetries('sink');
    await imageIdleTimeoutRetries();
    await downloadsApplyBackpressureUntilUploadCompletes();
    console.log('collector resilience tests: ok');
  } finally {clearInterval(keepAlive);}
})().catch(error => {console.error(error);process.exitCode = 1;});
