const assert = require('node:assert/strict');
const {runCollector,response} = require('./collector_harness');
const {simulate} = require('./collector_simulation');
const config = {archive:true,archive_run:'efficiency',report_start_timestamp:100,scan_start_timestamp:100,
  end_timestamp:2000,min_comments:null,min_favorites:null,max_pages:1,request_concurrency:8,
  sink_url:'http://127.0.0.1:12345/ingest?token=test'};
const post = {pid:'1',timestamp:900,reply:1,likenum:1,text:'fixture'};
const tick = () => new Promise(resolve=>setImmediate(resolve));

async function cachedStateAvoidsPrepare() {
  const result = await runCollector({config:{...config,archive_cached_pages:1,archive_cache_only:true},
    sinkFetch:async (_url,options)=>{
      const p=JSON.parse(options.body);
      if(p.archive_source)return response({posts:[post],source_count:1,oldest:900,resumes:{'1':{complete:false,next_page:1}}});
      assert.ok(!p.archive_prepare,'unchanged cached candidate already has a prepared state');
      return response({ok:true});
    },remoteFetch:async url=>{
      assert.ok(url.includes('/comment/list'));
      return response({code:20000,data:{list:[]}});
    }});
  assert.equal(result.wirePayloads.length,4);
}

async function cachedDetailChangeRequiresPrepare() {
  let prepares=0;
  await runCollector({config:{...config,archive_cached_pages:1,archive_cache_only:true},
    sinkFetch:async (_url,options)=>{
      const p=JSON.parse(options.body);
      if(p.archive_source)return response({posts:[{...post,text:''}],source_count:1,oldest:900,resumes:{'1':{complete:true,next_page:1}}});
      if(p.archive_prepare){prepares++;assert.equal(p.posts[0].reply,2);return response({resumes:{'1':{complete:false,next_page:1}}});}
      return response({ok:true});
    },remoteFetch:async url=>response(url.includes('/hole/one')?{code:20000,data:{hole:{reply:2,text:'updated'}}}:{code:20000,data:{list:[]}})});
  assert.equal(prepares,1);
}

async function imagesDoNotOccupyCommentWorkers() {
  let release, ninthStarted=false;
  const gate=new Promise(resolve=>release=resolve);
  const planned=new Set();
  const commits=[];
  const task=runCollector({config:{...config,extract_images:true,download_images:true},
    sinkFetch:async (_url,options)=>{
      const p=JSON.parse(options.body);
      if(p.archive_prepare)return response({resumes:Object.fromEntries(p.posts.map(p=>[p.pid,{complete:false,post_known:true,next_page:1}]))});
      if(p.archive_media_plan){const images=planned.has(p.post.pid)?[]:[{media_key:p.post.pid,url:'https://test/image'}];planned.add(p.post.pid);return response({images});}
      if(p.start_page)commits.push(p.start_page);
      return response({ok:true});
    },remoteFetch:async url=>{
      if(url.includes('list_comments'))return response({code:20000,data:{list:Array.from({length:9},(_,i)=>({...post,pid:String(i+1)}))}});
      if(url.includes('/comment/list')){
        if(new URL(url,'https://test').searchParams.get('pid')==='9'){ninthStarted=true;assert.deepEqual(commits,[]);release();}
        return response({code:20000,data:{list:[]}});
      }
      await gate;
      return new Response(new Uint8Array([137,80,78,71]),{headers:{'content-type':'image/png'}});
    }});
  task.catch(()=>{});
  for(let i=0;i<200&&!ninthStarted;i++)await tick();
  const progressedWithoutImages = ninthStarted;
  if(!ninthStarted)release();
  await task;
  assert.ok(progressedWithoutImages,'ninth comment must run while the first eight posts wait for images');
  assert.deepEqual(commits,[1]);
}

async function duplicatesRefreshQueuedResume() {
  let prepares=0, comments=0, complete=false;
  let releaseFirst;
  const gate=new Promise(resolve=>releaseFirst=resolve);
  const task=runCollector({config:{...config,max_pages:2,request_concurrency:2},
    sinkFetch:async (_url,options)=>{
      const p=JSON.parse(options.body);
      if(p.archive_prepare){
        prepares++;
        if(prepares===2)releaseFirst();
        return response({resumes:Object.fromEntries(p.posts.map(p=>[p.pid,{complete:p.pid==='1'?complete:true,next_page:1}]))});
      }
      if(p.archive_comments)complete=true;
      return response({ok:true});
    },remoteFetch:async url=>{
      if(url.includes('list_comments')){const n=new URL(url,'https://test').searchParams.get('page');return response({code:20000,data:{list:[post,{...post,pid:`other-${n}`,timestamp:899}]}});}
      comments++;
      await gate;
      return response({code:20000,data:{list:[]}});
    }});
  task.catch(()=>{});
  for(let i=0;i<200&&prepares<2;i++)await tick();
  releaseFirst();
  await task;
  assert.equal(prepares,3,'second copy must refresh after the first copy finishes');
  assert.equal(comments,1,'already-completed duplicate must not rescan comments');
}

async function favoritesAreDeferredOnlyForAndRejections(mode) {
  let details=0;
  const result=await runCollector({config:{...config,archive:false,min_comments:10,min_favorites:20,match_mode:mode},
    remoteFetch:async url=>{
      if(url.includes('list_comments'))return response({code:20000,data:{list:[{...post,pid:'rejected',reply:0,likenum:null},{...post,pid:'selected',reply:11,likenum:null}]}});
      details++;
      return response({code:20000,data:{hole:{likenum:21}}});
    }});
  const chunk=result.sinkPayloads[0];
  assert.equal(details,mode==='all'?1:2);
  assert.deepEqual(chunk.favorite_deferred_pids,mode==='all'?['rejected']:[]);
  assert.equal(chunk.matched_pids.length,mode==='all'?1:2);
}

(async()=>{
  await cachedStateAvoidsPrepare();
  await cachedDetailChangeRequiresPrepare();
  await imagesDoNotOccupyCommentWorkers();
  await duplicatesRefreshQueuedResume();
  await favoritesAreDeferredOnlyForAndRejections('all');
  await favoritesAreDeferredOnlyForAndRejections('any');
  const options={pages:4,matches:9,replies:[1,1,1,1,1,1,1,1,1000]};
  const current=await simulate('priority',options);
  const baseline=await simulate('list-order',{...options,listOrder:true});
  assert.ok(current.model_wall_ms<baseline.model_wall_ms);
  assert.deepEqual(current.requests,baseline.requests);
  assert.equal(current.unique_comments,baseline.unique_comments);
  console.log('collector efficiency tests: ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
