/* Test actual public-wall initialization without a browser/DB/credentials. */
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname,'../public_wall.js'),'utf8');
const readySource = source.slice(source.indexOf('async function ready('),source.indexOf('identitySelector.onchange ='));
async function check(failure) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {textContent:'', hidden:false, previousElementSibling:{},
      classList:{values:new Set(),add(v){this.values.add(v)},remove(v){this.values.delete(v)},toggle(v,on){on?this.add(v):this.remove(v)}},
      replaceChildren(){}});
    return nodes.get(id);
  }
  let message='';
  const context = { $:node, actor:'', busy:false, stopProfiles:null, stopDecor:null,
    readyEpoch:0,feedEpoch:0,wallState:{clear(){}},canForward:false,
    publishMentions:{clear(){}},URLSearchParams,location:{search:''},
    loadMentionNotices:async()=>{},
    canInteract:true, canManageAvatar:false, identityChoices:[], managedProfiles:[], profileSubject:'',
    identitySelector:node('selector'), identityLabel:node('label'), profileLinkSlot:node('slot'),
    showHeaderIdentity(){}, updateProfileEditor(){}, Option:function(name,id){this.name=name;this.id=id},
    status(value){message=value},
    async api(route) {
      if (failure === 'auth' && route === '/me') throw Object.assign(new Error('unauthorized'),{status:401});
      if (route === '/me') return {actor:{actor_id:'visitor:fixture'},capabilities:['post'],can_manage_avatar:true};
      if (failure === 'identity' && route === '/me/identities') throw Object.assign(new Error('not found'),{status:404});
      return {};
    },
    async load(){if(failure==='feed')throw Object.assign(new Error('unavailable'),{status:502})},
    window:{MirrowDecor:{async mount(){if(failure==='decor')throw new Error('decor failure');return ()=>{}}}},
  };
  vm.createContext(context); vm.runInContext(readySource,context);
  const success = await context.ready();
  if (failure === 'auth') {
    assert.equal(success,false); assert.equal(node('login-panel').classList.values.has('hidden'),false);
    assert.equal(node('wall-panel').classList.values.has('hidden'),true);
  } else {
    assert.equal(success,true); assert.equal(node('login-panel').classList.values.has('hidden'),true);
    assert.equal(node('wall-panel').classList.values.has('hidden'),false);
    if(failure)assert.match(message,/部分模块未加载/);
  }
}
(async()=>{for(const failure of ['', 'identity','feed','decor','auth'])await check(failure);process.stdout.write('PASS: 5 public initialization/auth isolation cases\n')})().catch(error=>{process.stderr.write(String(error));process.exitCode=1});
