import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getApiBase } from '../config';
import SocialPersonCard, { type SocialPerson } from '../components/SocialPersonCard';
import SocialRemoteFeed, { type SocialSite } from './SocialRemoteFeed';
import './SocialFeedPage.css';
import SocialDecorPanel from '../components/SocialDecorPanel';
import SocialIdentityManager from '../components/SocialIdentityManager';
import './SocialFeedVisibility.css';
import SocialTimelinePage from './SocialTimelinePage';
import SocialFeedFilters,{ALL_SOCIAL_SITES,SocialSourceLabel,type VisibilityFilter} from '../components/SocialFeedFilters';
import {socialViewKey} from '../social/viewState';
import {useSocialView,useSocialReadingPlace} from '../social/useSocialView';
import SocialMentions from '../components/SocialMentions';
import {SocialForwardCard,SocialForwardDialog,type Forward} from '../components/SocialForward';

type Comment = { id:string; author:string; content:string; reply_to_id?:string|null; created_at:number; can_delete?:boolean; mention_actor_ids?:string[] };
type Reaction = { author:string; reaction:'like'; created_at:number };
type Visibility = 'private'|'public';
type PublicPerson = { name:string; avatar:string };
type Moment = { id:string; author:string; content:string; visibility:Visibility; created_at:number; revision?:number; comments:Comment[]; reactions:Reaction[]; people?:Record<string,PublicPerson>; mention_actor_ids?:string[]; forward?:Forward };
type Notice = { id:string; actor:string; actor_name?:string; kind:'moment'|'comment'|'like'; moment_id:string; moment_content?:string|null; comment_content?:string|null };
type FeedResponse = { items?:Moment[]; next_cursor?:string|null; has_more?:boolean };

const today = () => {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit' }).formatToParts(new Date());
  const map = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
};

const who = (author:string) => author === 'k' ? 'AI' : author === 'aning' ? '站主' : '好友';
const named = (moment:Moment, author:string) => moment.people?.[author]?.name || who(author);
const avatarFallback = (author:string) => author === 'k' ? 'AI' : author === 'aning' ? '主' : '友';
const kindText = (kind:string) => kind === 'moment' ? '发了新动态' : kind === 'comment' ? '留下了新评论' : '点了赞';
const formatTime = (timestamp:number) => new Intl.DateTimeFormat('zh-CN', { timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit' }).format(new Date(timestamp * 1000));
const formatDay = (timestamp:number) => new Intl.DateTimeFormat('zh-CN', { timeZone:'Asia/Shanghai', year:'numeric', month:'long', day:'numeric', weekday:'short' }).format(new Date(timestamp * 1000));

const isImageAvatar = (value:string) => value.startsWith('data:image/') || value.startsWith('blob:') || value.startsWith('/') || value.startsWith('https://') || value.startsWith('http://');
const imageSource = (value:string) => value.startsWith('/api/social-feed/') ? `${getApiBase()}${value}` : value;

function FeedAvatar({ author, value, className = 'social-feed-avatar', onClick }:{ author:string; value:string; className?:string; onClick?:()=>void }) {
  const display = value || avatarFallback(author);
  const avatar = isImageAvatar(display)
    ? <img className={`${className} social-feed-avatar-image`} src={imageSource(display)} alt={`${who(author)}头像`} />
    : <div className={className} aria-label={`${who(author)}头像`}>{display}</div>;
  return onClick ? <button type="button" data-social-actor={author} className="social-avatar-trigger" onClick={onClick} aria-label={`查看${who(author)}头像`}>{avatar}</button> : <span data-social-actor={author}>{avatar}</span>;
}

export default function SocialFeedPage({ onBack }:{ onBack:()=>void }) {
  const api = getApiBase();
  const view=(field:string)=>socialViewKey(api,'aning','home',field);
  const [anchorDay, setAnchorDay] = useSocialView(view('day'),today);
  const [siteId, setSiteId] = useSocialView(view('site'),ALL_SOCIAL_SITES);
  const [visibilityFilter, setVisibilityFilter] = useSocialView<VisibilityFilter>(view('filter'),'all');
  const [shelfOpen, setShelfOpen] = useSocialView(view('shelf'),false);
  const [sites, setSites] = useState<SocialSite[]>([]);
  const [sitesReady,setSitesReady]=useState(false);
  const [items, setItems] = useState<Moment[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [completedFilter,setCompletedFilter]=useState<VisibilityFilter|null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [draft, setDraft] = useSocialView(view('draft'),'');
  const [visibility, setVisibility] = useSocialView<Visibility>(view('visibility'),'private');
  const [composeOpen, setComposeOpen] = useSocialView(view('compose'),false);
  const [draftMentions,setDraftMentions]=useSocialView<string[]>(view(`draft-mentions:${visibility}`),[]);
  const [publishing, setPublishing] = useState(false);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [notices, setNotices] = useState<Notice[]>([]);
  const [noticeVisible, setNoticeVisible] = useState(true);
  const [commentDrafts, setCommentDrafts] = useSocialView<Record<string,string>>(view('comments'),{});
  const [expandedComments,setExpandedComments]=useSocialView<Record<string,boolean>>(view('expanded-comments'),{});
  const [replyTo, setReplyTo] = useSocialView<Record<string,Comment|undefined>>(view('replies'),{});
  const [commentMentions,setCommentMentions]=useSocialView<Record<string,string[]>>(view('comment-mentions'),{});
  const [userAvatar, setUserAvatar] = useState('');
  const [socialFeedAvatar, setSocialFeedAvatar] = useState('');
  const [avatarSubject, setAvatarSubject] = useState<'aning'|'k'|null>(null);
  const [personSubject, setPersonSubject] = useState<string|null>(null);
  const [forwardMoment,setForwardMoment]=useState<string|null>(null);
  const [openingPublicWall, setOpeningPublicWall] = useState(false);
  const feedEndRef = useRef<HTMLDivElement | null>(null);
  const publishAbortRef = useRef<AbortController | null>(null);
  const publishRequestRef = useRef(0);
  const feedRequestRef = useRef(0);
  const mentionVisibilityRef=useRef(visibility);
  useEffect(()=>{if(mentionVisibilityRef.current!==visibility){mentionVisibilityRef.current=visibility;setDraftMentions([]);}},[visibility,setDraftMentions]);

  const canOpenOwnerWall = !((window as any).Capacitor?.isNativePlatform?.())
    && ['127.0.0.1', 'localhost'].includes(window.location.hostname);

  useEffect(() => {
    let alive = true;
    fetch(`${api}/api/social-sites`, { headers: { 'X-MIRROW-Lounge-Admin': '1' }, cache: 'no-store' })
      .then(response => response.ok ? response.json() : Promise.reject(new Error('共域站点列表不可用')))
      .then(payload => { if (alive) setSites(payload.sites || []); })
      .catch(() => { if (alive) setSites([]); })
      .finally(()=>{if(alive)setSitesReady(true);});
    return () => { alive = false; };
  }, [api]);

  const openPublicWall = async () => {
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    setOpeningPublicWall(true);
    try {
      const response = await fetch(`${api}/api/social-feed/owner-ticket`, {
        method:'POST', headers:{'X-MIRROW-Owner-Pair':'1'}, cache:'no-store',
      });
      if (!response.ok) throw new Error('本机身份授权未完成，请在这台电脑的 MIRROW 页面重试。');
      const data = await response.json() as { ticket:string; origin:string };
      const target = `${data.origin}/#owner-ticket=${encodeURIComponent(data.ticket)}`;
      if (tab) tab.location.replace(target);
      else window.location.assign(target);
    } catch (e) {
      tab?.close();
      setError(e instanceof Error ? e.message : '无法打开共域公开页');
    } finally {
      setOpeningPublicWall(false);
    }
  };

  const loadAvatarSettings = useCallback(async () => {
    try {
      const [aningResponse, kResponse] = await Promise.all(['aning', 'k'].map(subject =>
        fetch(`${api}/api/social-feed/people/${subject}`)));
      if (!aningResponse.ok || !kResponse.ok) throw new Error('共域头像暂时不可用');
      const aning = await aningResponse.json() as {person:PublicPerson};
      const k = await kResponse.json() as {person:PublicPerson};
      setUserAvatar(aning.person.avatar || '');
      setSocialFeedAvatar(k.person.avatar || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : '共域头像暂时不可用');
    }
  }, [api]);

  // A publish is a one-shot write.  The UI may abandon the request, but it
  // must never retry automatically: after a timeout the server may have
  // accepted the write and the feed is the authority for checking it.
  useEffect(() => () => {
    publishRequestRef.current += 1;
    publishAbortRef.current?.abort();
    publishAbortRef.current = null;
  }, []);

  const setPendingAction = (momentId:string, action:string, value:boolean) => {
    const key = `${momentId}:${action}`;
    setPending(current => ({ ...current, [key]: value }));
  };
  const isPending = (momentId:string, action:string) => Boolean(pending[`${momentId}:${action}`]);

  const fetchFeedPage = useCallback(async (cursor:string | null, anchor:string) => {
    const params = new URLSearchParams({ limit:'30' });
    params.set('visibility_filter',visibilityFilter);
    if (cursor) params.set('cursor', cursor);
    else if (anchor) params.set('anchor_day', anchor);
    const response = await fetch(`${api}/api/social-feed/moments?${params.toString()}`);
    if (!response.ok) throw new Error('共域服务暂时不可用');
    return await response.json() as FeedResponse;
  }, [api,visibilityFilter]);

  const reload = useCallback(async (anchor = '') => {
    const requestId = ++feedRequestRef.current;
    setLoading(true);
    setLoadingMore(false);
    setError('');
    try {
      const page = await fetchFeedPage(null, anchor);
      if (requestId !== feedRequestRef.current) return;
      setItems(page.items || []);
      setNextCursor(page.next_cursor || null);
      setHasMore(Boolean(page.has_more));
      setCompletedFilter(visibilityFilter);
    } catch (e) {
      if (requestId !== feedRequestRef.current) return;
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      if (requestId === feedRequestRef.current) setLoading(false);
    }
  }, [fetchFeedPage]);

  const loadMore = useCallback(async () => {
    if (loading || loadingMore || !hasMore || !nextCursor) return;
    const requestId = feedRequestRef.current;
    setLoadingMore(true);
    try {
      const page = await fetchFeedPage(nextCursor, '');
      if (requestId !== feedRequestRef.current) return;
      setItems(current => {
        const known = new Set(current.map(item => item.id));
        return [...current, ...(page.items || []).filter(item => !known.has(item.id))];
      });
      setNextCursor(page.next_cursor || null);
      setHasMore(Boolean(page.has_more));
    } catch (e) {
      if (requestId !== feedRequestRef.current) return;
      setError(e instanceof Error ? e.message : '继续加载失败');
    } finally {
      if (requestId === feedRequestRef.current) setLoadingMore(false);
    }
  }, [fetchFeedPage, hasMore, loading, loadingMore, nextCursor]);

  const loadNotifications = useCallback(async () => {
    try {
      const response = await fetch(`${api}/api/social-feed/notifications`);
      if (!response.ok) throw new Error('共域提醒暂时不可用');
      const payload = await response.json() as { items?:Notice[] };
      const unread = payload.items || [];
      // Render the unread snapshot before marking those explicit IDs read.
      setNotices(unread);
      setNoticeVisible(unread.length > 0);
      if (unread.length) {
        await fetch(`${api}/api/social-feed/notifications/mark-read`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ notification_ids: unread.map(item => item.id) }),
        });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '提醒加载失败');
    }
  }, [api]);

  const place=useSocialReadingPlace(view('place:'+visibilityFilter),!loading&&!loadingMore&&completedFilter===visibilityFilter,items,hasMore,()=>void loadMore(),siteId==='');
  useEffect(()=>{
    if(sitesReady&&siteId&&siteId!==ALL_SOCIAL_SITES&&!sites.some(s=>s.id===siteId&&s.enabled&&s.has_human_key))setSiteId(ALL_SOCIAL_SITES);
  },[sitesReady,siteId,sites,setSiteId]);

  useEffect(() => {
    if(siteId)return;
    void reload();
    void loadNotifications();
    void loadAvatarSettings();
    return () => { feedRequestRef.current += 1; };
  }, [loadAvatarSettings, loadNotifications, reload,siteId]);

  useEffect(() => {
    const node = feedEndRef.current;
    if (!node) return undefined;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void loadMore();
    }, { rootMargin:'600px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadMore]);

  const jumpToDay = async (value:string) => {
    setAnchorDay(value);
    await reload(value);
  };

  const publish = async () => {
    const content = draft.trim(); if (!content) return;
    const requestId = ++publishRequestRef.current;
    const controller = new AbortController();
    publishAbortRef.current = controller;
    const timeoutId = window.setTimeout(() => controller.abort(), 15000);
    setPublishing(true); setError(''); setFeedback('');
    try {
      const response = await fetch(`${api}/api/social-feed/moments`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({content, visibility, ...(draftMentions.length?{mention_actor_ids:draftMentions}:{})}), signal: controller.signal,
      });
      if (!response.ok) throw new Error('发布失败');
      const created = await response.json() as Moment;
      if (requestId !== publishRequestRef.current) return;
      setItems(current => [created, ...current.filter(item => item.id !== created.id)]);
      setDraft(''); setDraftMentions([]); setComposeOpen(false); setFeedback('已发表，发布时间以共域记录为准。');
    } catch (e) {
      // A user-initiated close invalidates this request and intentionally
      // leaves no stale error in a newly opened compose sheet.  Timeout,
      // offline and other aborts while the sheet is still open are ambiguous:
      // ask for an authoritative feed check instead of claiming no write.
      if (requestId !== publishRequestRef.current) return;
      const message = controller.signal.aborted
        ? '发布超时/网络不可用，结果未确认，请联网后核对共域再重试'
        : (e instanceof Error ? e.message : '发布失败');
      setError(message);
    } finally {
      window.clearTimeout(timeoutId);
      if (publishRequestRef.current === requestId) {
        publishAbortRef.current = null;
        setPublishing(false);
      }
    }
  };

  const closeCompose = () => {
    // Invalidate the old request before aborting it, so its finally block
    // cannot clear state belonging to a later publish attempt.
    publishRequestRef.current += 1;
    publishAbortRef.current?.abort();
    publishAbortRef.current = null;
    setPublishing(false);
    setComposeOpen(false);
  };

  const cancelReply = (momentId:string) => {
    setReplyTo(current => ({ ...current, [momentId]: undefined }));
  };

  const comment = async (momentId:string) => {
    const content = (commentDrafts[momentId] || '').trim(); if (!content || isPending(momentId, 'comment')) return;
    const reply = replyTo[momentId];
    const temporary:Comment = { id:`pending-${Date.now()}`, author:'aning', content, reply_to_id:reply?.id || null, created_at:Date.now() / 1000 };
    setPendingAction(momentId, 'comment', true);
    setItems(current => current.map(item => item.id === momentId ? { ...item, comments:[...item.comments, temporary] } : item));
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(momentId)}/comments`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ content, reply_to_id: reply?.id || null, ...(commentMentions[momentId]?.length?{mention_actor_ids:commentMentions[momentId]}:{}) }),
      });
      if (!response.ok) throw new Error('评论失败');
      const created = await response.json() as Comment;
      setItems(current => current.map(item => item.id === momentId ? { ...item, comments:item.comments.map(row => row.id === temporary.id ? created : row) } : item));
      setCommentDrafts(current => ({...current, [momentId]:''}));
      setCommentMentions(current => ({...current, [momentId]:[]}));
      setReplyTo(current => ({...current, [momentId]:undefined}));
      setFeedback('评论已发出。');
    } catch (e) {
      setItems(current => current.map(item => item.id === momentId
        ? { ...item, comments:item.comments.filter(row => row.id !== temporary.id) }
        : item));
      setError(e instanceof Error ? e.message : '评论失败');
    } finally { setPendingAction(momentId, 'comment', false); }
  };

  const removeComment = async (momentId:string, commentId:string) => {
    if (commentId.startsWith('pending-') || isPending(momentId, `comment-delete:${commentId}`)) return;
    if (!window.confirm('删除这条评论？其他人的回复会保留，但不再指向这条评论。')) return;
    setPendingAction(momentId, `comment-delete:${commentId}`, true);
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(momentId)}/comments/${encodeURIComponent(commentId)}`, {method:'DELETE'});
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || '删除评论失败');
      }
      setItems(rows => rows.map(item => item.id === momentId ? {...item,
        comments:item.comments.filter(row => row.id !== commentId).map(row => row.reply_to_id === commentId ? {...row, reply_to_id:null} : row),
      } : item));
      setReplyTo(current => current[momentId]?.id === commentId ? {...current, [momentId]:undefined} : current);
      setFeedback('评论已删除。');
    } catch (e) { setError(e instanceof Error ? e.message : '删除评论失败'); }
    finally { setPendingAction(momentId, `comment-delete:${commentId}`, false); }
  };

  const like = async (momentId:string) => {
    if (isPending(momentId, 'like')) return;
    const current = items.find(item => item.id === momentId);
    if (!current) return;
    const liked = current.reactions.some(reaction => reaction.author === 'aning');
    setPendingAction(momentId, 'like', true);
    setItems(rows => rows.map(item => {
      if (item.id !== momentId) return item;
      const reactions = liked
        ? item.reactions.filter(reaction => reaction.author !== 'aning')
        : [...item.reactions, { author:'aning' as const, reaction:'like' as const, created_at:Date.now() / 1000 }];
      return { ...item, reactions };
    }));
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(momentId)}/like`, {method:'POST'});
      if (!response.ok) throw new Error('点赞失败');
      const result = await response.json() as { active?:boolean };
      if (typeof result.active === 'boolean') {
        setItems(rows => rows.map(item => {
          if (item.id !== momentId) return item;
          const withoutOwner = item.reactions.filter(reaction => reaction.author !== 'aning');
          return { ...item, reactions:result.active
            ? [...withoutOwner, { author:'aning' as const, reaction:'like' as const, created_at:Date.now() / 1000 }]
            : withoutOwner };
        }));
      }
      setFeedback(result.active ? '已点赞。' : '已取消点赞。');
    } catch (e) {
      setItems(rows => rows.map(item => {
        if (item.id !== momentId) return item;
        const withoutOwner = item.reactions.filter(reaction => reaction.author !== 'aning');
        return { ...item, reactions:liked
          ? [...withoutOwner, ...current.reactions.filter(reaction => reaction.author === 'aning')]
          : withoutOwner };
      }));
      setError(e instanceof Error ? e.message : '点赞失败');
    } finally { setPendingAction(momentId, 'like', false); }
  };

  const changeVisibility = async (momentId:string, next:Visibility) => {
    const previous = items.find(item => item.id === momentId)?.visibility;
    setPendingAction(momentId, 'visibility', true);
    setItems(rows => rows.map(item => item.id === momentId ? {...item, visibility:next} : item));
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(momentId)}/visibility`, {
        method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ visibility: next }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || '修改可见性失败');
      }
      setFeedback('可见范围已更新。');
    } catch (e) {
      if (previous) setItems(rows => rows.map(item => item.id === momentId ? {...item, visibility:previous} : item));
      setError(e instanceof Error ? e.message : '修改可见性失败');
    } finally { setPendingAction(momentId, 'visibility', false); }
  };

  const editMoment = async (moment:Moment) => {
    const content = window.prompt('修改这条动态', moment.content)?.trim();
    if (!content || content === moment.content || isPending(moment.id, 'edit')) return;
    setPendingAction(moment.id, 'edit', true);
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(moment.id)}`, {
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({content, revision:moment.revision || 1}),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail === 'revision_conflict' ? '这条动态已被别处修改，请刷新后重试' : payload.detail || '修改失败');
      }
      const updated = await response.json() as Moment;
      setItems(rows => rows.map(row => row.id === moment.id ? {...row, ...updated, people:updated.people || row.people} : row));
      setFeedback('动态已更新。');
    } catch (e) { setError(e instanceof Error ? e.message : '修改失败'); }
    finally { setPendingAction(moment.id, 'edit', false); }
  };

  const removeMoment = async (moment:Moment) => {
    if (!window.confirm('确认删除这条动态？评论和点赞也会一并删除。')) return;
    const originalIndex = items.findIndex(item => item.id === moment.id);
    setPendingAction(moment.id, 'delete', true);
    setItems(rows => rows.filter(item => item.id !== moment.id));
    try {
      const response = await fetch(`${api}/api/social-feed/moments/${encodeURIComponent(moment.id)}`, { method:'DELETE' });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || '删除失败');
      }
      setFeedback('动态已删除。');
    } catch (e) {
      setItems(rows => {
        if (rows.some(item => item.id === moment.id)) return rows;
        const restored = [...rows];
        restored.splice(Math.max(0, Math.min(originalIndex, restored.length)), 0, moment);
        return restored;
      });
      setError(e instanceof Error ? e.message : '删除失败');
    } finally { setPendingAction(moment.id, 'delete', false); }
  };

  const noticeRows = useMemo(() => notices.map(notice => {
    const detail = notice.kind === 'comment' ? notice.comment_content : notice.kind === 'moment' ? notice.moment_content : '';
    const suffix = detail ? `：“${String(detail).slice(0, 80)}”` : '';
    return `${notice.actor_name || who(notice.actor)}${kindText(notice.kind)}${suffix}`;
  }), [notices]);
  const filteredItems=useMemo(()=>items.filter(m=>visibilityFilter==='all'||m.visibility===visibilityFilter),[items,visibilityFilter]);
  const rowsWithDay = useMemo(() => filteredItems.map((moment, index) => ({
    moment,
    showDay: index === 0 || formatDay(moment.created_at) !== formatDay(filteredItems[index - 1].created_at),
  })), [filteredItems]);

  if(siteId===ALL_SOCIAL_SITES)return <SocialTimelinePage sites={sites} onSwitch={setSiteId} onBack={onBack}/>;
  if(siteId&&!sitesReady)return <div className="social-feed-page"><p role="status">正在核对已注册共域…</p></div>;
  const selectedSite = sites.find(site => site.id === siteId && site.enabled && site.has_human_key);
  if (selectedSite) return <SocialRemoteFeed key={selectedSite.id} site={selectedSite} sites={sites} onSwitch={setSiteId} onBack={onBack} />;

  return <div className="social-feed-page">
    <header className="social-feed-header">
      <button className="social-feed-back" onClick={onBack} aria-label="返回">‹</button>
      <div className="social-feed-heading"><h2>共域</h2><span>MIRROW·Comment</span></div>
      <div className="social-feed-header-actions">
        <span data-social-music-slot />
        <SocialIdentityManager initialSubject={avatarSubject} onClose={() => setAvatarSubject(null)} onSaved={() => { void loadAvatarSettings(); void reload(); }} />
      </div>
    </header>
    <SocialFeedFilters sites={sites} current="" onSwitch={setSiteId} value={visibilityFilter} onChange={setVisibilityFilter}/>
    <main className="social-feed-body" ref={place.body}>
      <label className="social-feed-day-locator">定位日期<input className="social-feed-date" type="date" value={anchorDay} onChange={e => void jumpToDay(e.target.value)} /></label>
      <section className="social-timeline-shelf"><button type="button" aria-expanded={shelfOpen} onClick={()=>setShelfOpen(v=>!v)}>✧ 家里的奇物架与空间管理 {shelfOpen?'⌃':'⌄'}</button><SocialDecorPanel compact={!shelfOpen}/></section>
      <div className="social-feed-public-entrance">
        {canOpenOwnerWall && <button type="button" className="social-feed-public-link" onClick={() => void openPublicWall()} disabled={openingPublicWall}>{openingPublicWall ? '正在打开共享墙…' : '以站主身份查看共享墙 ↗'}</button>}
      </div>
      {noticeVisible && noticeRows.length > 0 && <section className="social-feed-notice" aria-label="共域新动态">
        <div className="social-feed-notice-icon" aria-hidden="true">💌</div>
        <div className="social-feed-notice-copy"><strong>有新动静</strong><div className="social-feed-notice-list">{noticeRows.map((row, index) => <div key={`${row}-${index}`}>{row}</div>)}</div></div>
        <button type="button" className="social-feed-notice-close" onClick={() => setNoticeVisible(false)} aria-label="收起新动态提醒">×</button>
      </section>}
      {error && <div className="social-feed-error" role="alert">{error}</div>}
      {feedback && <div className="social-feed-feedback" role="status">✓ {feedback}</div>}
      {place.notice&&<p role="status">{place.notice}</p>}
      {loading ? <div className="social-feed-loading">正在加载共域……</div> : filteredItems.length === 0 ? <div className="social-feed-empty">当前筛选没有动态。</div> : rowsWithDay.map(({ moment, showDay }) => {
        const liked = moment.reactions.some(reaction => reaction.author === 'aning');
        const visibilityPending = isPending(moment.id, 'visibility');
        return <div key={moment.id} data-social-view-post={moment.id}>
          {showDay && <div className="social-feed-day-separator"><span>{formatDay(moment.created_at)}</span></div>}
          <article data-social-post-author={moment.author} className={`social-feed-card social-feed-card-${moment.author === 'k' || moment.author === 'aning' ? moment.author : 'friend'}`}>
            <div className="social-feed-card-head"><FeedAvatar author={moment.author} onClick={moment.author === 'k' || moment.author === 'aning' ? () => setAvatarSubject(moment.author as 'k'|'aning') : () => setPersonSubject(moment.author)} value={moment.author === 'k' ? socialFeedAvatar : moment.author === 'aning' ? userAvatar : moment.people?.[moment.author]?.avatar || ''} /><span className="social-feed-author">{named(moment, moment.author)}</span>{moment.author === 'k' || moment.author === 'aning' ? <select className="social-feed-visibility-control" value={moment.visibility} disabled={visibilityPending} onChange={e => void changeVisibility(moment.id, e.target.value as Visibility)} aria-label={`${named(moment, moment.author)}共域可见性`} title={moment.visibility==='public'?'可与朋友节点分享':'仅站主与 AI 可见'}><option value="private">🔒 私密</option><option value="public">🌐 公开</option></select> : <span className="social-feed-visibility-control">🌐 公开</span>}<time className="social-feed-time">{formatTime(moment.created_at)}</time></div>
            <div className="social-feed-content">{moment.content}</div>
            <SocialForwardCard forward={moment.forward} name={id=>named(moment,id)}/>
            {moment.mention_actor_ids?.length? <div className="social-mention-chips">{moment.mention_actor_ids.map(id=>moment.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPersonSubject(id)}>@{moment.people[id].name}</button>)}</div>:null}
            <div className="social-feed-actions" role="group" aria-label="共域互动">
              <button className={`social-feed-action ${liked?'active':''}`} disabled={isPending(moment.id, 'like')} onClick={() => void like(moment.id)} aria-label={liked?'取消点赞':'点赞'}><span aria-hidden="true">♥</span><span>{isPending(moment.id, 'like') ? '处理中…' : liked ? `已赞 ${moment.reactions.length}` : moment.reactions.length || '赞'}</span></button>
              <button className="social-feed-action" onClick={() => document.getElementById(`comment-${moment.id}`)?.focus()}><span aria-hidden="true">◌</span><span>{moment.comments.length ? `${moment.comments.length} 条评论` : '评论'}</span></button>
              {moment.visibility==='public'&&!moment.forward&&<button type="button" className="social-feed-action social-feed-action-forward" onClick={()=>setForwardMoment(moment.id)}>↗ 转发</button>}
              {moment.author!=='k'&&<details className="social-feed-manage-actions"><summary>更多</summary><div className="social-feed-manage-menu">
              {moment.author==='aning' && <button className="social-feed-action" disabled={isPending(moment.id, 'edit')} onClick={() => void editMoment(moment)}><span aria-hidden="true">✎</span><span>{isPending(moment.id, 'edit') ? '修改中…' : '编辑'}</span></button>}
              {moment.author!=='k' && <button className="social-feed-action social-feed-delete" disabled={isPending(moment.id, 'delete')} onClick={() => void removeMoment(moment)}><span aria-hidden="true">⌫</span><span>{isPending(moment.id, 'delete') ? '删除中…' : moment.author === 'aning' ? '删除' : '移除'}</span></button>}
              </div></details>}
            </div>
            {moment.reactions.length > 0 && <div className="social-feed-liked-by" aria-label="点赞的人"><span aria-hidden="true">♥</span>{moment.reactions.map(reaction=>named(moment, reaction.author)).join('、')}</div>}
            {moment.comments.length > 0 && <div className="social-feed-comments">{(expandedComments[moment.id]?moment.comments:moment.comments.slice(-3)).map(commentRow => {
              const parent = commentRow.reply_to_id ? moment.comments.find(row=>row.id===commentRow.reply_to_id) : undefined;
              return <div className={`social-feed-comment ${commentRow.id.startsWith('pending-') ? 'pending' : ''}`} key={commentRow.id}>
                <FeedAvatar author={commentRow.author} onClick={commentRow.author === 'k' || commentRow.author === 'aning' ? () => setAvatarSubject(commentRow.author as 'k'|'aning') : () => setPersonSubject(commentRow.author)} value={commentRow.author === 'k' ? socialFeedAvatar : commentRow.author === 'aning' ? userAvatar : moment.people?.[commentRow.author]?.avatar || ''} className="social-feed-comment-avatar" />
                <div className="social-feed-comment-body"><div className="social-feed-comment-meta"><strong>{named(moment, commentRow.author)}</strong>{parent&&<span>回复 {named(moment, parent.author)}</span>}<time>{formatTime(commentRow.created_at)}</time></div><p className="social-feed-comment-text">{commentRow.content}</p>{commentRow.mention_actor_ids?.length?<div className="social-mention-chips">{commentRow.mention_actor_ids.map(id=>moment.people?.[id]?.name&&<button type="button" className="social-mention-chip" key={id} onClick={()=>setPersonSubject(id)}>@{moment.people[id].name}</button>)}</div>:null}<div className="social-feed-comment-tools"><button type="button" onClick={() => { setReplyTo(current => ({...current,[moment.id]:commentRow})); document.getElementById(`comment-${moment.id}`)?.focus(); }}>回复</button>{!commentRow.id.startsWith('pending-') && commentRow.can_delete !== false && <button type="button" disabled={isPending(moment.id, `comment-delete:${commentRow.id}`)} onClick={() => void removeComment(moment.id, commentRow.id)}>{isPending(moment.id, `comment-delete:${commentRow.id}`) ? '删除中…' : '删除'}</button>}</div></div>
              </div>;
            })}</div>}
            {moment.comments.length>3&&<button type="button" className="social-comments-toggle" onClick={()=>setExpandedComments(current=>({...current,[moment.id]:!current[moment.id]}))}>{expandedComments[moment.id]?'收起评论':`展开全部 ${moment.comments.length} 条评论`}</button>}
            {replyTo[moment.id] && <div className="social-feed-replying" role="status"><span>正在回复 {named(moment, replyTo[moment.id]!.author)} ·</span><button type="button" onClick={() => cancelReply(moment.id)} aria-label="取消回复">取消 ×</button></div>}
            <div className="social-feed-comment-box"><SocialMentions compact endpoint={`${api}/api/social-feed/mention-people?visibility=${moment.visibility}`} selected={commentMentions[moment.id]||[]} onChange={ids=>setCommentMentions(current=>({...current,[moment.id]:ids}))} disabled={isPending(moment.id,'comment')} onPerson={setPersonSubject}/><input id={`comment-${moment.id}`} maxLength={600} value={commentDrafts[moment.id]||''} onChange={e => setCommentDrafts(current => ({...current,[moment.id]:e.target.value}))} placeholder={replyTo[moment.id]?`回复 ${named(moment, replyTo[moment.id]!.author)}`:'写下一句……'} /><button type="button" disabled={!commentDrafts[moment.id]?.trim() || isPending(moment.id, 'comment')} onClick={() => void comment(moment.id)}>{isPending(moment.id, 'comment')?'发送中…':'发送'}</button></div>
            <SocialSourceLabel name="本家" hosted={moment.author.startsWith('visitor:')}/>
          </article>
        </div>;
      })}
      <div ref={feedEndRef} className="social-feed-load-sentinel" aria-hidden="true" />
      {!loading && loadingMore && <div className="social-feed-loading-more">正在加载更早的动态……</div>}
      {!loading && !loadingMore && !hasMore && items.length > 0 && <div className="social-feed-end">已经看到最早的动态了</div>}
    </main>
    <button className="social-feed-fab" type="button" onClick={() => { setError(''); setComposeOpen(true); }} aria-label="发动态">＋</button>
    {forwardMoment&&<SocialForwardDialog momentId={forwardMoment} onClose={()=>setForwardMoment(null)}/>}
    {personSubject && <SocialPersonCard actorId={personSubject} onClose={() => setPersonSubject(null)}
      onSaved={(person:SocialPerson) => setItems(rows => rows.map(row => row.people?.[person.actor_id]
        ? {...row, people:{...row.people, [person.actor_id]:person}} : row))} />}
    {composeOpen && <div className="social-feed-compose-backdrop" onClick={closeCompose}>
      <section className="social-feed-compose-sheet" role="dialog" aria-modal="true" aria-label="发共域动态" onClick={e => e.stopPropagation()}>
        <header><div><h3>发一条动态</h3><small>发布于今天 · {today()}</small></div><button type="button" onClick={closeCompose} aria-label="关闭">×</button></header>
        <textarea autoFocus value={draft} maxLength={1200} onChange={e => setDraft(e.target.value)} placeholder="这一刻想分享什么……" /><SocialMentions endpoint={`${api}/api/social-feed/mention-people?visibility=${visibility}`} selected={draftMentions} onChange={setDraftMentions} disabled={publishing} onPerson={setPersonSubject}/>
        {error && <div className="social-feed-compose-error" role="alert">{error}</div>}
        <footer><select value={visibility} onChange={e => setVisibility(e.target.value as Visibility)} aria-label="发布权限"><option value="private">🔒 私密 · 仅站主与 AI</option><option value="public">🌐 公开 · 可与朋友节点分享</option></select><button className="social-feed-publish" type="button" onClick={publish} disabled={!draft.trim() || publishing}>{publishing?'发布中…':'发表'}</button></footer>
      </section>
    </div>}
  </div>;
}
