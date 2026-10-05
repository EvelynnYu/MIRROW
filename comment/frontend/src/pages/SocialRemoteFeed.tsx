import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getApiBase } from '../config';
import './SocialRemoteFeed.css';
import SocialDecorPanel from '../components/SocialDecorPanel';
import RemoteSocialAvatar from '../components/RemoteSocialAvatar';
import SocialIdentityManager from '../components/SocialIdentityManager';
import SocialPersonCard, { type SocialPerson } from '../components/SocialPersonCard';
import './SocialFeedPage.css';
import SocialFeedFilters,{SocialSourceLabel,type VisibilityFilter} from '../components/SocialFeedFilters';
import {socialViewKey} from '../social/viewState';
import {useSocialView,useSocialReadingPlace} from '../social/useSocialView';
import SocialMentions from '../components/SocialMentions';
import {SocialForwardCard,SocialForwardDialog,type Forward} from '../components/SocialForward';

export type SocialSite = {
  id: string;
  name: string;
  origin: string;
  has_human_key: boolean;
  has_ai_key: boolean;
  enabled: boolean;
};

type Person = { name?: string; nickname?: string; avatar?: string };
type Comment = { id: string; author: string; content: string; reply_to_id?: string | null; created_at: number; can_delete?: boolean; mention_actor_ids?:string[] };
type Reaction = { author: string };
type Moment = {
  id: string; author: string; content: string; created_at: number;
  migrated?: boolean;
  hosting_mode?:string;
  forward?:Forward;
  people?: Record<string, Person>; comments: Comment[]; reactions: Reaction[]; mention_actor_ids?:string[];
};
type Page = { items: Moment[]; has_more: boolean; next_cursor: [number, string] | null };
type PendingTransfer = { id: string; source_origin: string; source_moment_id: string; target_actor: string };

export default function SocialRemoteFeed({ site, sites, onSwitch, onBack }: {
  site: SocialSite; sites: SocialSite[]; onSwitch: (id: string) => void; onBack: () => void;
}) {
  const base = `${getApiBase()}/api/social-sites/${encodeURIComponent(site.id)}`;
  const view=(field:string)=>socialViewKey(getApiBase(),'aning','remote',site.id,field);
  const alive=useRef(false), readSequence=useRef(0), readAbort=useRef<AbortController|null>(null);
  const [actorId, setActorId] = useState('');
  const [aiActorId, setAiActorId] = useState('');
  const [pending, setPending] = useState<PendingTransfer[]>([]);
  const [transferReady, setTransferReady] = useState(false);
  const [items, setItems] = useState<Moment[]>([]);
  const [cursor, setCursor] = useState<[number, string] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [draft, setDraft] = useSocialView(view('draft'),'');
  const [comments, setComments] = useSocialView<Record<string, string>>(view('comments'),{});
  const [replyTo, setReplyTo] = useSocialView<Record<string, string>>(view('replies'),{});
  const [expandedComments,setExpandedComments]=useSocialView<Record<string,boolean>>(view('expanded-comments'),{});
  const [personSubject, setPersonSubject] = useState('');
  const [forwardMoment,setForwardMoment]=useState('');
  const [visibilityFilter,setVisibilityFilter]=useSocialView<VisibilityFilter>(view('filter'),'all');
  const [shelfOpen,setShelfOpen]=useSocialView(view('shelf'),false);
  const [composeOpen,setComposeOpen]=useSocialView(view('compose'),false);
  const [draftMentions,setDraftMentions]=useSocialView<string[]>(view('draft-mentions'),[]);
  const [commentMentions,setCommentMentions]=useSocialView<Record<string,string[]>>(view('comment-mentions'),{});
  const peopleCache = useMemo(() => new Map<string, Person>(), [base]);

  const name = (item: Moment, who: string) => {
    const person = item.people?.[who];
    const label = person?.name?.trim() || person?.nickname?.trim();
    return label && label !== '好友' ? label : who === actorId ? '我（资料待同步）' : '资料待同步';
  };
  const when = (timestamp: number) => new Date(timestamp * 1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  const day = (timestamp: number) => new Date(timestamp * 1000).toLocaleDateString('zh-CN');

  const request = useCallback(async (path: string, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    headers.set('X-MIRROW-Lounge-Admin', '1');
    const response = await fetch(`${base}${path}`, { cache: 'no-store', ...init, headers });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `共域连接失败 (${response.status})`);
    return payload;
  }, [base]);

  const hydrate = useCallback(async (rows: Moment[]) => {
    // Older hosts may omit interaction participants from people. Recover only
    // through this home's authenticated person API, never by matching names.
    const missing = new Set<string>();
    for (const row of rows) for (const actor of [row.author, ...row.comments.map(r => r.author), ...row.reactions.map(r => r.author)]) {
      const person = row.people?.[actor];
      if (!person?.name && !person?.nickname && !peopleCache.has(actor)) missing.add(actor);
    }
    const actors = [...missing].slice(0, 8);
    for (let i = 0; i < actors.length; i += 4) await Promise.all(actors.slice(i, i + 4).map(async actor => {
      try {
        const result = await request(`/people/${encodeURIComponent(actor)}`, {signal:AbortSignal.timeout(2500)});
        if (result.person?.actor_id === actor) peopleCache.set(actor, result.person);
      } catch { /* Missing optional profile API does not hide the actual feed. */ }
    }));
    return rows.map(row => {
      const people = {...row.people};
      for (const [actor, person] of peopleCache) if (!people[actor]?.name && !people[actor]?.nickname) people[actor] = person;
      return {...row, people};
    });
  }, [request, peopleCache]);

  const load = useCallback(async (next?: [number, string]) => {
    const seq=++readSequence.current;
    readAbort.current?.abort();const controller=new AbortController();readAbort.current=controller;
    const params = new URLSearchParams({ limit: '20' });
    if (next) { params.set('before_time', String(next[0])); params.set('before_id', next[1]); }
    const page = await request(`/moments?${params}`,{signal:controller.signal}) as Page;
    if(!alive.current||seq!==readSequence.current)return;
    setItems(current => next ? [...current, ...page.items.filter(row => !current.some(old => old.id === row.id))] : page.items);
    setCursor(page.next_cursor);
    setHasMore(page.has_more);
    void hydrate(page.items).then(rows=>{
      if(!alive.current||seq!==readSequence.current)return;
      setItems(current=>current.map(row=>{
        const recovered=rows.find(r=>r.id===row.id),people={...row.people};
        for(const [id,person] of Object.entries(recovered?.people||{}))if(!people[id]?.name&&!people[id]?.nickname)people[id]=person;
        return {...row,people};
      }));
    });
  }, [request, hydrate]);

  const earlier=()=>{if(!busy&&hasMore&&cursor){setBusy(true);void load(cursor).catch(err=>{if(alive.current)setError(err.message);}).finally(()=>{if(alive.current)setBusy(false);});}};
  const place=useSocialReadingPlace(view('place:'+visibilityFilter),!loading&&!busy,items,hasMore,earlier,visibilityFilter!=='private');

  useEffect(() => {
    alive.current=true;
    let mounted=true;
    const seq=++readSequence.current;
    const controller=new AbortController();readAbort.current=controller;
    const current=()=>alive.current&&seq===readSequence.current;
    setLoading(true); setError(''); setStatus(''); setItems([]); setCursor(null); setPersonSubject(''); setForwardMoment('');
    // Optional machine/migration controls do not hold the authenticated feed.
    void request('/me/ai',{signal:controller.signal}).then(ai=>{if(mounted)setAiActorId(ai.actor?.actor_id||'');}).catch(()=>{});
    void Promise.all([
      fetch(`${getApiBase()}/api/social-sites/migrations/pending`, { headers: { 'X-MIRROW-Lounge-Admin': '1' }, cache: 'no-store',signal:controller.signal })
        .then(async response => response.ok ? response.json() : { items: [] }).catch(() => ({ items: [] })),
      fetch(`${getApiBase()}/api/social-sites/migrations/capability`, { headers: { 'X-MIRROW-Lounge-Admin': '1' }, cache: 'no-store',signal:controller.signal })
        .then(async response => response.ok ? response.json() : { enabled: false }).catch(() => ({ enabled: false }))
    ]).then(([migrations,capability])=>{
      if(!mounted)return;
      setPending((migrations.items || []).filter((row: PendingTransfer) => row.source_origin === site.origin));
      setTransferReady(Boolean(capability.enabled));
    });
    Promise.all([request('/me',{signal:controller.signal}),request('/moments?limit=20',{signal:controller.signal})]).then(([me,page])=>{
      if(!current())return;
      setActorId(me.actor?.actor_id || '');
      setItems(page.items || []); setCursor(page.next_cursor || null); setHasMore(Boolean(page.has_more));
      // Render real content first. Optional old-host profile recovery runs in
      // the background and cannot overwrite a newly selected home or actions.
      void hydrate(page.items || []).then(hydrated => {
        if (current()) setItems(current => current.map(row => {
          const recovered = hydrated.find(item => item.id === row.id);
          if (!recovered) return row;
          const people = {...row.people};
          for (const [actor, person] of Object.entries(recovered.people || {})) {
            if (!people[actor]?.name && !people[actor]?.nickname) people[actor] = person;
          }
          return {...row, people};
        }));
      });
    }).catch(err => { if (current()) setError(err.message); })
      .finally(() => { if (current()) setLoading(false); });
    return () => { mounted=false;alive.current=false;readSequence.current++;controller.abort();readAbort.current?.abort(); };
  }, [request, site.origin, hydrate]);

  const migrate = async (momentId: string, actor: 'human' | 'ai') => {
    if (busy || !window.confirm('把这条寄存动态和评论、点赞迁回本家？旧站会只留迁移提示。迁移期间暂时不能互动。')) return;
    setBusy(true); setError(''); setStatus('');
    try {
      const result = await request(`/moments/${encodeURIComponent(momentId)}/migrate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor }),
      });
      setStatus(result.state === 'complete' ? '动态与互动已迁回本家；旧站只保留迁移提示。' : result.message || '迁移待续，请稍后重试。');
      if (result.state === 'complete') {
        setItems(current => current.filter(row => row.id !== momentId));
        setPending(current => current.filter(row => row.source_moment_id !== momentId));
      } else {
        setPending(current => current.some(row => row.source_moment_id === momentId) ? current :
          [...current, { id: result.transfer_id, source_origin: site.origin, source_moment_id: momentId,
            target_actor: actor === 'human' ? 'aning' : 'k' }]);
      }
    } catch (err) { setError(err instanceof Error ? err.message : '迁移结果未确认，请稍后重试同一条动态'); }
    finally { setBusy(false); }
  };

  const cancelMigration = async (transferId: string) => {
    if (busy || !window.confirm('取消这次待续迁移？旧站会解冻原帖，新站未公开的暂存副本会删除。已完成的迁移不能取消。')) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await request(`/migrations/${encodeURIComponent(transferId)}/cancel`, { method: 'POST' });
      setPending(current => current.filter(row => row.id !== transferId));
      setStatus('迁移已取消，旧站原帖可继续互动。');
    } catch (err) { setError(err instanceof Error ? err.message : '取消结果未确认，请刷新后重试'); }
    finally { setBusy(false); }
  };

  const post = async () => {
    const content = draft.trim(); if (!content || busy) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await request('/moments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, ...(draftMentions.length?{mention_actor_ids:draftMentions}:{}) }) });
      setDraft(''); setDraftMentions([]); await load(); setStatus(`已发布在${site.name}。`);
    } catch (err) { setError(err instanceof Error ? err.message : '发布结果未确认，请刷新核对后再试'); }
    finally { setBusy(false); }
  };

  const comment = async (item: Moment) => {
    const content = comments[item.id]?.trim(); if (!content || busy) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await request(`/moments/${encodeURIComponent(item.id)}/comments`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, reply_to_id: replyTo[item.id] || null, ...(commentMentions[item.id]?.length?{mention_actor_ids:commentMentions[item.id]}:{}) }),
      });
      setComments(current => ({ ...current, [item.id]: '' }));
      setCommentMentions(current => ({ ...current, [item.id]: [] }));
      setReplyTo(current => ({ ...current, [item.id]: '' }));
      const [refreshed] = await hydrate([await request(`/moments/${encodeURIComponent(item.id)}`) as Moment]);
      setItems(current => current.map(row => row.id === item.id ? refreshed : row));
      setStatus(`评论已发到${site.name}。`);
    } catch (err) { setError(err instanceof Error ? err.message : '评论结果未确认，请刷新核对后再试'); }
    finally { setBusy(false); }
  };

  const like = async (item: Moment) => {
    if (busy) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await request(`/moments/${encodeURIComponent(item.id)}/like`, { method: 'POST' });
      const [refreshed] = await hydrate([await request(`/moments/${encodeURIComponent(item.id)}`) as Moment]);
      setItems(current => current.map(row => row.id === item.id ? refreshed : row));
      setStatus('互动已更新。');
    } catch (err) { setError(err instanceof Error ? err.message : '点赞结果未确认，请刷新核对后再试'); }
    finally { setBusy(false); }
  };

  const removeComment = async (item: Moment, row: Comment) => {
    if (busy || !(row.can_delete ?? row.author === actorId) || !window.confirm('删除这条评论？删除后无法恢复。')) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await request(`/moments/${encodeURIComponent(item.id)}/comments/${encodeURIComponent(row.id)}`, {method:'DELETE'});
      setItems(current => current.map(old => old.id === item.id ? {...old, comments:old.comments.filter(c => c.id !== row.id)} : old));
      if (replyTo[item.id] === row.id) setReplyTo(current => ({...current, [item.id]:''}));
      setStatus('评论已删除。');
    } catch (err) { setError(err instanceof Error ? err.message : '删除结果未确认，请刷新核对'); }
    finally { setBusy(false); }
  };

  const updatePerson = (person: SocialPerson) => {
    peopleCache.set(person.actor_id, person);
    setItems(rows => rows.map(row => ({...row, people:{...row.people, [person.actor_id]:person}})));
  };

  return <div className="social-remote-page">
    <header className="social-remote-header">
      <button type="button" onClick={onBack} aria-label="返回">‹</button>
      <div><h2>共域</h2><small>MIRROW·Comment</small></div>
      <span data-social-music-slot className="md-remote-music-slot" />
      <SocialIdentityManager remote onSaved={() => {}} />
    </header>
    <SocialFeedFilters sites={sites} current={site.id} onSwitch={onSwitch} value={visibilityFilter} onChange={setVisibilityFilter}/>
    <main className="social-remote-body" ref={place.body}>
      <section className="social-timeline-shelf"><button type="button" aria-expanded={shelfOpen} onClick={()=>setShelfOpen(v=>!v)}>✧ {site.name}的奇物架与访客礼物 {shelfOpen?'⌃':'⌄'}</button><SocialDecorPanel siteId={site.id} compact={!shelfOpen}/></section>
      <p className="social-timeline-status">{site.name} · 仅公开；寄存动态与互动由这家的主人管理。</p>
      {error && <div role="alert" className="social-remote-error">{error}</div>}
      {status && <div role="status" className="social-remote-status">{status}</div>}
      {place.notice&&<p role="status">{place.notice}</p>}
      {pending.length > 0 && <section className="social-remote-compose"><strong>待续迁移</strong>{pending.map(row =>
        <div key={row.id}><button type="button" disabled={busy} onClick={() => void migrate(row.source_moment_id, row.target_actor === 'k' ? 'ai' : 'human')}>继续迁回 {row.source_moment_id.slice(0, 12)}…</button>
        <button type="button" disabled={busy} onClick={() => void cancelMigration(row.id)}>取消迁移</button></div>)}</section>}
      <button type="button" className="social-feed-action" onClick={()=>setComposeOpen(v=>!v)}>{composeOpen?'收起发布':'在这里写一条'}</button>
      {composeOpen&&<section className="social-remote-compose"><p className="social-compose-warning">公开寄存在{site.name}，那家的主人拥有管理权限。</p><textarea value={draft} maxLength={1200} onChange={event => setDraft(event.target.value)} placeholder={`在${site.name}留一条公开动态……`} /><SocialMentions endpoint={`${base}/mention-people`} selected={draftMentions} onChange={setDraftMentions} disabled={busy} onPerson={setPersonSubject}/><button type="button" disabled={busy || !draft.trim()} onClick={() => void post()}>{busy ? '处理中…' : '发布到这里'}</button></section>}
      {visibilityFilter==='private'?<p>外家共域只开放公开动态；私密内容仅在本家可见。</p>:loading ? <p>正在进入{site.name}……</p> : items.length === 0 ? <p>这里还没有动态。</p> : items.map((item, index) => <div key={item.id}>
        {(index === 0 || day(items[index - 1].created_at) !== day(item.created_at)) && <div className="social-feed-day-separator"><span>{day(item.created_at)}</span></div>}
        <article className="social-feed-card social-feed-card-friend" data-social-post-author={item.author} data-social-view-post={item.id}>
        <header className="social-feed-card-head"><RemoteSocialAvatar base={base} origin={site.origin} actor={item.author} avatar={item.people?.[item.author]?.avatar} name={name(item,item.author)} onClick={() => setPersonSubject(item.author)} /><strong className="social-feed-author">{name(item, item.author)}</strong><time className="social-feed-time" title={new Date(item.created_at*1000).toLocaleString('zh-CN')}>{when(item.created_at)}</time></header>
        {transferReady && !item.migrated && (item.author === actorId || item.author === aiActorId) &&
          <button type="button" disabled={busy} onClick={() => void migrate(item.id, item.author === aiActorId ? 'ai' : 'human')}>迁回本家</button>}
        <div className="social-feed-content">{item.content}</div>{item.mention_actor_ids?.length?<div className="social-mention-chips">{item.mention_actor_ids.map(id=>item.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPersonSubject(id)}>@{item.people[id].name}</button>)}</div>:null}
        <SocialForwardCard forward={item.forward} name={id=>name(item,id)} base={base}/>
        {!item.migrated && <>
        <div className="social-feed-actions"><button type="button" className={`social-feed-action ${item.reactions.some(row => row.author === actorId) ? 'active' : ''}`} disabled={busy} onClick={() => void like(item)}>{item.reactions.some(row => row.author === actorId) ? '♥ 已赞' : '♡ 点赞'} {item.reactions.length || ''}</button><button type="button" className="social-feed-action" onClick={() => document.getElementById(`remote-comment-${item.id}`)?.focus()}>◌ {item.comments.length || ''} 条评论</button>{!item.forward&&<button type="button" className="social-feed-action social-feed-action-forward" disabled={busy} onClick={()=>setForwardMoment(item.id)}>↗ 转发</button>}</div>
        {item.reactions.length > 0 && <div className="social-feed-liked-by" aria-label="点赞的人"><span aria-hidden="true">♥</span>{item.reactions.map(row => name(item, row.author)).join('、')}</div>}
        {item.comments.length > 0 && <div className="social-feed-comments">{(expandedComments[item.id]?item.comments:item.comments.slice(-3)).map(row => <div className="social-feed-comment" key={row.id}>
          <RemoteSocialAvatar base={base} origin={site.origin} actor={row.author} avatar={item.people?.[row.author]?.avatar} name={name(item,row.author)} className="social-feed-comment-avatar" onClick={() => setPersonSubject(row.author)} />
          <div className="social-feed-comment-body"><div className="social-feed-comment-meta"><strong>{name(item, row.author)}</strong>{row.reply_to_id && <span> 回复 {name(item, item.comments.find(parent => parent.id === row.reply_to_id)?.author || '')}</span>}<time>{when(row.created_at)}</time></div><p className="social-feed-comment-text">{row.content}</p>{row.mention_actor_ids?.length?<div className="social-mention-chips">{row.mention_actor_ids.map(id=>item.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPersonSubject(id)}>@{item.people[id].name}</button>)}</div>:null}<div className="social-feed-comment-tools"><button type="button" onClick={() => {setReplyTo(current => ({ ...current, [item.id]: row.id })); document.getElementById(`remote-comment-${item.id}`)?.focus();}}>回复</button>{(row.can_delete ?? row.author === actorId) && <button type="button" disabled={busy} onClick={() => void removeComment(item,row)}>删除</button>}</div></div>
        </div>)}</div>}
        {item.comments.length>3&&<button type="button" className="social-comments-toggle" onClick={()=>setExpandedComments(current=>({...current,[item.id]:!current[item.id]}))}>{expandedComments[item.id]?'收起评论':`展开全部 ${item.comments.length} 条评论`}</button>}
        {replyTo[item.id] && <div className="social-feed-replying"><span>正在回复 {name(item,item.comments.find(row => row.id === replyTo[item.id])?.author || '')}</span><button type="button" onClick={() => setReplyTo(current => ({ ...current, [item.id]: '' }))}>取消 ×</button></div>}
        <div className="social-feed-comment-box"><SocialMentions compact endpoint={`${base}/mention-people`} selected={commentMentions[item.id]||[]} onChange={ids=>setCommentMentions(current=>({...current,[item.id]:ids}))} disabled={busy} onPerson={setPersonSubject}/><input id={`remote-comment-${item.id}`} value={comments[item.id] || ''} maxLength={600} onChange={event => setComments(current => ({ ...current, [item.id]: event.target.value }))} placeholder="写下一句……" /><button type="button" disabled={busy || !comments[item.id]?.trim()} onClick={() => void comment(item)}>发送</button></div>
        </>}
        <SocialSourceLabel name={site.name} hosted={item.hosting_mode==='hosted'||(!item.hosting_mode&&item.author.startsWith('visitor:'))}/>
      </article></div>)}
      {hasMore&&visibilityFilter!=='private' && <button type="button" className="social-remote-more" disabled={busy||loading} onClick={earlier}>{busy?'正在加载…':'加载更早动态'}</button>}
    </main>
    {forwardMoment&&<SocialForwardDialog key={site.id+forwardMoment} sourceSiteId={site.id} momentId={forwardMoment} onClose={()=>setForwardMoment('')}/>}
    {personSubject && <SocialPersonCard actorId={personSubject} remote={{base,origin:site.origin}} onClose={() => setPersonSubject('')} onSaved={updatePerson} />}
  </div>;
}
