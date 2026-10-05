// Actual public picker + in-memory DOM/transport. No browser session or real wall.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test');
const source=fs.readFileSync(__dirname+'/../public_wall.js','utf8');
const picker=source.slice(source.indexOf('function mountMentions('),source.indexOf('const publishMentionSlot'));
async function scenario(firstStatus){
  const nodes=[];let requests=0;
  const el=tag=>{
    const node={tag,childNodes:[],hidden:false,isConnected:true,open:false,value:'',textContent:'',
      classList:{add(){}},setAttribute(){},addEventListener(){},focus(){},
      replaceChildren(...children){this.childNodes=children},append(...children){this.childNodes.push(...children)},
      showModal(){this.open=true},close(){this.open=false},remove(){this.open=false;this.isConnected=false}};
    nodes.push(node);return node;
  };
  const context={actor:'visitor:fixture',URLSearchParams,Response,el,
    button:(label,click)=>Object.assign(el('button'),{textContent:label,click}),
    mentionText:person=>person?.name||'',openPerson(){},status(){},document:{body:el('body')},
    fetch:async()=>{requests++;return Response.json(requests===1&&firstStatus!==200?{}:{capability:'mentions_v1',items:[]},
      {status:requests===1?firstStatus:200});}};
  vm.createContext(context);vm.runInContext(picker,context);
  const slot=el('slot');context.mountMentions(slot);
  const toggle=slot.childNodes[1];toggle.click();await new Promise(setImmediate);
  const firstHidden=toggle.hidden;
  for(const node of nodes)if(node.tag==='dialog')node.close();
  toggle.click();await new Promise(setImmediate);
  return {firstHidden,requests};
}
test('temporary public candidate failure can be reopened and retried',async()=>{
  const result=await scenario(503);
  assert.equal(result.firstHidden,false);
  assert.equal(result.requests,2);
});
test('old public host without mention capability hides unsupported entry',async()=>{
  const result=await scenario(404);
  assert.equal(result.firstHidden,true);
  assert.equal(result.requests,1);
});
