import React from 'react';
import {createRoot} from 'react-dom/client';
import SocialRemoteFeed from '../src/pages/SocialRemoteFeed';

// In-memory transport only: no Keys, production DB, real network or friend posts.
const image = new Blob([Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/f1sAAAAASUVORK5CYII='), c => c.charCodeAt(0))], {type:'image/png'});
const avatar = `/social/v1/avatars/${'a'.repeat(64)}.png`;
const people = {
  'visitor:human':{actor_id:'visitor:human',name:'演示人',nickname:'演示人',avatar,remark:''},
  'visitor:friend':{actor_id:'visitor:friend',name:'远方朋友',nickname:'远方朋友',avatar,remark:''},
};
const moment = {id:'fixture-post',author:'visitor:friend',content:'这是一条从别家读取的演示动态。',created_at:1790900000,
  people:{'visitor:friend':people['visitor:friend']},
  reactions:[{author:'visitor:human'}],comments:[
    {id:'fixture-own',author:'visitor:human',content:'评论也有头像、时间与删除入口。',created_at:1790900020},
    {id:'fixture-other',author:'visitor:friend',content:'点头像可以看身份卡。',reply_to_id:'fixture-own',created_at:1790900050}]};
window.fetch = async (input,init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
  const path = url.pathname;
  if (path.endsWith('/me')) return Response.json({actor:people['visitor:human']});
  if (path.endsWith('/me/ai')) return Response.json({actor:null});
  if (path.endsWith('/migrations/pending')) return Response.json({items:[]});
  if (path.endsWith('/migrations/capability')) return Response.json({enabled:false});
  if (path.endsWith('/moments')) return Response.json({items:[moment],has_more:false,next_cursor:null});
  if (path.includes('/avatars/')) return new Response(image);
  if (path.includes('/people/')) {
    const actor = decodeURIComponent(path.split('/people/')[1].replace('/remark','')) as keyof typeof people;
    const person = people[actor];
    if (init?.method === 'PUT') {
      person.remark = JSON.parse(String(init.body)).remark;
      person.name = person.nickname + (person.remark ? `（${person.remark}）` : '');
    }
    return Response.json({person});
  }
  if (path.endsWith('/decor/state')) return Response.json({actor:'visitor:human',owner:false,human:false,profiles:[],people:{},
    home:{theme:{preset:'paper',font:'inherit',text_size:'normal',accent:'#9a6756',shelf_name:'演示家的奇物架',music:''},exhibits:[]}});
  if (path.endsWith('/decor/visit')) return Response.json({gifts:[],local_receipts:true});
  if (path.endsWith('/fixture-post')) return Response.json(moment);
  return Response.json({detail:'隔离验收未实现该模块'}, {status:404});
};
const site = {id:'fixture',name:'演示家',origin:'https://home.example.test',has_human_key:true,has_ai_key:false,enabled:true};
createRoot(document.getElementById('root')!).render(<React.StrictMode><SocialRemoteFeed site={site} sites={[site]} onSwitch={() => {}} onBack={() => {}} /></React.StrictMode>);
