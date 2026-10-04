// Pure source tests: no real backend, browser, credentials, or LLM.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function moduleFile(name){
  const exports={},context={exports,Response,ReadableStream,TextDecoder,DOMException};
  const source=fs.readFileSync(__dirname+'/../src/social/'+name+'.ts','utf8');
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,context);
  return exports;
}
test('reading preferences/drafts are scoped by backend, home and actor; clearing drops them',()=>{
  const s=moduleFile('viewState'),key=s.socialViewKey('backend','aning','a','draft');
  s.writeView(key,'草稿');assert.equal(s.readView(key,''),'草稿');
  assert.equal(s.readView(s.socialViewKey('other','aning','a','draft'),''),'');
  assert.equal(s.readView(s.socialViewKey('backend','aning','b','draft'),''),'');
  s.clearSocialViews();assert.equal(s.readView(key,''),'');
});
test('split UTF8 records deliver progress before complete and return only final page',async()=>{
  const {readTimelineStream}=moduleFile('timelineStream'),encoder=new TextEncoder();
  const page={items:[{content:'中文'}],next_cursor:'final-only'};
  const raw=encoder.encode(JSON.stringify({type:'source',items:[{content:'先到'}]})+'\n'+JSON.stringify({type:'complete',page})+'\n');
  const response=new Response(new ReadableStream({start(c){for(let i=0;i<raw.length;i+=5)c.enqueue(raw.slice(i,i+5));c.close();}}));
  const events=[];const result=await readTimelineStream(response,new AbortController().signal,event=>events.push(event));
  assert.deepEqual(JSON.parse(JSON.stringify(result)),page);assert.equal(events[0].type,'source');assert.equal(events[0].items[0].content,'先到');
});
test('unfinished stream never yields a final cursor; abort cancels the read',async()=>{
  const {readTimelineStream}=moduleFile('timelineStream');
  await assert.rejects(readTimelineStream(new Response('{"type":"source","items":[]}\n'),new AbortController().signal,()=>{}),/尚未读完/);
  let cancelled=false;const controller=new AbortController();
  const response=new Response(new ReadableStream({cancel(){cancelled=true;}}));
  const pending=readTimelineStream(response,controller.signal,()=>{});controller.abort();
  await assert.rejects(pending,{name:'AbortError'});assert.equal(cancelled,true);
});
