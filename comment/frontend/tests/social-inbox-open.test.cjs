const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const exportsForTest={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/../src/social/acknowledgeInbox.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:exportsForTest,Map,Set,Promise,JSON});
const {acknowledgeInbox,inboxReference}=exportsForTest;
test('one offline source does not prevent another source confirming its exact IDs',async()=>{
 const rows=[{site_id:'offline',id:'same'},{site_id:'home',id:'same'}],confirmed=[];
 const ok=await acknowledgeInbox(rows,async(site,ids)=>{if(site==='offline')throw Error('offline');assert.deepEqual([...ids],['same']);},batch=>confirmed.push(...batch));
 assert.equal(ok,false);assert.deepEqual(confirmed,[rows[1]]);
 assert.notEqual(inboxReference(rows[0]),inboxReference(rows[1]));
});
test('large opening snapshots are confirmed in source-specific batches of at most 100',async()=>{
 const rows=Array.from({length:205},(_,i)=>({site_id:'home',id:String(i)})),sizes=[],confirmed=[];
 assert.equal(await acknowledgeInbox(rows,async(site,ids)=>{assert.equal(site,'home');sizes.push(ids.length);},batch=>confirmed.push(...batch)),true);
 assert.deepEqual(sizes,[100,100,5]);assert.deepEqual(confirmed,rows);
});
test('partial batch failure preserves later unconfirmed IDs without undoing earlier confirmation',async()=>{
 const rows=Array.from({length:150},(_,i)=>({site_id:'home',id:String(i)})),confirmed=[];let calls=0;
 assert.equal(await acknowledgeInbox(rows,async()=>{if(++calls===2)throw Error('offline');},batch=>confirmed.push(...batch)),false);
 assert.deepEqual(confirmed,rows.slice(0,100));
});
test('opening an empty inbox makes no acknowledgement request',async()=>{
 assert.equal(await acknowledgeInbox([],()=>{assert.fail('unexpected request');},()=>assert.fail('unexpected confirmation')),true);
});
