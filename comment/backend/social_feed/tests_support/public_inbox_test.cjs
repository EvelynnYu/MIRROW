const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),{test}=require('node:test');
const source=fs.readFileSync(__dirname+'/../public_inbox.js','utf8');
function fixture({presented=true,rejectRead=false,empty=false}={}){
 const nodes=[],calls=[];let actor='visitor:one',pending=null;
 const node=tag=>{const n={tag,children:[],open:false,hidden:false,textContent:'',setAttribute(){},append(...c){this.children.push(...c)},prepend(...c){this.children.unshift(...c)},replaceChildren(...c){this.children=c},showModal(){this.open=true},close(){this.open=false},remove(){this.removed=true}};nodes.push(n);return n;};
 const header=node('header'),body=node('body');
 const context={window:{},document:{visibilityState:'visible',createElement:node,querySelector:s=>s.includes('body>')?header:null,body,addEventListener(){},removeEventListener(){}},Set,Map,Date,CSS:{escape:s=>s},setInterval:()=>1,clearInterval(){}};
 vm.createContext(context);vm.runInContext(source,context);
 const row={id:'fixture',kind:'comment',actor_name:'Friend',moment_id:'post',comment_id:'comment',created_at:2};
 const stop=context.window.MirrowInbox.mount({actor,currentActor:()=>actor,api:async(path,method,payload)=>{calls.push({path,method,payload});if(path==='/notifications/read'&&rejectRead)throw Error('offline');return path==='/notifications'?{items:empty?[]:[row]}:{marked:1};},openDetail:async()=>pending?await pending:presented});
 return {nodes,calls,stop,switch:()=>{actor='visitor:two'},defer:()=>{let release;pending=new Promise(r=>release=r);return release}};
}
const settle=()=>new Promise(setImmediate);
test('listing shows all interaction types without acknowledging',async()=>{
 const f=fixture();await settle();assert.equal(f.calls.length,1);assert.equal(f.calls[0].path,'/notifications');assert.equal(f.nodes.filter(n=>n.className==='wall-inbox-row').length,1);f.stop();
});
test('body presentation acknowledges exact source-wall post notices',async()=>{
 const f=fixture();await settle();await f.nodes.find(n=>n.className==='wall-inbox-row').onclick();await settle();
 assert.equal(f.calls[1].path,'/notifications/read');assert.deepEqual(JSON.parse(JSON.stringify(f.calls[1].payload)),{notification_ids:['fixture']});f.stop();
});
test('failed body presentation leaves receipt unread',async()=>{
 const f=fixture({presented:false});await settle();await f.nodes.find(n=>n.className==='wall-inbox-row').onclick();await settle();assert.equal(f.calls.length,1);f.stop();
});
test('identity switch during detail read cannot ACK new actor',async()=>{
 const f=fixture();await settle();const release=f.defer();f.nodes.find(n=>n.className==='wall-inbox-row').onclick();f.switch();release(true);await settle();assert.equal(f.calls.length,1);f.stop();
});
test('logout disposes widgets and subsequent reads',async()=>{
 const f=fixture();await settle();f.stop();await f.nodes.find(n=>n.className==='wall-inbox-row').onclick();assert.equal(f.calls.length,1);assert.ok(f.nodes.find(n=>n.tag==='dialog').removed);
});

test('opening the bell clears unread immediately but retains this opening list',async()=>{
 const f=fixture();await settle();f.nodes.find(n=>n.className==='wall-inbox-bell').onclick();await settle();
 const bell=f.nodes.find(n=>n.className==='wall-inbox-bell'),panel=f.nodes.find(n=>n.tag==='dialog');
 assert.equal(bell.textContent,'🔔');assert.ok(panel.open);
 assert.equal(f.calls.filter(c=>c.path==='/notifications/read').length,1);
 assert.equal(panel.children.at(-1).children.filter(n=>n.className==='wall-inbox-row').length,1);
 // A stale polling response cannot resurrect the already confirmed notification.
 await f.nodes.find(n=>n.textContent==='刷新').onclick();await settle();assert.equal(bell.textContent,'🔔');
 assert.equal(f.calls.filter(c=>c.path==='/notifications/read').length,1);f.stop();
});
test('failed bell acknowledgement keeps the real unread count',async()=>{
 const f=fixture({rejectRead:true});await settle();f.nodes.find(n=>n.className==='wall-inbox-bell').onclick();await settle();
 assert.equal(f.nodes.find(n=>n.className==='wall-inbox-bell').textContent,'🔔 1');f.stop();
});
test('opening an empty bell makes no write',async()=>{
 const f=fixture({empty:true});await settle();f.nodes.find(n=>n.className==='wall-inbox-bell').onclick();await settle();
 assert.equal(f.calls.filter(c=>c.path==='/notifications/read').length,0);f.stop();
});
