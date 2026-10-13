import {lazy,Suspense,useCallback,useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {acknowledgeInbox,inboxReference} from '../social/acknowledgeInbox';
import {getApiBase} from '../config';
import type {TimelineMoment} from './SocialTimelineCard';
import './SocialInbox.css';
import {socialViewKey,writeView} from '../social/viewState';
import {ALL_SOCIAL_SITES} from './SocialFeedFilters';
import {invalidateSocialPrewarm,prewarmSocial} from '../social/prewarm';
const Card=lazy(()=>import('./SocialTimelineCard'));
type Notice={id:string;actor:string;actor_name?:string;kind:string;moment_id:string;comment_id?:string;
  created_at:number;site_id:string;site_name:string;preview:string;priority:string};
type Gift={id:string;machine_name:string;received:number;snapshot:{name:string;description?:string;image?:string;gift_image?:string;human_note?:string;ai_note?:string}};
type Head={site_id:string;site_name:string;id:string;created_at:number};
type Inbox={items:Notice[];heads:Head[];unavailable:{site_id:string;site_name:string;status:string}[]};
const labels:Record<string,string>={moment:'发了新动态',comment:'留下了评论/回复',like:'点了赞',mention:'艾特了你'};
export default function SocialInbox({onOpenFeed}:{onOpenFeed:()=>void}) {
  const api=getApiBase(),endpoint=`${api}/api/social-sites/inbox`;
  const [data,setData]=useState<Inbox>({items:[],heads:[],unavailable:[]}),[gifts,setGifts]=useState<Gift[]>([]);
  const [open,setOpen]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState('');
  const [detail,setDetail]=useState<TimelineMoment|null>(null),[gift,setGift]=useState<Gift|null>(null),[focus,setFocus]=useState('');
  const [newHomes,setNewHomes]=useState<string[]>([]),[toast,setToast]=useState('');
  const [giftImage,setGiftImage]=useState('');
  const alive=useRef(false),inflight=useRef(false),heads=useRef(new Map<string,number>()),known=useRef(new Set<string>());
  const initialized=useRef(false),dialog=useRef<HTMLDialogElement>(null),abort=useRef<AbortController|null>(null);
  const knownGifts=useRef(new Set<string>());
  const giftsInitialized=useRef(false);
  const readInFlight=useRef(new Set<string>()),giftInFlight=useRef(new Set<string>()),confirmed=useRef(new Set<string>());
  const [listed,setListed]=useState<Notice[]>([]),[listedGifts,setListedGifts]=useState<Gift[]>([]);
  const request=useCallback(async(path:string,body?:object,signal?:AbortSignal)=>{
    const r=await fetch(path,{cache:'no-store',signal,method:body?'POST':'GET',headers:{'X-MIRROW-Lounge-Admin':'1',...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
    const p=await r.json().catch(()=>({}));if(!r.ok)throw new Error('这条共域通知暂时无法读取，请稍后重试');return p;
  },[]);
  const reload=useCallback(async()=>{
    if(inflight.current&&!abort.current?.signal.aborted)return;inflight.current=true;setLoading(true);
    const controller=new AbortController();abort.current=controller;
    let newInteractions=0,newGifts=0;
    const notify=()=>{if(newInteractions||newGifts)setToast(`共域有 ${newInteractions} 条新互动${newGifts?` · 小机收到 ${newGifts} 份礼物`:''}`);};
    setError('');
    try {
      const results=await Promise.allSettled([
      request(endpoint,undefined,controller.signal).then(p=>{
      if(!alive.current||controller.signal.aborted)return;
      const next=p as Inbox;
      next.items=next.items.filter(n=>!confirmed.current.has(inboxReference(n)));
      const fresh=next.items.filter(n=>!known.current.has(JSON.stringify([n.site_id,n.id]))&&n.priority==='normal');
      next.items.forEach(n=>known.current.add(JSON.stringify([n.site_id,n.id])));
      while(known.current.size>2000)known.current.delete(known.current.values().next().value!);
      if(initialized.current)newInteractions=fresh.length;notify();
      const changed=next.heads.filter(h=>heads.current.has(h.site_id)&&h.created_at>(heads.current.get(h.site_id)||0)).map(h=>h.site_name);
      setNewHomes(old=>[...new Set([...old,...changed])]);
      next.heads.forEach(h=>heads.current.set(h.site_id,h.created_at));initialized.current=true;
      setData(next);
      }),
      request(`${api}/api/social-decor/human-gift-notices`,undefined,controller.signal).then(g=>{
        if(!alive.current||controller.signal.aborted)return;
        const rows=(g.items||[]) as Gift[];
        if(giftsInitialized.current)newGifts=rows.filter(r=>!knownGifts.current.has(r.id)).length;
        rows.forEach(r=>knownGifts.current.add(r.id));
        while(knownGifts.current.size>1000)knownGifts.current.delete(knownGifts.current.values().next().value!);
        giftsInitialized.current=true;setGifts(rows);notify();
      })]);
      if(alive.current&&!controller.signal.aborted&&results.some(r=>r.status==='rejected'))setError('部分共域通知暂时不可用；原未读保留。');
    }
    finally {if(abort.current===controller){inflight.current=false;if(alive.current)setLoading(false);}}
  },[api,endpoint,request]);
  useEffect(()=>{
    const warm=()=>{if(document.visibilityState==='visible')void prewarmSocial(api);};
    const refresh=()=>{invalidateSocialPrewarm(api);warm();};
    // Let the chat's startup rendering finish; all independent reads then overlap.
    const timer=window.setTimeout(warm,1200);
    document.addEventListener('visibilitychange',warm);window.addEventListener('social-inbox-refresh',refresh);
    return()=>{window.clearTimeout(timer);document.removeEventListener('visibilitychange',warm);window.removeEventListener('social-inbox-refresh',refresh);};
  },[api]);
  useEffect(()=>{
    alive.current=true;void reload();
    const tick=()=>{if(document.visibilityState==='visible')void reload();};
    const timer=window.setInterval(tick,60000);document.addEventListener('visibilitychange',tick);window.addEventListener('social-inbox-refresh',tick);
    return()=>{alive.current=false;abort.current?.abort();window.clearInterval(timer);document.removeEventListener('visibilitychange',tick);window.removeEventListener('social-inbox-refresh',tick);};
  },[reload]);
  useEffect(()=>{if(open)dialog.current?.showModal();else dialog.current?.close();},[open]);
  useEffect(()=>{if(!toast)return;const t=window.setTimeout(()=>setToast(''),6000);return()=>window.clearTimeout(t);},[toast]);
  useEffect(()=>{
    if(!detail||!focus)return;
    const node=dialog.current?.querySelector<HTMLElement>(`[data-comment-id="${CSS.escape(focus)}"]`);
    node?.scrollIntoView({block:'center'});
  },[detail,focus]);
  useEffect(()=>{
    let url='',disposed=false;setGiftImage('');const asset=gift?.snapshot.gift_image||gift?.snapshot.image;
    if(asset&&/^[a-f0-9]{64}\.(png|gif)$/.test(asset))void fetch(`${api}/api/social-decor/media/${asset}`,{headers:{'X-MIRROW-Lounge-Admin':'1'}}).then(r=>r.ok?r.blob():Promise.reject()).then(b=>{url=URL.createObjectURL(b);if(disposed)URL.revokeObjectURL(url);else setGiftImage(url);}).catch(()=>{});
    return()=>{disposed=true;if(url)URL.revokeObjectURL(url);};
  },[gift,api]);
  const acknowledge=async(rows:Notice[])=>{
    const ok=await acknowledgeInbox(rows,(site_id,notification_ids)=>request(`${endpoint}/read`,{site_id,notification_ids}),batch=>{
      if(!alive.current)return;
      const refs=new Set(batch.map(inboxReference));refs.forEach(id=>confirmed.current.add(id));
      while(confirmed.current.size>2000)confirmed.current.delete(confirmed.current.values().next().value!);
      setData(old=>({...old,items:old.items.filter(n=>!refs.has(inboxReference(n)))}));
    });
    if(!ok)throw Error('部分来源未确认');
  };
  useEffect(()=>{if(open)setNewHomes([]);},[open,data.heads]);
  useEffect(()=>{
    if(!open)return;
    if(data.items.length)setListed(old=>[...new Map([...old,...data.items].map(n=>[inboxReference(n),n])).values()]);
    const rows=data.items.filter(n=>!readInFlight.current.has(inboxReference(n)));
    if(!rows.length)return;
    const refs=rows.map(inboxReference);refs.forEach(id=>readInFlight.current.add(id));
    void acknowledge(rows).catch(()=>{if(alive.current)setError('部分来源暂时无法确认已读；已确认的提醒已消除，可刷新重试。');})
      .finally(()=>refs.forEach(id=>readInFlight.current.delete(id)));
  },[open,data.items]);
  useEffect(()=>{
    if(!open||!gifts.length)return;
    setListedGifts(old=>[...new Map([...old,...gifts].map(g=>[g.id,g])).values()]);
    const ids=gifts.filter(g=>!giftInFlight.current.has(g.id)).map(g=>g.id);if(!ids.length)return;
    ids.forEach(id=>giftInFlight.current.add(id));
    void request(`${api}/api/social-decor/human-gift-notices/seen`,{receipt_ids:ids}).then(()=>{
      if(alive.current)setGifts(old=>old.filter(g=>!ids.includes(g.id)));
    }).catch(()=>{if(alive.current)setError('赠礼提醒已阅确认暂时失败；礼物和收藏不变。');}).finally(()=>ids.forEach(id=>giftInFlight.current.delete(id)));
  },[open,gifts,api,request]);
  const inspect=async(row:Notice)=>{
    setError('');setLoading(true);
    try {
      const q=new URLSearchParams({moment_id:row.moment_id,site_id:row.site_id});
      const item=await request(`${endpoint}/moment?${q}`);if(!alive.current||!dialog.current?.open)return;
      setGift(null);setDetail(item);setFocus(row.comment_id||'');
    } catch {setError('内容或已读确认暂不可用；未确认的通知会保留。');}
    finally {if(alive.current)setLoading(false);}
  };
  const seeGift=async(item:Gift)=>{
    setGift(item);setDetail(null);setError('');
  };
  const count=data.items.length+gifts.length;
  const openFeed=(siteId=ALL_SOCIAL_SITES)=>{
    writeView(socialViewKey(api,'aning','home','site'),siteId);
    window.dispatchEvent(new CustomEvent('social-feed-open',{detail:{siteId}}));
    onOpenFeed();
  };
  const groups=new Map<string,Notice[]>();
  for(const row of listed){const key=JSON.stringify([row.site_id,row.kind==='like'?row.moment_id:row.id,row.kind]);groups.set(key,[...(groups.get(key)||[]),row]);}
  const openBell=()=>{setListed(data.items);setListedGifts(gifts);setDetail(null);setGift(null);setNewHomes([]);setToast('');setOpen(true);void reload();};
  return <><button type="button" className="social-inbox-bell" title="共域通知" aria-label={`共域通知，${count} 条未读`} onClick={openBell}><svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg>{count>0&&<small>{count>99?'99+':count}</small>}{!count&&newHomes.length>0&&<i/>}</button>
    {createPortal(<>{toast&&<button className="social-inbox-toast" role="status" onClick={openBell}>{toast}</button>}
    <dialog ref={dialog} className="social-inbox-dialog" onCancel={e=>{e.preventDefault();setOpen(false);}}>
      <header><div><h2>{gift?'你家小机收到了一份礼物':detail?'共域动态':'共域通知'}</h2><small>打开即标记当前列表已读 · 不影响 AI 的未读</small></div><button type="button" aria-label="关闭通知" onClick={()=>{setOpen(false);setDetail(null);setGift(null);}}>×</button></header>
      {(detail||gift)&&<button className="social-inbox-back" onClick={()=>{setDetail(null);setGift(null);}}>‹ 返回通知</button>}
      {error&&<p role="alert">{error}</p>}
      {detail?<Suspense fallback={<p>正在展示原动态…</p>}><Card item={detail} detail onUpdated={setDetail} onSwitch={siteId=>{setOpen(false);openFeed(siteId);}} onPresented={()=>{if(focus)dialog.current?.querySelector<HTMLElement>(`[data-comment-id="${CSS.escape(focus)}"]`)?.scrollIntoView({block:'center'});}}/></Suspense>:gift?<article className="social-inbox-gift">{giftImage&&<img src={giftImage} alt={gift.snapshot.name}/>}<h3>{gift.machine_name} · {gift.snapshot.name}</h3><p>{gift.snapshot.description}</p><p>{gift.snapshot.human_note}</p><p>{gift.snapshot.ai_note}</p><small>已收进小机的收藏；这里仅记录你的查看。</small></article>:<>
        <div className="social-inbox-tools"><button onClick={()=>void reload()} disabled={loading}>{loading?'正在读取…':'刷新'}</button><button disabled={!count||loading} onClick={()=>{setLoading(true);void (async()=>{try{await acknowledge(data.items);if(gifts.length){await request(`${api}/api/social-decor/human-gift-notices/seen`,{receipt_ids:gifts.map(g=>g.id)});setGifts([]);}}catch{setError('部分已读未确认，稍后重试。');}finally{setLoading(false);}})();}}>当前列表全部已读</button></div>
        {newHomes.length>0&&<button className="social-inbox-new" onClick={()=>{setNewHomes([]);setOpen(false);openFeed();}}>有新动态 · {newHomes.join('、')}　去共域刷新 ›</button>}
        {[...groups.values()].map(group=>{const n=group[0];return <button key={JSON.stringify([n.site_id,n.id])} className="social-inbox-row" onClick={()=>void inspect(n)} disabled={loading}><small>{n.site_name} · {new Date(n.created_at*1000).toLocaleString()}</small><strong>{[...new Set(group.map(r=>r.actor_name||'朋友'))].join('、')}{labels[n.kind]||'有新动静'}</strong><span>{n.preview}</span></button>;})}
        {listedGifts.map(g=><button className="social-inbox-row" key={'gift:'+g.id} onClick={()=>void seeGift(g)}><small>赠礼 · 已收入收藏</small><strong>{g.machine_name}收到了「{g.snapshot.name}」</strong></button>)}
        {!listed.length&&!listedGifts.length&&!loading&&<p>当前没有新通知。朋友的新动态请看全域时间线。</p>}
        {data.unavailable.length>0&&<p className="social-inbox-muted">{data.unavailable.map(s=>`${s.site_name}：${s.status==='unsupported'?'尚未支持通知':s.status==='limited'?'尚未声明完整互动通知能力':'暂时无法读取'}`).join('；')}。提醒范围以来源域能力为准；无法读取不等于没有新动静。</p>}
      </>}
    </dialog></>,document.querySelector('.app')||document.body)}</>;
}
