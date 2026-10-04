import {useEffect,useRef,useState} from 'react';
import {getApiBase} from '../config';
import './SocialForward.css';

export type Forward = {reference:{origin:string;moment_id:string};status:'available'|'unavailable'|'link_only';url?:string;source?:{id:string;author:string;content:string;created_at:number}};

export function SocialForwardCard({forward,name,base}:{forward?:Forward;name:(id:string)=>string;base?:string}) {
  const [opened,setOpened]=useState(false),[current,setCurrent]=useState<{content:string;author:string;people?:Record<string,{name:string}>}|null>(null);
  const [reading,setReading]=useState(false),[error,setError]=useState('');
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{if(opened)dialog.current?.showModal();},[opened]);
  if(!forward)return null;
  const source=forward.source;
  // Only server-validated reference data; never use the containing post body as a source snapshot.
  const external=forward.status==='link_only';
  let href='',host='';
  try{const url=new URL(forward.reference.origin);if(external&&url.protocol==='https:'&&!url.username&&!url.password){host=url.hostname;href=forward.reference.origin+'/?moment='+encodeURIComponent(forward.reference.moment_id);}}catch{/* Old or invalid optional reference cannot break the feed. */}
  const read=async()=>{
    setOpened(true);setReading(true);setError('');setCurrent(null);
    try{
      const r=await fetch((base||getApiBase()+'/api/social-feed')+'/moments/'+encodeURIComponent(forward.reference.moment_id),{cache:'no-store',headers:{'X-MIRROW-Lounge-Admin':'1'},signal:AbortSignal.timeout(15000)});
      if(!r.ok)throw Error();const item=await r.json();
      if(item.visibility!=='public'||item.migrated)throw Error();setCurrent(item);
    }catch{setError('原动态当前无法读取；可能已私密、删除、迁移或来源暂不可用。');}
    finally{setReading(false);}
  };
  return <><aside className="social-forward-card" aria-label="转发来源">
    <small>↗ 转发来源{external?' · '+host:''}</small>
    {source&&forward.status==='available'?<><strong>{name(source.author)}</strong><p>{source.content}</p><button type="button" onClick={()=>void read()}>查看原动态</button></>:
      <p>{external?'原文保留在来源共域，需要在那里登录查看。':'原动态已私密、删除或迁移，当前无法查看。'}</p>}
    {href&&<a href={href} target="_blank" rel="noopener noreferrer">去来源共域查看 ↗</a>}
  </aside>{opened&&<dialog ref={dialog} className="social-forward-dialog" onCancel={e=>{e.preventDefault();setOpened(false);}}><header><h3>来源动态</h3><button type="button" onClick={()=>setOpened(false)}>×</button></header>{reading?<p>正在读取来源…</p>:error?<p role="alert">{error}</p>:current&&<><strong>{current.people?.[current.author]?.name||name(current.author)}</strong><p className="social-feed-content">{current.content}</p></>}</dialog>}</>;
}

export function SocialForwardDialog({sourceSiteId='',momentId,onClose,onDone}:{sourceSiteId?:string;momentId:string;onClose:()=>void;onDone?:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [sites,setSites]=useState<{id:string;name:string}[]>([]);
  const [destination,setDestination]=useState('');
  const [visibility,setVisibility]=useState<'private'|'public'>('private');
  const [content,setContent]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[done,setDone]=useState(false);
  const requestId=useRef({signature:'',id:''});
  const controller=useRef<AbortController|null>(null);
  const alive=useRef(true);
  useEffect(()=>{
    alive.current=true;dialog.current?.showModal();
    const abort=new AbortController();
    fetch(getApiBase()+'/api/social-sites',{headers:{'X-MIRROW-Lounge-Admin':'1'},signal:abort.signal,cache:'no-store'})
      .then(r=>r.ok?r.json():Promise.reject()).then(p=>{if(alive.current)setSites((p.sites||[]).filter((s:{enabled:boolean;has_human_key:boolean})=>s.enabled&&s.has_human_key));}).catch(()=>{});
    return()=>{alive.current=false;abort.abort();controller.current?.abort();};
  },[]);
  const submit=async()=>{
    if(busy||done)return;
    const payload={source_site_id:sourceSiteId,source_moment_id:momentId,site_id:destination,visibility:destination?'public':visibility,content:content.trim()};
    const signature=JSON.stringify(payload);
    if(requestId.current.signature!==signature)requestId.current={signature,id:crypto.randomUUID()};
    setBusy(true);setError('');
    const abort=new AbortController();controller.current=abort;
    const timer=window.setTimeout(()=>abort.abort(),20000);
    try {
      const r=await fetch(getApiBase()+'/api/social-feed/forward',{method:'POST',headers:{'Content-Type':'application/json','X-MIRROW-Lounge-Admin':'1'},body:JSON.stringify({...payload,request_id:requestId.current.id}),signal:abort.signal});
      if(!r.ok){const p=await r.json().catch(()=>({}));throw Error(p.detail==='forward_source_unavailable'?'原动态已不可转发，请重新查看来源。':p.detail==='remote_social_forwarding_unavailable'?'目标共域尚未安装转发更新。':'转发未确认，请到目标共域核对后再重试。');}
      if(alive.current){setDone(true);onDone?.();}
    }catch(e){if(alive.current)setError(e instanceof Error?e.message:'转发未确认');}
    finally{window.clearTimeout(timer);if(alive.current)setBusy(false);}
  };
  return <dialog ref={dialog} className="social-forward-dialog" onCancel={e=>{e.preventDefault();onClose();}} aria-labelledby="forward-title"><header><h3 id="forward-title">转发这条动态</h3><button type="button" onClick={onClose} aria-label="关闭转发">×</button></header>
    {done?<p role="status">已转发。来源正文仍由原站管理。</p>:<><label>转发到<select value={destination} disabled={busy} onChange={e=>setDestination(e.target.value)}><option value="">本家</option>{sites.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
    {!destination&&<label>可见范围<select value={visibility} disabled={busy} onChange={e=>setVisibility(e.target.value as 'private'|'public')}><option value="private">私密 · 仅本家</option><option value="public">公开 · 本家朋友</option></select></label>}
    <label>顺便说一句<textarea maxLength={1200} value={content} disabled={busy} onChange={e=>setContent(e.target.value)} placeholder="可留空；不会复制原帖内容"/></label>
    <p className="social-forward-hint">跨家转发只带来源入口，原站仍需登录；在别人家发布，由那家的主人管理。私密动态不支持转发。</p>
    {error&&<p role="alert">{error}</p>}<button className="social-forward-submit" type="button" disabled={busy} onClick={()=>void submit()}>{busy?'正在转发…':'确认转发'}</button></>}
  </dialog>;
}
