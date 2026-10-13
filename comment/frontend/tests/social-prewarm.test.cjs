const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),path=require('node:path');
function setup(fetch){
 const modules=new Map();
 function load(file){
  if(file.includes('/pages/'))return {default:()=>null};
  if(modules.has(file))return modules.get(file);
  const exports={};modules.set(file,exports);
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,
   {exports,require:name=>load(path.resolve(path.dirname(file),name)+'.ts'),fetch,Map,Set,Date,Promise,JSON,URLSearchParams,AbortController,DOMException,TextDecoder,setTimeout,clearTimeout});
  return exports;
 }
 return {api:load(path.resolve(__dirname,'../src/social/prewarm.ts')),snap:load(path.resolve(__dirname,'../src/social/timelineSnapshot.ts'))};
}
const page={items:[{id:'one'}],has_more:false,next_cursor:null,unavailable:[],omitted_sources:0};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const turn=()=>new Promise(resolve=>setImmediate(resolve));

test('warm and foreground join one read; page cancellation leaves the warm read alive',async()=>{
 const pending=deferred(),calls=[];const {api,snap}=setup((url,options)=>{calls.push({url,options});return calls.length===1?pending.promise:Promise.resolve(Response.json(page));});
 const warm=api.readSocialTimeline('/one','all',[]),controller=new AbortController();
 const foreground=api.readSocialTimeline('/one','all',[],null,controller.signal);
 controller.abort();await assert.rejects(foreground,{name:'AbortError'});
 assert.equal(calls.length,1);assert.equal(calls[0].options.signal.aborted,false);
 pending.resolve(Response.json(page));await warm;
 assert.equal(snap.getTimelineSnapshot(api.timelineSnapshotKey('/one','all',[])).items[0].id,'one');
 // Completed old bodies are displayed as snapshots; interaction needs a fresh read.
 await api.readSocialTimeline('/one','all',[]);assert.equal(calls.length,2);
});
test('failed shared reads retry; APIs, filters and source registries are separate',async()=>{
 let fail=true;const calls=[];const {api}=setup(async url=>{calls.push(url);if(fail){fail=false;throw Error('offline');}return Response.json(page);});
 await assert.rejects(api.readSocialTimeline('/one','all',[]));
 await api.readSocialTimeline('/one','all',[]);
 await api.readSocialTimeline('/two','private',[]);
 await api.readSocialTimeline('/one','public',[{id:'changed',origin:'example',enabled:true,has_human_key:true}]);
 assert.equal(calls.length,4);assert.ok(calls[2].includes('visibility_filter=private'));
});
test('invalidated late responses cannot refill a snapshot or registry',async()=>{
 const old=deferred();const {api,snap}=setup(()=>old.promise);
 const read=api.readSocialTimeline('/one','all',[]);api.invalidateSocialPrewarm('/one');
 old.resolve(Response.json(page));await assert.rejects(read,{name:'AbortError'});
 assert.equal(snap.getTimelineSnapshot(api.timelineSnapshotKey('/one','all',[])),undefined);
 const sites=deferred(),env=setup(()=>sites.promise),registry=env.api.readSocialSites('/one');
 env.api.invalidateSocialPrewarm('/one');sites.resolve(Response.json({sites:[]}));await registry;
 assert.equal(env.api.cachedSocialSites('/one'),undefined);
});
test('background warmup only uses pure reads and is bounded between foreground resumes',async()=>{
 const calls=[];const {api}=setup(async(url,options)=>{calls.push({url,options});return Response.json(url.endsWith('/social-sites')?{sites:[]}:page);});
 await Promise.all([api.prewarmSocial('/one'),api.prewarmSocial('/one')]);await api.prewarmSocial('/one');
 assert.equal(calls.length,2);assert.ok(calls.every(c=>!c.options.method));
 assert.ok(calls.some(c=>c.url.includes('/timeline/stream?')));
 api.invalidateSocialPrewarm('/one');await api.prewarmSocial('/one');assert.equal(calls.length,4);
});
test('invalidation during registry warmup does not start an obsolete timeline',async()=>{
 const registry=deferred(),calls=[];const {api}=setup(url=>{calls.push(url);return registry.promise;});
 const warm=api.prewarmSocial('/one');await turn();api.invalidateSocialPrewarm('/one');
 registry.resolve(Response.json({sites:[]}));await warm;assert.equal(calls.length,1);
});
test('NDJSON progress reaches a joining page and JSON fallback remains available',async()=>{
 let release;const encoder=new TextEncoder();let calls=0;
 const {api}=setup(async()=>{calls++;return new Response(new ReadableStream({start(c){
  c.enqueue(encoder.encode(JSON.stringify({type:'source',items:[],finished_sources:1,total_sources:2})+'\n'));
  release=()=>{c.enqueue(encoder.encode(JSON.stringify({type:'complete',page})+'\n'));c.close();};
 }}),{headers:{'Content-Type':'application/x-ndjson'}});});
 const warm=api.readSocialTimeline('/one','all',[]);await turn();const events=[];
 const joined=api.readSocialTimeline('/one','all',[],null,undefined,e=>events.push(e));
 assert.equal(events[0].finished_sources,1);release();await Promise.all([warm,joined]);assert.equal(calls,1);assert.equal(events.at(-1).type,'complete');
 const urls=[],fallback=setup(async url=>{urls.push(url);return url.includes('/stream?')?new Response('',{status:404}):Response.json(page);});
 await fallback.api.readSocialTimeline('/one','all',[]);assert.equal(urls.length,2);assert.ok(urls[1].includes('/timeline?'));
});
