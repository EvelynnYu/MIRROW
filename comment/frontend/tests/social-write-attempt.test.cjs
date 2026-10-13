const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),{webcrypto}=require('node:crypto');
const loaded={};
function load(name){
 if(loaded[name])return loaded[name];
 const exports={};loaded[name]=exports;
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/../src/social/'+name+'.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,
  {exports,Map,Uint8Array,crypto:webcrypto,require:path=>load(path.replace('./',''))});
 return exports;
}
test('ambiguous retry survives view switches; new payload/destination/success starts a new attempt',()=>{
 const {beginSocialWrite:begin,completeSocialWrite:complete}=load('writeAttempt');
 const body={content:'demo'},one=begin('home/post',body);
 assert.equal(begin('home/post',body),one);
 assert.notEqual(begin('remote/post',body),one);
 const changed=begin('home/post',{content:'edited'});
 assert.notEqual(changed,one);
 complete('home/post',one); // late ACK cannot clear a newer draft
 assert.equal(begin('home/post',{content:'edited'}),changed);
 complete('home/post',changed);
 assert.notEqual(begin('home/post',{content:'edited'}),changed);
 assert.match(one,/^[a-f0-9]{32}$/);
});
