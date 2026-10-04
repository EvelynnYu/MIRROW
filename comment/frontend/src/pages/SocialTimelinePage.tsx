import {useCallback,useEffect,useRef,useState} from 'react';
import {getApiBase} from '../config';
import SocialTimelineCard,{timelineRef,type TimelineMoment} from '../components/SocialTimelineCard';
import SocialFeedFilters,{ALL_SOCIAL_SITES,type VisibilityFilter} from '../components/SocialFeedFilters';
import SocialIdentityManager from '../components/SocialIdentityManager';
import SocialDecorPanel from '../components/SocialDecorPanel';
import type {SocialSite} from './SocialRemoteFeed';
import {socialViewKey} from '../social/viewState';
import {useSocialView,useSocialReadingPlace} from '../social/useSocialView';
import {readTimelineStream,type TimelinePage as Page} from '../social/timelineStream';
import './SocialFeedPage.css';
import SocialMentions from '../components/SocialMentions';

export default function SocialTimelinePage({sites,onSwitch,onBack}:{sites:SocialSite[];onSwitch:(id:string)=>void;onBack:()=>void}) {
  const view=(field:string)=>socialViewKey(getApiBase(),'aning','timeline',field);
  const [filter,setFilter]=useSocialView<VisibilityFilter>(view('filter'),'all');
  const [items,setItems]=useState<TimelineMoment[]>([]);
  const itemsRef=useRef(items);itemsRef.current=items;
  const [cursor,setCursor]=useState<string|null>(null);
  const [more,setMore]=useState(false);
  const [loading,setLoading]=useState(true);
  const [completedFilter,setCompletedFilter]=useState<VisibilityFilter|null>(null);
  const [error,setError]=useState('');
  const [unavailable,setUnavailable]=useState<string[]>([]);
  const [omitted,setOmitted]=useState(0);
  const [shelf,setShelf]=useSocialView(view('shelf'),false);
  const [compose,setCompose]=useSocialView(view('compose'),false);
  const [destination,setDestination]=useSocialView(view('destination'),'');
  const [draft,setDraft]=useSocialView(view('draft'),'');
  const [visibility,setVisibility]=useSocialView<'private'|'public'>(view('visibility'),'private');
  const [mentions,setMentions]=useSocialView<string[]>(view(`draft-mentions:${destination||'home'}:${visibility}`),[]);
  const [publishing,setPublishing]=useState(false);
  const [status,setStatus]=useState('');
  const sequence=useRef(0);
  const readAbort=useRef<AbortController|null>(null);
  const [progress,setProgress]=useState('');
  const [staged,setStaged]=useState<Page|null>(null);
  const [provisional,setProvisional]=useState(false);
  const place=useSocialReadingPlace(view('place:'+filter),!loading&&!staged&&completedFilter===filter,items,more,()=>void load(cursor));
  const publishAbort=useRef<AbortController|null>(null);
  const publishSequence=useRef(0);
  const mentionDomain=useRef(`${destination}:${visibility}`);
  const load=useCallback(async(next:string|null=null)=>{
    const seq=++sequence.current;
    readAbort.current?.abort();
    const controller=new AbortController();readAbort.current=controller;
    setLoading(true);setError('');setProgress('');setStaged(null);
    let preview:TimelineMoment[]=[];
    try {
      const params=new URLSearchParams({limit:'30',visibility_filter:filter});
      if(next)params.set('cursor',next);
      const options={headers:{'X-MIRROW-Lounge-Admin':'1'},cache:'no-store' as RequestCache,signal:controller.signal};
      let response=await fetch(`${getApiBase()}/api/social-sites/timeline/stream?${params}`,options);
      // Older friend deployments retain the complete JSON endpoint.
      if([404,405].includes(response.status))response=await fetch(`${getApiBase()}/api/social-sites/timeline?${params}`,options);
      if(!response.ok)throw new Error('时间线暂时不可用，请确认后端已更新。');
      const page=response.headers.get('Content-Type')?.includes('application/x-ndjson')?
        await readTimelineStream(response,controller.signal,event=>{
          if(seq!==sequence.current)return;
          if(event.type==='source'){
            setProgress(`已读取 ${event.finished_sources}/${event.total_sources} 家`);
            // Only a new entry previews one fast source. A return restores from
            // the complete page; refresh/paging never replaces the reading spot.
            if(!next&&!place.restoring.current&&!itemsRef.current.length&&!preview.length&&event.items.length){
              preview=event.items.slice(0,5);setItems(preview);setProvisional(true);
            }
          }
        }):await response.json() as Page;
      if(seq!==sequence.current)return;
      setCursor(page.next_cursor);setMore(page.has_more);
      setCompletedFilter(filter);
      setUnavailable(page.unavailable.map(r=>r.site_name));setOmitted(page.omitted_sources);
      if(preview.length&&JSON.stringify(preview.map(timelineRef))!==JSON.stringify(page.items.map(timelineRef)))setStaged(page);
      else {setProvisional(false);setItems(old=>next?[...old,...page.items.filter(r=>!old.some(p=>timelineRef(p)===timelineRef(r)))]:page.items);}
    } catch(e){if(seq===sequence.current)setError(e instanceof Error?e.message:'加载失败');}
    finally{if(seq===sequence.current)setLoading(false);}
  },[filter]);
  useEffect(()=>{itemsRef.current=[];setItems([]);setProvisional(false);setCursor(null);setMore(false);void load();return()=>{sequence.current++;readAbort.current?.abort();};},[load]);
  useEffect(()=>()=>{publishSequence.current++;publishAbort.current?.abort();},[]);
  useEffect(()=>{const next=`${destination}:${visibility}`;if(mentionDomain.current!==next){mentionDomain.current=next;setMentions([]);}},[destination,visibility,setMentions]);
  const closeCompose=()=>{publishSequence.current++;publishAbort.current?.abort();setPublishing(false);setCompose(false);};
  const publish=async()=>{
    if(publishing||!draft.trim())return;
    const seq=++publishSequence.current;
    const controller=new AbortController();publishAbort.current=controller;
    const timeout=window.setTimeout(()=>controller.abort(),15000);
    setPublishing(true);setError('');
    const base=destination?`${getApiBase()}/api/social-sites/${encodeURIComponent(destination)}`:`${getApiBase()}/api/social-feed`;
    try {
      const response=await fetch(`${base}/moments`,{method:'POST',headers:{'Content-Type':'application/json','X-MIRROW-Lounge-Admin':'1'},body:JSON.stringify(destination?{content:draft.trim(),...(mentions.length?{mention_actor_ids:mentions}:{})}:{content:draft.trim(),visibility,...(mentions.length?{mention_actor_ids:mentions}:{})}),signal:controller.signal});
      if(!response.ok)throw new Error();
      if(seq!==publishSequence.current)return;
      setDraft('');setMentions([]);setCompose(false);setStatus(destination?'已寄存在选定共域，由那家的主人管理。':'已发布在本家。');
      // An explicit new post may refresh the timeline; background updates never do.
      void load();
    } catch {if(seq===publishSequence.current)setError('发布结果未确认，请到目标共域核对；不会自动重试。');}
    finally {window.clearTimeout(timeout);if(seq===publishSequence.current)setPublishing(false);}
  };
  const day=(stamp:number)=>new Date(stamp*1000).toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric'});
  const updated=(fresh:TimelineMoment)=>setItems(old=>old.map(r=>timelineRef(r)===timelineRef(fresh)?fresh:r).filter(r=>filter==='all'||r.visibility===filter));
  return <div className="social-feed-page social-timeline-page">
    <header className="social-feed-header"><button className="social-feed-back" type="button" onClick={onBack} aria-label="返回机生活">‹</button><div className="social-feed-heading"><h2>共域</h2><span>MIRROW·Comment</span></div><span data-social-music-slot/><SocialIdentityManager onSaved={()=>void load()}/></header>
    <SocialFeedFilters sites={sites} current={ALL_SOCIAL_SITES} onSwitch={onSwitch} value={filter} onChange={setFilter}/>
    <main className="social-feed-body" ref={place.body}>
      <section className="social-timeline-shelf">
        <button type="button" aria-expanded={shelf} onClick={()=>setShelf(value=>!value)}>{shelf?'▾':'▸'} ✧ 本家的奇物架与空间管理</button>
        <SocialDecorPanel compact={!shelf}/>
      </section>
      <div className="social-timeline-status" role="status">{filter==='private'?'只显示本家的私密动态':'本家与已注册域合并浏览 · 远端仅公开'}<button type="button" disabled={loading} onClick={()=>void load()}>刷新</button>
        {unavailable.length>0&&<div>暂时无法读取：{unavailable.join('、')}。其他共域仍可浏览。</div>}{omitted>0&&<div>本次仅汇总前 32 家；另有 {omitted} 家可单独进入查看。</div>}</div>
      {error&&<div role="alert" className="social-feed-error">{error}</div>}{status&&<div role="status" className="social-feed-feedback">{status}</div>}
      {place.notice&&<p role="status">{place.notice}</p>}
      {items.map((item,i)=><div key={timelineRef(item)} data-social-view-post={timelineRef(item)}>{(i===0||day(items[i-1].created_at)!==day(item.created_at))&&<div className="social-feed-day-separator"><span>{day(item.created_at)}</span></div>}<SocialTimelineCard item={item} onUpdated={updated} onSwitch={onSwitch} readingOnly={provisional}/></div>)}
      {(loading||provisional)&&<p className="social-feed-loading">{progress||'正在读取时间线……'}{items.length>0?' · 先看已到达内容，完整页就绪后再互动。':''}</p>}{!loading&&!items.length&&<p className="social-feed-empty">当前筛选没有可展示的动态。</p>}
      {more&&<button className="social-feed-action" type="button" disabled={loading||Boolean(staged)} onClick={()=>void load(cursor)}>加载更早动态</button>}
    </main>
    {staged&&<div className="social-timeline-ready" role="status"><span>本页已读完</span><button className="social-feed-action" type="button" onClick={()=>{setItems(staged.items);setStaged(null);setProvisional(false);}}>显示完整时间线</button></div>}
    <button type="button" className="social-feed-fab" aria-label="写一条动态" onClick={()=>{setDestination('');setCompose(true);}}>＋</button>
    {compose&&<div className="social-feed-compose-backdrop" onClick={closeCompose}><section className="social-feed-compose-sheet" role="dialog" aria-modal="true" aria-label="发布共域动态" onClick={e=>e.stopPropagation()}><header><h3>写一条动态</h3><button type="button" onClick={closeCompose} aria-label="关闭">×</button></header>
      <label className="social-compose-destination">发布到<select value={destination} onChange={e=>setDestination(e.target.value)}><option value="">本家</option>{sites.filter(s=>s.enabled&&s.has_human_key).map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      <p className="social-compose-warning">{destination?'寄存在这家；获准访问这家的朋友可见，那家的主人拥有管理权限。':'发布于今天；公开给有访问权的朋友，私密仅本家可见。'}</p>
      <textarea value={draft} maxLength={1200} onChange={e=>setDraft(e.target.value)} placeholder="这一刻想分享什么……"/><SocialMentions endpoint={destination?`${getApiBase()}/api/social-sites/${encodeURIComponent(destination)}/mention-people`:`${getApiBase()}/api/social-feed/mention-people?visibility=${visibility}`} selected={mentions} onChange={setMentions} disabled={publishing}/>
      {error&&<div className="social-feed-error" role="alert">{error}</div>}
      <footer>{!destination&&<select value={visibility} onChange={e=>setVisibility(e.target.value as 'private'|'public')} aria-label="发布可见范围"><option value="private">私密 · 仅本家</option><option value="public">公开 · 朋友可见</option></select>}<button className="social-feed-publish" type="button" disabled={publishing||!draft.trim()} onClick={()=>void publish()}>{publishing?'发布中…':'发表'}</button></footer>
    </section></div>}
  </div>;
}
