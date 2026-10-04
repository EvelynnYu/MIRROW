import {useState} from 'react';
import {getApiBase} from '../config';
import RemoteSocialAvatar from './RemoteSocialAvatar';
import SocialPersonCard, {type SocialPerson} from './SocialPersonCard';
import {SocialSourceLabel} from './SocialFeedFilters';
import {socialViewKey} from '../social/viewState';
import {useSocialView} from '../social/useSocialView';
import SocialMentions from './SocialMentions';
import {SocialForwardCard,SocialForwardDialog,type Forward} from './SocialForward';

type Person = {name?:string; nickname?:string; avatar?:string};
type Comment = {id:string;author:string;content:string;reply_to_id?:string|null;created_at:number;can_delete?:boolean;mention_actor_ids?:string[]};
export type TimelineMoment = {
  id:string;author:string;content:string;visibility:'public'|'private';created_at:number;
  migrated?:boolean; comments:Comment[];reactions:{author:string}[];people?:Record<string,Person>;
  viewer_actor:string;source:{site_id:string;site_name:string;origin:string;hosting_mode:string};mention_actor_ids?:string[];
  forward?:Forward;
};
export const timelineRef=(item:TimelineMoment)=>JSON.stringify([item.source.site_id,item.id]);

export default function SocialTimelineCard({item,onUpdated,onSwitch,readingOnly=false}:{
  item:TimelineMoment;onUpdated:(item:TimelineMoment)=>void;onSwitch:(site:string)=>void;readingOnly?:boolean;
}) {
  const [busy,setBusy]=useState('');
  const [error,setError]=useState('');
  const view=(field:string)=>socialViewKey(getApiBase(),'aning','timeline-card',item.viewer_actor,timelineRef(item),field);
  const [draft,setDraft]=useSocialView(view('draft'),'');
  const [mentions,setMentions]=useSocialView<string[]>(view('mentions'),[]);
  const [replyId,setReplyId]=useSocialView<string|null>(view('reply'),null);
  const reply=item.comments.find(row=>row.id===replyId)||null;
  const setReply=(row:Comment|null)=>setReplyId(row?.id||null);
  const [expanded,setExpanded]=useSocialView(view('expanded'),false);
  const [person,setPerson]=useState('');
  const [forwarding,setForwarding]=useState(false);
  const remote=Boolean(item.source.site_id);
  const base=remote?`${getApiBase()}/api/social-sites/${encodeURIComponent(item.source.site_id)}`:`${getApiBase()}/api/social-feed`;
  const target=`${base}/moments/${encodeURIComponent(item.id)}`;
  const own=item.viewer_actor;
  const inputId=`timeline-comment-${encodeURIComponent(timelineRef(item))}`;
  const name=(actor:string)=>item.people?.[actor]?.name||item.people?.[actor]?.nickname||(actor==='k'&&!remote?'AI':actor==='aning'&&!remote?'站主':'资料待同步');
  const time=(stamp:number)=>new Date(stamp*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  const request=async(url:string,init?:RequestInit)=>{
    const response=await fetch(url,{cache:'no-store',...init,headers:{'X-MIRROW-Lounge-Admin':'1',...init?.headers}});
    const result=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'操作结果未确认，请核对后再试');
    return result;
  };
  const act=async(kind:string,path:string,method='POST',payload?:object)=>{
    if(busy||readingOnly)return;
    setBusy(kind);setError('');
    try {
      await request(target+path,{method,headers:payload?{'Content-Type':'application/json'}:undefined,body:payload?JSON.stringify(payload):undefined,signal:AbortSignal.timeout(15000)});
      if(kind==='comment'){setDraft('');setMentions([]);setReply(null);setExpanded(true);}
      const fresh=await request(target,{signal:AbortSignal.timeout(15000)});
      onUpdated({...item,...fresh,source:item.source,viewer_actor:item.viewer_actor,people:fresh.people||item.people});
    } catch {setError('操作结果未确认，请重新进入这家核对；不会自动重试。');}
    finally {setBusy('');}
  };
  const avatar=(actor:string,small=false)=>remote?
    <RemoteSocialAvatar base={base} origin={item.source.origin} actor={actor} avatar={item.people?.[actor]?.avatar} name={name(actor)} className={small?'social-feed-comment-avatar':'social-feed-avatar'} onClick={()=>setPerson(actor)} />:
    <button type="button" className="social-avatar-trigger" onClick={()=>setPerson(actor)} aria-label={`查看${name(actor)}资料`}>
      {item.people?.[actor]?.avatar?<img className={`${small?'social-feed-comment-avatar':'social-feed-avatar'} social-feed-avatar-image`} alt={`${name(actor)}头像`} src={item.people[actor].avatar!.startsWith('/api/')?getApiBase()+item.people[actor].avatar:item.people[actor].avatar}/>:<span className={small?'social-feed-comment-avatar':'social-feed-avatar'}>{name(actor).slice(0,1)}</span>}
    </button>;
  const saved=(p:SocialPerson)=>onUpdated({...item,people:{...item.people,[p.actor_id]:p}});
  const rows=expanded?item.comments:item.comments.slice(-3);
  return <article className={`social-feed-card social-feed-card-${!remote&&item.author==='k'?'k':'friend'}`} data-source-site={item.source.site_id}>
    <header className="social-feed-card-head">{avatar(item.author)}<strong className="social-feed-author">{name(item.author)}</strong>
      {!remote&&['aning','k'].includes(item.author)?<select className="social-feed-visibility-control" aria-label="动态可见性" value={item.visibility} disabled={Boolean(busy)||readingOnly} onChange={e=>void act('visibility','/visibility','PATCH',{visibility:e.target.value})}><option value="private">🔒 私密</option><option value="public">公开</option></select>:<span className="social-feed-visibility-control">公开</span>}
      <time className="social-feed-time">{time(item.created_at)}</time></header>
    <div className="social-feed-content">{item.content}</div>{item.mention_actor_ids?.length?<div className="social-mention-chips">{item.mention_actor_ids.map(id=>item.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPerson(id)}>@{item.people[id].name}</button>)}</div>:null}
    <SocialForwardCard forward={item.forward} name={name} base={base}/>
    {!item.migrated&&!readingOnly&&<><div className="social-feed-actions">
      <button type="button" className={`social-feed-action ${item.reactions.some(r=>r.author===own)?'active':''}`} disabled={Boolean(busy)} onClick={()=>void act('like','/like')}>{busy==='like'?'处理中…':item.reactions.some(r=>r.author===own)?'♥ 已赞':'♡ 赞'}</button>
      <button type="button" className="social-feed-action" onClick={()=>{setExpanded(true);document.getElementById(inputId)?.focus();}}>◌ {item.comments.length||''} 评论</button>
      {item.visibility==='public'&&!item.forward&&<button type="button" className="social-feed-action social-feed-action-forward" disabled={Boolean(busy)} onClick={()=>setForwarding(true)}>↗ 转发</button>}
    </div>
    {item.reactions.length>0&&<div className="social-feed-liked-by">♥ {item.reactions.map(r=>name(r.author)).join('、')}</div>}
    {rows.length>0&&<div className="social-feed-comments">{rows.map(c=><div className="social-feed-comment" key={c.id}>{avatar(c.author,true)}<div className="social-feed-comment-body"><div className="social-feed-comment-meta"><strong>{name(c.author)}</strong>{c.reply_to_id&&<span>回复 {name(item.comments.find(p=>p.id===c.reply_to_id)?.author||'')}</span>}<time>{time(c.created_at)}</time></div><p className="social-feed-comment-text">{c.content}</p>{c.mention_actor_ids?.length?<div className="social-mention-chips">{c.mention_actor_ids.map(id=>item.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPerson(id)}>@{item.people[id].name}</button>)}</div>:null}<div className="social-feed-comment-tools"><button type="button" onClick={()=>{setReply(c);document.getElementById(inputId)?.focus();}}>回复</button>{(c.can_delete??(!remote||c.author===own))&&<button type="button" disabled={Boolean(busy)} onClick={()=>{if(window.confirm('删除这条评论？删除后无法恢复。'))void act('delete-comment',`/comments/${encodeURIComponent(c.id)}`,'DELETE');}}>删除</button>}</div></div></div>)}</div>}
    {item.comments.length>3&&<button type="button" className="social-comments-toggle" onClick={()=>setExpanded(v=>!v)}>{expanded?'收起评论':`展开全部 ${item.comments.length} 条评论`}</button>}
    {reply&&<div className="social-feed-replying">正在回复 {name(reply.author)}<button type="button" onClick={()=>setReply(null)}>取消 ×</button></div>}
    <div className="social-feed-comment-box"><SocialMentions compact endpoint={`${base}/mention-people${remote?'':'?visibility='+item.visibility}`} selected={mentions} onChange={setMentions} disabled={Boolean(busy)} onPerson={setPerson}/><input id={inputId} value={draft} maxLength={600} onChange={e=>setDraft(e.target.value)} placeholder={reply?`回复 ${name(reply.author)}`:'写下一句……'}/><button type="button" disabled={Boolean(busy)||!draft.trim()} onClick={()=>void act('comment','/comments','POST',{content:draft.trim(),reply_to_id:reply?.id||null,...(mentions.length?{mention_actor_ids:mentions}:{})})}>{busy==='comment'?'发送中…':'发送'}</button></div></>}
    <SocialSourceLabel name={item.source.site_name} hosted={item.source.hosting_mode==='hosted'} onClick={()=>onSwitch(item.source.site_id)}/>
    {error&&<p className="social-feed-error" role="alert">{error}</p>}
    {forwarding&&<SocialForwardDialog sourceSiteId={item.source.site_id} momentId={item.id} onClose={()=>setForwarding(false)}/>}
    {person&&<SocialPersonCard actorId={person} remote={remote?{base,origin:item.source.origin}:undefined} onClose={()=>setPerson('')} onSaved={saved}/>} 
  </article>;
}
