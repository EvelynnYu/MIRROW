import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getApiBase } from '../config';
import { apiFetch } from '../api';
import { useModalFocus } from '../hooks/useModalFocus';
import { usePersona } from '../persona';
import './WishBoardModal.css';

type WishStatus = 'open' | 'in_progress' | 'impossible_pending' | 'impossible_kept' | 'fulfilled' | 'deleted';
type WishTab = 'open' | 'in_progress' | 'impossible' | 'fulfilled' | 'deleted';

type WishComment = {
  id: number;
  wish_id: number;
  author: 'k' | 'user' | 'system' | string;
  content: string;
  reply_to_comment_id?: number | null;
  created_at: string;
};

type Wish = {
  id: number;
  feature: string;
  reason?: string;
  status: WishStatus | string;
  times_wished: number;
  first_wished_at?: string;
  last_wished_at?: string;
  updated_at?: string;
  latest_comments?: WishComment[];
  all_comments?: WishComment[];
};

export type WishBoardFocus = {
  target_wish_id?: number | string | null;
  wish_id?: number | string | null;
  change_kind?: string;
  kind?: string;
  before?: unknown;
  after?: unknown;
  title?: string;
};

const STATUS_LABELS: Record<string, string> = {
  open: '许愿中',
  in_progress: '正在努力实现中',
  impossible_pending: '天方夜谭 · 等待决定',
  impossible_kept: '天方夜谭 · 已保留',
  fulfilled: '已实现',
  deleted: '删除记录',
};

const TAB_ORDER: WishTab[] = ['open', 'in_progress', 'impossible', 'fulfilled', 'deleted'];
const TAB_LABELS: Record<WishTab, string> = {
  open: '许愿中',
  in_progress: '正在努力实现中',
  impossible: '天方夜谭',
  fulfilled: '已实现',
  deleted: '删除记录',
};
const TAB_STATUSES: Record<WishTab, WishStatus[]> = {
  open: ['open'],
  in_progress: ['in_progress'],
  impossible: ['impossible_pending', 'impossible_kept'],
  fulfilled: ['fulfilled'],
  deleted: ['deleted'],
};

function tabForWishStatus(status?: string): WishTab {
  if (status === 'in_progress') return 'in_progress';
  if (status === 'impossible_pending' || status === 'impossible_kept') return 'impossible';
  if (status === 'fulfilled') return 'fulfilled';
  if (status === 'deleted') return 'deleted';
  return 'open';
}

function focusBadge(focus?: WishBoardFocus | null): string {
  const kind = focus?.change_kind || focus?.kind;
  if (kind === 'reaffirm') return '+1';
  if (kind === 'comment' || kind === 'create') return 'New';
  return '更新';
}

function formatDate(value?: string): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString([], { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function authorLabel(author: string, aiName: string, userName: string): string {
  return author === 'user' ? userName : author === 'k' ? aiName : '系统';
}

export default function WishBoardModal({ open, onClose, focus }: { open: boolean; onClose: () => void; focus?: WishBoardFocus | null }) {
  const { aiName, userName } = usePersona();
  useModalFocus(open, '许愿板');
  const contentRef = useRef<HTMLDivElement>(null);
  const [wishes, setWishes] = useState<Wish[]>([]);
  const [activeTab, setActiveTab] = useState<WishTab>('open');
  const [expandedComments, setExpandedComments] = useState<Set<number>>(new Set());
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [editingCommentId, setEditingCommentId] = useState<number | null>(null);
  const [editCommentDraft, setEditCommentDraft] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string>('');
  const [error, setError] = useState('');
  const [highlightWishId, setHighlightWishId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await apiFetch(`${getApiBase()}/api/wander/wishes`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      setWishes(Array.isArray(data.wishes) ? data.wishes : []);
    } catch (err: any) {
      setError(err?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
    else setHighlightWishId(null);
  }, [open, load]);

  useEffect(() => {
    if (!open || !focus) return undefined;
    const rawId = focus.target_wish_id ?? focus.wish_id;
    const targetId = Number(rawId);
    if (!Number.isInteger(targetId) || targetId <= 0) return undefined;
    const target = wishes.find(wish => wish.id === targetId);
    if (!target) return undefined;
    setActiveTab(tabForWishStatus(target.status));
    setHighlightWishId(targetId);
    if ((focus.change_kind || focus.kind) === 'comment') {
      setExpandedComments(previous => new Set(previous).add(targetId));
    }
    const scrollTimer = window.setTimeout(() => {
      document.getElementById(`wish-card-${targetId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 80);
    const clearTimer = window.setTimeout(() => setHighlightWishId(null), 3200);
    return () => { window.clearTimeout(scrollTimer); window.clearTimeout(clearTimer); };
  }, [focus, open, wishes]);

  useEffect(() => {
    if (open) contentRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  }, [activeTab, open]);

  const visibleWishes = useMemo(
    () => wishes.filter(wish => TAB_STATUSES[activeTab].includes((wish.status || 'open') as WishStatus)),
    [activeTab, wishes],
  );

  const updateStatus = async (wish: Wish, status: WishStatus) => {
    if (status === 'fulfilled' && !window.confirm(`确认将「${wish.feature}」标记为已实现吗？`)) return;
    const key = `status:${wish.id}`;
    setSaving(key);
    try {
      const response = await apiFetch(`${getApiBase()}/api/wander/wishes/${wish.id}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || `HTTP ${response.status}`);
      await load();
    } catch (err: any) {
      setError(err?.message || '状态更新失败');
    } finally {
      setSaving('');
    }
  };

  const editComment = async (comment: WishComment) => {
    const content = editCommentDraft.trim();
    if (!content) return;
    const key = `edit-comment:${comment.id}`;
    setSaving(key);
    try {
      const response = await apiFetch(`${getApiBase()}/api/wander/wishes/comments/${comment.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || `HTTP ${response.status}`);
      setEditingCommentId(null);
      setEditCommentDraft('');
      await load();
    } catch (err: any) {
      setError(err?.message || '评论编辑失败');
    } finally {
      setSaving('');
    }
  };

  const deleteComment = async (comment: WishComment) => {
    if (!window.confirm('确认删除这条 comment 吗？')) return;
    const key = `delete-comment:${comment.id}`;
    setSaving(key);
    try {
      const response = await apiFetch(`${getApiBase()}/api/wander/wishes/comments/${comment.id}`, { method: 'DELETE' });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || `HTTP ${response.status}`);
      if (editingCommentId === comment.id) {
        setEditingCommentId(null);
        setEditCommentDraft('');
      }
      await load();
    } catch (err: any) {
      setError(err?.message || '评论删除失败');
    } finally {
      setSaving('');
    }
  };

  const addComment = async (wish: Wish) => {
    const content = (drafts[wish.id] || '').trim();
    if (!content) return;
    const key = `comment:${wish.id}`;
    setSaving(key);
    try {
      const response = await apiFetch(`${getApiBase()}/api/wander/wishes/${wish.id}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || `HTTP ${response.status}`);
      setDrafts(prev => ({ ...prev, [wish.id]: '' }));
      await load();
    } catch (err: any) {
      setError(err?.message || '评论保存失败');
    } finally {
      setSaving('');
    }
  };

  if (!open) return null;

  return (
    <div className="ledger-overlay wish-board-overlay" onClick={onClose}>
      <div className="ledger-modal wish-board-modal" role="dialog" aria-modal="true" aria-label="许愿板" onClick={event => event.stopPropagation()}>
        <div className="ledger-title wish-board-modal-title">
          <span>🌠 {aiName} 的许愿板</span>
          <button className="ledger-close-btn" onClick={onClose} aria-label="关闭许愿板">✕</button>
        </div>
        <div className="wish-board-tabs" role="tablist" aria-label="许愿状态">
          {TAB_ORDER.map(tab => {
            const count = wishes.filter(wish => TAB_STATUSES[tab].includes((wish.status || 'open') as WishStatus)).length;
            return (
              <button
                key={tab}
                className={`wish-board-tab ${activeTab === tab ? 'active' : ''}`}
                onClick={() => setActiveTab(tab)}
                role="tab"
                aria-selected={activeTab === tab}
              >
                {TAB_LABELS[tab]} <span>{count}</span>
              </button>
            );
          })}
        </div>
        <div className="wish-board-content" ref={contentRef}>
          {error && <div className="wish-board-modal-error">{error}<button onClick={() => setError('')}>×</button></div>}
          {loading ? (
            <div className="wish-board-modal-empty">许愿板加载中…</div>
          ) : visibleWishes.length === 0 ? (
            <div className="wish-board-modal-empty">这个分区还没有愿望。</div>
          ) : (
            <div className="wish-sticky-grid">
              {visibleWishes.map(wish => {
              const comments = wish.all_comments || [];
              const shownComments = expandedComments.has(wish.id) ? comments : (wish.latest_comments || comments.slice(-2));
              const isGrey = wish.status === 'impossible_kept' || wish.status === 'deleted';
              const terminal = wish.status === 'fulfilled' || wish.status === 'deleted';
              const focusedKind = highlightWishId === wish.id ? (focus?.change_kind || focus?.kind) : '';
              const newestKCommentId = [...comments].reverse().find(comment => comment.author === 'k')?.id;
              const statusButtons: Array<[WishStatus, string]> = [
                ['fulfilled', '已实现'], ['in_progress', '努力中'], ['impossible_pending', '天方夜谭'],
              ];
              return (
                <article id={`wish-card-${wish.id}`} key={wish.id} className={`wish-sticky-card ${isGrey ? 'grey' : ''}${highlightWishId === wish.id ? ' wish-sticky-card-highlight' : ''}`}>
                  <div className="wish-sticky-pin">✦</div>
                  <div className="wish-sticky-head">
                    <h3>{wish.feature}{focusedKind === 'create' && <em className="wish-update-bubble">{focusBadge(focus)}</em>}</h3>
                    <span className="wish-sticky-status">{focusedKind === 'status' && <em className="wish-update-bubble">{focusBadge(focus)}</em>}{STATUS_LABELS[wish.status] || wish.status}</span>
                  </div>
                  {wish.reason && <p className="wish-sticky-reason">{wish.reason}</p>}
                  <div className="wish-sticky-meta">
                    <span className="wish-count-position">许愿 {wish.times_wished || 0} 次{focusedKind === 'reaffirm' && <em className="wish-update-bubble">{focusBadge(focus)}</em>}</span>
                    <span>最近 {formatDate(wish.last_wished_at || wish.updated_at)}</span>
                  </div>
                  {!terminal && (
                    <div className="wish-sticky-actions" aria-label="标记愿望状态">
                      {statusButtons.map(([status, label]) => (
                        <button
                          key={status}
                          className={(wish.status === status || (status === 'impossible_pending' && wish.status === 'impossible_kept')) ? 'active' : ''}
                          disabled={saving === `status:${wish.id}`}
                          onClick={() => void updateStatus(wish, status)}
                        >{label}</button>
                      ))}
                    </div>
                  )}
                  {wish.status === 'impossible_pending' && <div className="wish-pending-note">等 {aiName} 下次自省时决定保留或删除</div>}
                  {comments.length > 0 && (
                    <div className="wish-comment-thread">
                      {shownComments.map(comment => (
                        <div key={comment.id} className={`wish-comment ${comment.author === 'k' ? 'from-k' : ''}`}>
                          <div className="wish-comment-header">
                            <span className="wish-comment-author">{authorLabel(comment.author, aiName, userName)}</span>
                            {focusedKind === 'comment' && comment.id === newestKCommentId && <em className="wish-update-bubble">New</em>}
                            <time className="wish-comment-time" dateTime={comment.created_at}>{formatDate(comment.created_at)}</time>
                          </div>
                          {editingCommentId === comment.id ? (
                            <div className="wish-comment-edit-row">
                              <input
                                value={editCommentDraft}
                                onChange={event => setEditCommentDraft(event.target.value)}
                                onKeyDown={event => { if (event.key === 'Enter') void editComment(comment); }}
                                autoFocus
                              />
                              <button disabled={saving === `edit-comment:${comment.id}` || !editCommentDraft.trim()} onClick={() => void editComment(comment)}>保存</button>
                              <button onClick={() => { setEditingCommentId(null); setEditCommentDraft(''); }}>取消</button>
                            </div>
                          ) : (
                            <>
                              <span className="wish-comment-content">{comment.content}</span>
                              {comment.author === 'user' && (
                                <span className="wish-comment-actions">
                                  <button onClick={() => { setEditingCommentId(comment.id); setEditCommentDraft(comment.content); }}>编辑</button>
                                  <button disabled={saving === `delete-comment:${comment.id}`} onClick={() => void deleteComment(comment)}>删除</button>
                                </span>
                              )}
                            </>
                          )}
                        </div>
                      ))}
                      {comments.length > 2 && (
                        <button className="wish-comments-toggle" onClick={() => setExpandedComments(prev => {
                          const next = new Set(prev);
                          if (next.has(wish.id)) next.delete(wish.id); else next.add(wish.id);
                          return next;
                        })}>
                          {expandedComments.has(wish.id) ? '收起评论' : `展开全部 ${comments.length} 条评论`}
                        </button>
                      )}
                    </div>
                  )}
                  <div className="wish-comment-compose">
                    <input
                      value={drafts[wish.id] || ''}
                      onChange={event => setDrafts(prev => ({ ...prev, [wish.id]: event.target.value }))}
                      onKeyDown={event => { if (event.key === 'Enter') void addComment(wish); }}
                      placeholder="写一句 comment…"
                    />
                    <button disabled={saving === `comment:${wish.id}` || !(drafts[wish.id] || '').trim()} onClick={() => void addComment(wish)}>
                      {saving === `comment:${wish.id}` ? '…' : '发送'}
                    </button>
                  </div>
                </article>
              );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
