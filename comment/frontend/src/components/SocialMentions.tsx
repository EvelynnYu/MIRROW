import {useEffect, useMemo, useRef, useState} from 'react';
import './SocialMentions.css';

export type MentionPerson = {actor_id:string; name?:string; nickname?:string; avatar?:string};

/**
 * A server-backed mention picker.  The text field deliberately never parses
 * @names: the stable actor ids are the only notification contract.
 */
export default function SocialMentions({endpoint, selected, onChange, disabled=false, onPerson, compact=false}:{
  endpoint:string; selected:string[]; onChange:(ids:string[])=>void; disabled?:boolean; onPerson?:(actorId:string)=>void; compact?:boolean;
}) {
  const [open,setOpen]=useState(false), [query,setQuery]=useState(''), [items,setItems]=useState<MentionPerson[]>([]);
  const [supported,setSupported]=useState<boolean|null>(null), [loading,setLoading]=useState(false), [problem,setProblem]=useState('');
  const cache=useRef(new Map<string,MentionPerson>()), sequence=useRef(0);
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{if(open)dialog.current?.showModal();else dialog.current?.close();},[open]);
  const chosen=useMemo(()=>selected.map(id=>cache.current.get(id)).filter((row):row is MentionPerson=>Boolean(row)),[selected,items]);
  useEffect(()=>{ sequence.current++; setOpen(false); setQuery(''); setItems([]); setProblem(''); setSupported(null); cache.current.clear(); },[endpoint]);
  useEffect(()=>{
    if(!open)return;
    const current=++sequence.current, controller=new AbortController();
    const timer=window.setTimeout(async()=>{
      setLoading(true); setProblem('');
      try {
        const url=new URL(endpoint, window.location.href); if(query.trim())url.searchParams.set('q',query.trim());
        const response=await fetch(url.toString(),{cache:'no-store',signal:controller.signal});
        if(!response.ok)throw new Error(response.status===404?'unsupported':'unavailable');
        const payload=await response.json() as {items?:MentionPerson[];capability?:string};
        if(current!==sequence.current)return;
        if(payload.capability!=='mentions_v1')throw new Error('unsupported');
        const rows=(payload.items||[]).filter(row=>row.actor_id);
        rows.forEach(row=>cache.current.set(row.actor_id,row)); setItems(rows); setSupported(true);
      } catch(error) {
        if(current!==sequence.current||controller.signal.aborted)return;
        if(error instanceof Error&&error.message==='unsupported'){setSupported(false);setOpen(false);}
        else {setSupported(false);setProblem('艾特候选暂不可用；不影响正常发布。');}
      } finally {if(current===sequence.current)setLoading(false);}
    },180);
    return()=>{window.clearTimeout(timer);controller.abort();};
  },[endpoint,open,query]);
  const label=(person:MentionPerson)=>person.name?.trim()||person.nickname?.trim()||'成员';
  const toggle=(person:MentionPerson)=>{
    const exists=selected.includes(person.actor_id);
    if(!exists&&selected.length>=8){setProblem('一次最多艾特 8 位成员。');return;}
    cache.current.set(person.actor_id,person);
    onChange(exists?selected.filter(id=>id!==person.actor_id):[...selected,person.actor_id]);
  };
  const chips=<div className="social-mention-chips" aria-label="已艾特成员">{chosen.map(person=><span className="social-mention-chip" key={person.actor_id}><button type="button" disabled={!onPerson} onClick={()=>{setOpen(false);onPerson?.(person.actor_id);}}>@{label(person)}</button><button type="button" aria-label={`移除 ${label(person)}`} disabled={disabled} onClick={()=>onChange(selected.filter(id=>id!==person.actor_id))}>×</button></span>)}</div>;
  return <div className={`social-mentions${compact?' social-mentions-inline':''}`}>
    {supported===false&&problem&&<small className="social-mention-problem">{problem}</small>}
    {!compact&&chosen.length>0&&chips}
    {supported!==false&&<button type="button" className="social-mention-open" disabled={disabled} title={selected.length?`已选 ${selected.length} 位，点击修改`:'艾特成员'} aria-label={selected.length?`艾特成员，已选 ${selected.length} 位`:'艾特成员'} aria-haspopup="dialog" aria-expanded={open} onClick={()=>setOpen(true)}><span aria-hidden="true">@</span>{compact?(selected.length>0&&<small>{selected.length}</small>):<span>提到谁{selected.length?` · ${selected.length}`:''}</span>}</button>}
    <dialog ref={dialog} className="social-mention-picker" aria-label="选择艾特成员" onCancel={event=>{event.preventDefault();setOpen(false);}}>
      <header><div><h3>提到谁</h3><small>最多 8 位 · 发布后才会通知对方</small></div><button type="button" aria-label="关闭艾特选择" onClick={()=>setOpen(false)}>×</button></header>
      {chosen.length>0&&chips}
      <input value={query} maxLength={40} onChange={event=>setQuery(event.target.value)} placeholder="搜索可艾特的成员" aria-label="搜索可艾特的成员" />
        {loading&&<small>正在查找…</small>}{problem&&<small className="social-mention-problem">{problem}</small>}
      <div className="social-mention-options">{!loading&&!problem&&items.map(person=><button type="button" className="social-mention-option" key={person.actor_id} aria-pressed={selected.includes(person.actor_id)} disabled={disabled} onClick={()=>toggle(person)}><span>{selected.includes(person.actor_id)?'✓':'+'}</span>@{label(person)}</button>)}</div>
      {!loading&&!problem&&!items.length&&<small>没有可艾特的成员</small>}
      <footer><span>已选 {selected.length} 位</span><button type="button" onClick={()=>setOpen(false)}>完成</button></footer>
    </dialog>
  </div>;
}
