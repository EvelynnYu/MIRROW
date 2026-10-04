import React from 'react';
import {createRoot} from 'react-dom/client';
import SocialTimelinePage from '../src/pages/SocialTimelinePage';
import SocialFeedPage from '../src/pages/SocialFeedPage';
import SocialRemoteFeed from '../src/pages/SocialRemoteFeed';
import type {TimelineMoment} from '../src/components/SocialTimelineCard';
import decorScript from '../../backend/social_feed/decor_ui.js?raw';
import decorStyle from '../../backend/social_feed/decor_ui.css?raw';
import musicEditorScript from '../../backend/social_feed/decor_music_ui.js?raw';
import publicWallHTML from '../../backend/social_feed/public_wall.html?raw';
import '../src/desktop.css';

// Exercise the real shared runtime without loading a script/style from the
// production backend. Both resources and all domain requests stay isolated.
const append = document.head.append.bind(document.head);
document.head.append = (...nodes) => {
  for (const element of nodes) {
    if (element instanceof HTMLScriptElement && element.src.endsWith('/api/social-decor-ui/script')) {
      element.removeAttribute('src'); element.textContent = decorScript; append(element);
      element.dispatchEvent(new Event('load'));
    } else if (element instanceof HTMLScriptElement && (element.src.endsWith('/api/social-decor-ui/music-editor')||element.src.endsWith('/decor/music-editor.js'))) {
      element.removeAttribute('src');element.textContent=musicEditorScript;append(element);element.dispatchEvent(new Event('load'));
    } else if (element instanceof HTMLLinkElement && element.href.endsWith('/api/social-decor-ui/style')) {
      const style=document.createElement('style'); style.textContent=decorStyle; append(style);
    } else append(element);
  }
};
const audioMode = ['audio','blocked'].includes(new URLSearchParams(location.search).get('music')||'');
const blockedMode = new URLSearchParams(location.search).get('music') === 'blocked';
const importFailure = new URLSearchParams(location.search).get('import') === 'preview';
const publicSurface = new URLSearchParams(location.search).get('surface') === 'public';
const progressive = new URLSearchParams(location.search).get('progress') === 'slow';
let released=false;
const held=new Set<()=>void>();
let decorHome={exhibits:[] as any[],theme:{preset:'ink',font:'serif',text_size:'normal',background:'',card_background:'',accent:'#aa66bb',shelf_name:'隔离奇物架',music:audioMode?'fixture-audio':'',music_title:'验收音乐',music_track:{id:'1',title:'Demo Track'}}};
const fixtureWav=new ArrayBuffer(16044),wavView=new DataView(fixtureWav);
for(const [offset,text] of [[0,'RIFF'],[8,'WAVE'],[12,'fmt '],[36,'data']] as const)for(let i=0;i<text.length;i++)wavView.setUint8(offset+i,text.charCodeAt(i));
for(const [offset,value] of [[4,16036],[16,16],[24,8000],[28,16000],[40,16000]])wavView.setUint32(offset,value,true);
for(const [offset,value] of [[20,1],[22,1],[32,2],[34,16]])wavView.setUint16(offset,value,true);
decorHome.exhibits=[{id:'display',name:'一片蓝羽',description:'隔离验收用展品。',image:'fixture-exhibit',gift_image:'',human_note:'',ai_note:''}];
const options=new URLSearchParams(location.search);
if(options.get('empty')==='1')decorHome.exhibits=[];
if(options.get('font'))decorHome.theme.font=options.get('font')!;
if(options.get('size'))decorHome.theme.text_size=options.get('size')!;
if(options.get('longMusic')==='1')decorHome.theme.music_title='Demo Track — a very long music title for responsive layout';
let playing=false,plays=0,pauses=0;
Object.defineProperty(HTMLMediaElement.prototype,'paused',{get:()=>!playing,configurable:true});
HTMLMediaElement.prototype.play=async function(){plays++;if(blockedMode&&plays===1)throw new DOMException('Fixture autoplay denial','NotAllowedError');playing=true;this.dispatchEvent(new Event('play'));};
HTMLMediaElement.prototype.pause=function(){playing=false;pauses++;this.dispatchEvent(new Event('pause'));};

// Every request is intercepted in memory: no credentials, real posts or writes.
const sites=[{id:'fixture',name:'演示家',origin:'https://home.example.test',enabled:true,has_human_key:true,has_ai_key:true}];
const items:TimelineMoment[]=[
  {id:'same-id',author:'k',content:'演示私密动态：仅本家可见。',visibility:'private',created_at:1790900040,comments:[],reactions:[{author:'aning'}],people:{k:{name:'AI · 本家机'},aning:{name:'站主'}},viewer_actor:'aning',source:{site_id:'',site_name:'本家',origin:'',hosting_mode:'home'}},
  {id:'same-id',author:'visitor:friend',content:'演示公开动态：可用于测试点赞和评论。',visibility:'public',created_at:1790900000,comments:[{id:'own-comment',author:'visitor:self',content:'干得漂亮！',created_at:1790900020},{id:'friend-comment',author:'visitor:friend',content:'谢谢，来坐坐。',created_at:1790900030}],reactions:[{author:'visitor:self'}],people:{'visitor:friend':{name:'演示朋友'},'visitor:self':{name:'站主'}},viewer_actor:'visitor:self',source:{site_id:'fixture',site_name:'演示家',origin:sites[0].origin,hosting_mode:'hosted'}}
];
if(progressive)for(let i=0;i<15;i++)items.push({...items[i%2],id:'extra-'+i,content:`第 ${i+1} 条隔离阅读位置验收动态。`,created_at:1790899900-i*20,comments:[],reactions:[]});
(window as any).__timelineCalls=[];
window.fetch=async(input,init)=>{
  const url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url,location.href);
  const path=url.pathname,method=init?.method||'GET';
  (window as any).__timelineCalls.push({path,method});
  if(path==='/api/social-sites')return Response.json({sites});
  if(path.endsWith('/mention-people'))return Response.json({capability:'mentions_v1',items:[{actor_id:'k',name:'AI · 本家机'},{actor_id:'aning',name:'站主'}]});
  if(path.endsWith('/timeline/stream')&&progressive){
    const rows=items.filter(r=>url.searchParams.get('visibility_filter')==='all'||r.visibility===url.searchParams.get('visibility_filter'));
    const encoder=new TextEncoder();let closed=false;
    let finish:()=>void;
    const body=new ReadableStream({start(controller){
      const send=(event:object)=>{if(!closed)controller.enqueue(encoder.encode(JSON.stringify(event)+'\n'));};
      send({type:'begin',total_sources:2,omitted_sources:0});
      send({type:'source',site_id:'',items:rows.filter(r=>!r.source.site_id),finished_sources:1,total_sources:2});
      finish=()=>{if(closed)return;send({type:'source',site_id:'fixture',items:rows.filter(r=>r.source.site_id),finished_sources:2,total_sources:2});
        send({type:'complete',page:{items:rows,has_more:false,next_cursor:null,unavailable:[],omitted_sources:0}});closed=true;controller.close();held.delete(finish);};
      if(released)finish();else held.add(finish);
      init?.signal?.addEventListener('abort',()=>{closed=true;held.delete(finish);try{controller.close();}catch{}},{once:true});
    },cancel(){closed=true;held.delete(finish);}});
    return new Response(body,{headers:{'Content-Type':'application/x-ndjson'}});
  }
  if(path.endsWith('/me'))return Response.json({actor:{actor_id:'visitor:self'},can_manage_avatar:true});
  if(path.endsWith('/me/ai')||path.endsWith('/migrations/pending')||path.endsWith('/migrations/capability')){
    if(progressive&&!released)await new Promise<void>(resolve=>held.add(resolve));
    return Response.json({actor:null,items:[],enabled:false});
  }
  if(path.endsWith('/moments')&&method==='GET')return Response.json({items:items.filter(r=>Boolean(r.source.site_id)===path.includes('/social-sites/fixture/')),has_more:false,next_cursor:null});
  if(path.endsWith('/notifications'))return Response.json({items:[]});
  if(path.endsWith('/state'))return Response.json({owner:true,human:true,actor:'aning',profiles:[],sites:[],gifts:[],home:decorHome});
  if(path.endsWith('/presets'))return Response.json({themes:[{id:'ink',name:'深墨',accent:'#aa66bb'}],frames:[]});
  if(path.endsWith('/home')&&method==='PUT'){decorHome=JSON.parse(String(init?.body));return Response.json(decorHome);}
  if(path.endsWith('/music-import'))return importFailure
    ? Response.json({detail:'music_import_preview_only'},{status:400})
    : Response.json({id:'fixture-imported',mime:'audio/mpeg',size:123,track:{id:'1',title:'Demo Track',artist:'隔离测试'},duration_seconds:182});
  if(path.endsWith('/visit')||path.endsWith('/human-gift-notices'))return Response.json({gifts:[],items:[]});
  if(path.endsWith('/people'))return Response.json({people:{}});
  if(path.endsWith('/media/fixture-exhibit'))return new Response(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><path fill="#6eaed2" d="M28 110Q0 8 103 5Q120 95 28 110Z"/><path stroke="#d6efff" stroke-width="3" d="M18 115L95 16"/></svg>'],{type:'image/svg+xml'}));
  if(path.endsWith('/media/fixture-audio')||path.endsWith('/media/fixture-imported'))return new Response(new Blob([fixtureWav],{type:'audio/wav'}));
  if(path.endsWith('/timeline'))return Response.json({items:items.filter(r=>url.searchParams.get('visibility_filter')==='all'||r.visibility===url.searchParams.get('visibility_filter')),has_more:false,next_cursor:null,unavailable:[],omitted_sources:0});
  if(path.includes('/people/'))return Response.json({person:{actor_id:path.split('/people/')[1],name:'站主',nickname:'站主',avatar:''}});
  const remote=path.includes('/social-sites/fixture/');
  const item=items[remote?1:0];
  if(method==='POST'&&path.endsWith('/like')){item.reactions=item.reactions.length?[]:[{author:item.viewer_actor}];return Response.json({active:Boolean(item.reactions.length)});}
  if(method==='POST'&&path.endsWith('/comments')){const body=JSON.parse(String(init?.body));item.comments.push({id:'new-comment',author:item.viewer_actor,content:body.content,created_at:1790900050,reply_to_id:body.reply_to_id});return Response.json(item.comments.at(-1));}
  if(path.endsWith('/same-id'))return Response.json(item);
  return Response.json({detail:'隔离验收没有该模块'},{status:404});
};
function report(){
  const calls=(window as any).__timelineCalls as {path:string;method:string}[];
  document.getElementById('proof')!.textContent=JSON.stringify({theme:document.querySelector<HTMLElement>('.social-feed-page')?.dataset.mdTheme,plays,pauses,visits:calls.filter(r=>r.path.endsWith('/social-decor/visit')).length,remoteVisits:calls.filter(r=>r.path.includes('/social-sites/')&&r.path.endsWith('/visit')).length,imports:calls.filter(r=>r.path.endsWith('/music-import')).length,homeSaves:calls.filter(r=>r.path.endsWith('/social-decor/home')&&r.method==='PUT').length});
}
const proof=document.createElement('output');proof.id='proof';
const reportButton=document.createElement('button');reportButton.textContent='检查装饰状态';reportButton.onclick=report;
if(progressive){const release=document.createElement('button');release.textContent='让慢共域返回';release.onclick=()=>{released=true;for(const fn of [...held])fn();held.clear();};document.body.append(release);}
document.body.append(reportButton,proof);
function ReadingHarness(){const [open,setOpen]=React.useState(true);return <div className="app" data-platform="pc">{open?<SocialFeedPage onBack={()=>setOpen(false)}/>:<button type="button" onClick={()=>setOpen(true)}>重新进入共域</button>}</div>;}
function LayoutHarness(){const [site,setSite]=React.useState(options.get('surface')==='remote'?'fixture':'all');return <div className="app" data-platform="pc">{site==='fixture'?<SocialRemoteFeed site={sites[0]} sites={sites} onSwitch={id=>setSite(id||'home')} onBack={()=>setSite('all')}/>:site==='home'?<SocialFeedPage onBack={()=>setSite('all')}/>:<SocialTimelinePage sites={sites} onSwitch={id=>setSite(id||'home')} onBack={()=>{}}/>}</div>;}
if(publicSurface){
  const doc=new DOMParser().parseFromString(publicWallHTML,'text/html'),style=document.createElement('style');style.textContent=doc.querySelector('style')!.textContent;append(style);
  const header=doc.querySelector('header')!;header.querySelector('.header-brand h1')!.textContent='MIRROW·Comment 共域';
  const main=document.createElement('main'),panel=document.createElement('div');main.append(panel);document.body.replaceChildren(header,main,reportButton,proof);
  const script=document.createElement('script');script.textContent=decorScript;append(script);
  void (window as any).MirrowDecor.mount(panel,{api:'/social/v1/decor',root:document.body});
}else createRoot(document.getElementById('root')!).render(<React.StrictMode>{progressive?<ReadingHarness/>:<LayoutHarness/>}</React.StrictMode>);
