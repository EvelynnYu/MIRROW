// Execute the real remote-page delete handler; transport and data are in memory.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const source=fs.readFileSync(__dirname+'/../src/pages/SocialRemoteFeed.tsx','utf8');
const handler=source.slice(source.indexOf('  const removeComment ='),source.indexOf('  const updatePerson ='));
async function remove(row){
  let items=[{id:'post',comments:[row]}],replies={post:row.id};
  const calls=[];
  const context={busy:false,actorId:'visitor:human',replyTo:replies,
    window:{confirm:()=>true},setBusy(){},setError(){},setStatus(){},
    request:async(path,options)=>{calls.push([path,options.method]);return {deleted:true};},
    setItems:update=>{items=update(items)},setReplyTo:update=>{replies=update(replies)}};
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(handler+'\nglobalThis.runDelete=removeComment;',
    {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText,context);
  await context.runDelete(items[0],row);
  return {calls,items,replies};
}
test('remote human can use server-granted deletion for their machine comment',async()=>{
  const result=await remove({id:'machine-comment',author:'visitor:machine',can_delete:true});
  assert.deepEqual(result.calls,[['/moments/post/comments/machine-comment','DELETE']]);
  assert.equal(result.items[0].comments.length,0);
  assert.equal(result.replies.post,'');
});
test('remote explicit denial is respected; legacy self-only fallback still works',async()=>{
  assert.equal((await remove({id:'denied',author:'visitor:human',can_delete:false})).calls.length,0);
  assert.equal((await remove({id:'mine',author:'visitor:human'})).calls.length,1);
  assert.equal((await remove({id:'other',author:'visitor:friend'})).calls.length,0);
});

// Run the actual candidate-loading effect with a deterministic hook harness.
// No DOM, backend, keys or external network are used.
async function mentionCandidates(status){
  const effects=[],timers=[],calls=[],states=[];let stateIndex=0;
  const hooks={
    useState(initial){const i=stateIndex++;states[i]=i===0?true:initial;return [states[i],value=>{states[i]=value}];},
    useRef:initial=>({current:initial}),useMemo:fn=>fn(),useEffect:fn=>effects.push(fn),
  };
  const code=fs.readFileSync(__dirname+'/../src/components/SocialMentions.tsx','utf8');
  const context={exports:{},require:name=>name==='react'?hooks:name==='react/jsx-runtime'?{jsx:()=>null,jsxs:()=>null}:{},
    AbortController,URL,Response,window:{location:{href:'http://fixture.invalid/'},setTimeout:fn=>{timers.push(fn);return 1},clearTimeout(){}},
    fetch:async(url,options)=>{calls.push({url,options});return Response.json(status===200?{capability:'mentions_v1',items:[]}:{detail:'temporary'}, {status});}};
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,context);
  context.exports.default({endpoint:'http://fixture.invalid/api/social-sites/demo/mention-people',selected:[],onChange(){}});
  const cleanup=effects[2]();await timers[0]();cleanup();
  return {calls,supported:states[3],problem:states[5]};
}
test('remote mention picker sends the private management header',async()=>{
  const result=await mentionCandidates(200);
  assert.equal(result.calls[0].options.headers?.['X-MIRROW-Lounge-Admin'],'1');
  assert.equal(result.supported,true);
});
test('temporary mention failure keeps retry available; real unsupported endpoint stays disabled',async()=>{
  const temporary=await mentionCandidates(503);
  assert.notEqual(temporary.supported,false);
  assert.ok(temporary.problem);
  assert.equal((await mentionCandidates(404)).supported,false);
});
