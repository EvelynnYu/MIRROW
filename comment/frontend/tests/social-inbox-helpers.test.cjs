const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function load(path){const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,URL,Map});return exports;}
test('remote avatar compares origin, not registered route prefix',()=>{
 const {remoteAvatarUrl}=load(__dirname+'/../src/social/remoteAvatarUrl.ts'),hash='a'.repeat(64)+'.png';
 assert.equal(remoteAvatarUrl('/proxy','https://example.test/comment','https://example.test/social/v1/profile-links/avatars/'+hash),'/proxy/avatars/'+hash+'?linked=true');
 assert.equal(remoteAvatarUrl('/proxy','https://example.test/comment','https://evil.test/social/v1/avatars/'+hash),'');
 assert.equal(remoteAvatarUrl('/proxy','https://example.test','https://user:secret@example.test/social/v1/avatars/'+hash),'');
 assert.equal(remoteAvatarUrl('/proxy','https://example.test','https://example.test/other/'+hash),'');
});
test('completed-page cache is bounded and filter/source scoped',()=>{
 const {saveTimelineSnapshot:save,getTimelineSnapshot:get}=load(__dirname+'/../src/social/timelineSnapshot.ts');
 for(let i=0;i<5;i++)save('scope'+i,{items:[{id:String(i)}]});
 assert.equal(get('scope0'),undefined);assert.equal(get('scope4').items[0].id,'4');assert.equal(get('private'),undefined);
});

test('visible read claims preserve source, comment and snapshot-time boundaries',()=>{
 const {noticeWasVisible:match}=load(__dirname+'/../src/social/visibleReads.ts');
 const n={site_id:'one',moment_id:'post',actor:'friend',created_at:10,kind:'moment'};
 const seen={site_id:'one',moment_id:'post',part:'content',through:20};
 assert.equal(match(n,seen),true);
 assert.equal(match({...n,site_id:'two'},seen),false);
 assert.equal(match({...n,moment_id:'other'},seen),false);
 assert.equal(match({...n,created_at:21},seen),false);
 assert.equal(match({...n,kind:'comment',comment_id:'hidden'},seen),false);
 assert.equal(match({...n,kind:'mention',comment_id:'hidden'},seen),false);
 assert.equal(match({...n,kind:'mention'},seen),true);
 assert.equal(match({...n,kind:'comment',comment_id:'shown'},{...seen,part:'comment',comment_id:'shown'}),true);
 assert.equal(match({...n,kind:'comment',comment_id:'hidden'},{...seen,part:'comment',comment_id:'shown'}),false);
 assert.equal(match({...n,kind:'like'},{...seen,part:'likes',actors:['friend']}),true);
 assert.equal(match({...n,kind:'like',actor:'other'},{...seen,part:'likes',actors:['friend']}),false);
});
