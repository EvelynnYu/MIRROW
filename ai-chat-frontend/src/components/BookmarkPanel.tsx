import { useState } from 'react';
import { getApiBase } from '../config';
import { stripWikilinks } from '../utils';
import type { useBookmark } from '../hooks/useBookmark';
import XhsSavedPosts from './XhsSavedPosts';
import XhsDirectMessages from './XhsDirectMessages';

function BookmarkPanel({ bookmark, onClose }: { bookmark: ReturnType<typeof useBookmark>; onClose: () => void }) {
  const [showXhsMessages, setShowXhsMessages] = useState(false);
  const [showXhsPosts, setShowXhsPosts] = useState(false);
  const {
    bookmarkList, setBookmarkList, bookmarkTab, setBookmarkTab,
    bookmarkPage, setBookmarkPage, bookmarkTotal, setBookmarkTotal,
    expandedBookmarkContent, setExpandedBookmarkContent,
    bookmarkContextWindow, setBookmarkContextWindow,
    contextPopup, setContextPopup,
    setBookmarkedIds, setBookmarkBucketMap,
    fetchBookmarkList, BOOKMARK_PAGE_SIZE,
  } = bookmark;

  return (
    <>
      <div className="ledger-overlay" onClick={onClose}>
        <div className="ledger-modal" style={{maxWidth:560}} onClick={e => e.stopPropagation()}>
          <div className="ledger-title">
            <span>⭐ 收藏夹</span>
            <button className="ledger-close-btn" onClick={onClose}>✕</button>
          </div>
          <div className="bookmark-tabs">
            <button className={`bookmark-tab ${!showXhsPosts&&!showXhsMessages && bookmarkTab==='user'?'active':''}`} onClick={() => { setShowXhsPosts(false);setShowXhsMessages(false); setBookmarkTab('user'); setBookmarkPage(0); fetchBookmarkList('user', 0); }}>我的收藏</button>
            <button className={`bookmark-tab ${!showXhsPosts&&!showXhsMessages && bookmarkTab==='k'?'active':''}`} onClick={() => { setShowXhsPosts(false);setShowXhsMessages(false); setBookmarkTab('k'); setBookmarkPage(0); fetchBookmarkList('k', 0); }}>AI的收藏</button>
            <button className={`bookmark-tab ${showXhsPosts?'active':''}`} onClick={() => {setShowXhsPosts(true);setShowXhsMessages(false);}}>🍠 AI 留帖</button>
            <button className={`bookmark-tab ${showXhsMessages?'active':''}`} onClick={() => {setShowXhsMessages(true);setShowXhsPosts(false);}}>🍠 私信</button>
          </div>
          {showXhsMessages ? <XhsDirectMessages /> : showXhsPosts ? <XhsSavedPosts /> : <>
          <div className="bookmark-context-slider">
            <span>漫想翻阅上下文: ±{bookmarkContextWindow} 轮</span>
            <input type="range" min="1" max="10" value={bookmarkContextWindow}
              onChange={e => {
                const v = parseInt(e.target.value);
                setBookmarkContextWindow(v);
                localStorage.setItem('bookmarkContextWindow', String(v));
                fetch(`${getApiBase()}/api/settings/bookmark-context-window`, {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({value: v}),
                }).catch(() => {});
              }} />
          </div>
          {bookmarkList.length === 0 ? (
            <div className="ledger-empty">暂无收藏</div>
          ) : (
            <div className="ledger-list">
              {bookmarkList.map(b => (
                <div key={b.id} className={`bookmark-bubble ${b.collected_by==='k'?'by-k':'by-user'}`}>
                  <span className="bookmark-badge">{b.collected_by==='k'?'AI':'我'}</span>
                  {(() => {
                    const raw = stripWikilinks((b.content || '').replace(/^\[收藏\] /, ''));
                    const isLong = raw.length > 120;
                    const isExpanded = expandedBookmarkContent.has(b.id);
                    return (
                      <span className="bookmark-content">
                        {(!isLong || isExpanded) ? raw : raw.slice(0, 120) + '...'}
                        {isLong && (
                          <button className="bookmark-expand-toggle" onClick={(e) => {
                            e.stopPropagation();
                            setExpandedBookmarkContent(prev => {
                              const n = new Set(prev);
                              isExpanded ? n.delete(b.id) : n.add(b.id);
                              return n;
                            });
                          }}>{isExpanded ? '收起' : '展开'}</button>
                        )}
                      </span>
                    );
                  })()}
                  <span className="bookmark-meta">{b.original_timestamp?.slice(0,16)?.replace('T',' ') || ''}</span>
                  <div className="bookmark-actions">
                    <button className="bookmark-ctx-btn" title="查看上下文" onClick={async () => {
                      const raw2 = stripWikilinks((b.content || '').replace(/^\[收藏\] /, ''));
                      setContextPopup({ bookmarkId: b.id, bookmarkContent: raw2.slice(0, 30), before: 3, after: 3, data: null, showRangeMenu: false });
                      try {
                        const res = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(b.id)}/context?before=3&after=3`);
                        const data = await res.json();
                        setContextPopup(prev => {
                          if (!prev || prev.bookmarkId !== b.id) return prev;
                          return { ...prev, data: { before: data.messages_before || [], target: data.target_message, after: data.messages_after || [], hasMoreBefore: data.has_more_before, hasMoreAfter: data.has_more_after } };
                        });
                      } catch { setContextPopup(prev => !prev || prev.bookmarkId !== b.id ? prev : { ...prev, data: null }); }
                    }}>📋</button>
                    {!b.original_msg_id && (
                      <button className="bookmark-ctx-btn" title="补全上下文（自动定位原文）" style={{fontSize:'0.7rem'}} onClick={async (e) => {
                        e.stopPropagation();
                        try {
                          const res = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(b.id)}/ensure-context`, { method: 'POST' });
                          const data = await res.json();
                          if (data.success) {
                            setBookmarkList(prev => prev.map(x => x.id === b.id ? {...x, original_msg_id: data.original_msg_id, original_timestamp: data.original_timestamp} : x));
                            const cp = contextPopup;
                            if (cp?.bookmarkId === b.id) {
                              const { before, after } = cp!;
                              const ctxRes = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(b.id)}/context?before=${before}&after=${after}`);
                              const ctxData = await ctxRes.json();
                              setContextPopup(prev => {
                                if (!prev || prev.bookmarkId !== b.id) return prev;
                                return { ...prev, data: { before: ctxData.messages_before || [], target: ctxData.target_message, after: ctxData.messages_after || [], hasMoreBefore: ctxData.has_more_before, hasMoreAfter: ctxData.has_more_after } };
                              });
                            }
                          }
                        } catch { /* 静默失败 */ }
                      }}>🔗</button>
                    )}
                    <button className="ledger-del-btn" title="取消收藏" onClick={async () => {
                      try {
                        const res = await fetch(`${getApiBase()}/api/bookmarks/` + encodeURIComponent(b.id), { method: 'DELETE' });
                        if (!res.ok) throw new Error('删除失败');
                        setBookmarkList(prev => prev.filter(x => x.id !== b.id));
                        setBookmarkTotal(prev => prev - 1);
                        if (b.original_msg_id) {
                          setBookmarkedIds(prev => { const n = new Set(prev); n.delete(b.original_msg_id); return n; });
                          setBookmarkBucketMap(prev => { const m = {...prev}; delete m[b.original_msg_id!]; return m; });
                        }
                      } catch { /* 静默失败，下次刷新列表时恢复 */ }
                    }}>✕</button>
                  </div>
                </div>
              ))}
            </div>
          )}
          {bookmarkTotal > BOOKMARK_PAGE_SIZE && (
            <div className="bookmark-pagination">
              <button disabled={bookmarkPage===0} onClick={() => { const p = bookmarkPage-1; setBookmarkPage(p); fetchBookmarkList(bookmarkTab, p); }}>‹ 上一页</button>
              <span>{bookmarkPage+1}/{Math.ceil(bookmarkTotal/BOOKMARK_PAGE_SIZE)}</span>
              <button disabled={(bookmarkPage+1)*BOOKMARK_PAGE_SIZE >= bookmarkTotal} onClick={() => { const p = bookmarkPage+1; setBookmarkPage(p); fetchBookmarkList(bookmarkTab, p); }}>下一页 ›</button>
            </div>
          )}
          </>}
        </div>
      </div>

      {contextPopup && (
        <div className="context-popup-overlay" onClick={() => setContextPopup(null)}>
          <div className="context-popup" onClick={e => e.stopPropagation()}>
            <div className="context-popup-header">
              <span>⭐ 上下文 · {contextPopup.bookmarkContent}{contextPopup.bookmarkContent.length >= 30 ? '…' : ''}</span>
              <button className="context-popup-close" onClick={() => setContextPopup(null)}>✕</button>
            </div>
            <div className="context-popup-messages">
              {!contextPopup.data ? (
                <div className="context-loading">加载中…</div>
              ) : !contextPopup.data.target ? (
                <div className="context-loading">未找到上下文消息</div>
              ) : (
                <>
                  {contextPopup.data.hasMoreBefore && (
                    <button className="context-expand-btn" onClick={async () => {
                      const nb = contextPopup.before + 3;
                      const bId = contextPopup.bookmarkId;
                      setContextPopup(prev => prev ? { ...prev, before: nb } : null);
                      try {
                        const res = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(bId)}/context?before=${nb}&after=${contextPopup.after}`);
                        const data = await res.json();
                        setContextPopup(prev => {
                          if (!prev || prev.bookmarkId !== bId || !prev.data) return prev;
                          return { ...prev, data: {
                            ...prev.data,
                            before: data.messages_before || [],
                            hasMoreBefore: data.has_more_before,
                          }};
                        });
                      } catch {}
                    }}>↑ 加载更早</button>
                  )}
                  {contextPopup.data.before.map((msg: any, i: number) => (
                    <div key={`b${i}`} className={`context-bubble ${msg.role === 'user' ? 'user' : 'assistant'}`}>
                      {msg.is_wander && <span className="context-badge wander">💭 漫想</span>}
                      {msg.is_sentinel && <span className="context-badge sentinel">🛡️ 哨兵</span>}
                      {msg.is_reminder && <span className="context-badge reminder">⏰ 提醒</span>}
                      {msg.tool_summary && <span className="context-badge tool">🔧 {msg.tool_summary}</span>}
                      <div>{stripWikilinks(msg.content)}</div>
                      <div className="context-bubble-time">{msg.timestamp?.slice(11, 16) || ''}</div>
                    </div>
                  ))}
                  {contextPopup.data.target && (
                    <div className={`context-bubble ${contextPopup.data.target.role === 'user' ? 'user' : 'assistant'} target`}>
                      {contextPopup.data.target.is_wander && <span className="context-badge wander">💭 漫想</span>}
                      {contextPopup.data.target.is_sentinel && <span className="context-badge sentinel">🛡️ 哨兵</span>}
                      {contextPopup.data.target.is_reminder && <span className="context-badge reminder">⏰ 提醒</span>}
                      {contextPopup.data.target.tool_summary && <span className="context-badge tool">🔧 {contextPopup.data.target.tool_summary}</span>}
                      <div>{stripWikilinks(contextPopup.data.target.content)}</div>
                      <div className="context-bubble-time">⭐ {contextPopup.data.target.timestamp?.slice(11, 16) || ''}</div>
                    </div>
                  )}
                  {contextPopup.data.after.map((msg: any, i: number) => (
                    <div key={`a${i}`} className={`context-bubble ${msg.role === 'user' ? 'user' : 'assistant'}`}>
                      {msg.is_wander && <span className="context-badge wander">💭 漫想</span>}
                      {msg.is_sentinel && <span className="context-badge sentinel">🛡️ 哨兵</span>}
                      {msg.is_reminder && <span className="context-badge reminder">⏰ 提醒</span>}
                      {msg.tool_summary && <span className="context-badge tool">🔧 {msg.tool_summary}</span>}
                      <div>{stripWikilinks(msg.content)}</div>
                      <div className="context-bubble-time">{msg.timestamp?.slice(11, 16) || ''}</div>
                    </div>
                  ))}
                  {contextPopup.data.hasMoreAfter && (
                    <button className="context-expand-btn" onClick={async () => {
                      const na = contextPopup.after + 3;
                      const bId = contextPopup.bookmarkId;
                      setContextPopup(prev => prev ? { ...prev, after: na } : null);
                      try {
                        const res = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(bId)}/context?before=${contextPopup.before}&after=${na}`);
                        const data = await res.json();
                        setContextPopup(prev => {
                          if (!prev || prev.bookmarkId !== bId || !prev.data) return prev;
                          return { ...prev, data: {
                            ...prev.data,
                            after: data.messages_after || [],
                            hasMoreAfter: data.has_more_after,
                          }};
                        });
                      } catch {}
                    }}>↓ 加载更多</button>
                  )}
                </>
              )}
            </div>
            <div className="context-popup-toolbar">
              <div style={{position:'relative'}}>
                <button className="context-toolbar-btn" onClick={() => setContextPopup(prev => prev ? { ...prev, showRangeMenu: !prev.showRangeMenu } : null)}>🔍 抓取上下文</button>
                {contextPopup.showRangeMenu && (
                  <div className="context-range-menu">
                    {[1,2,3,5,10].map(n => (
                      <button key={n} className="context-range-item" onClick={async () => {
                        const bId = contextPopup.bookmarkId;
                        setContextPopup(prev => prev ? { ...prev, before: n, after: n, showRangeMenu: false, data: null } : null);
                        try {
                          const res = await fetch(`${getApiBase()}/api/bookmarks/${encodeURIComponent(bId)}/context?before=${n}&after=${n}`);
                          const data = await res.json();
                          setContextPopup(prev => prev?.bookmarkId === bId ? { ...prev, data: { before: data.messages_before || [], target: data.target_message, after: data.messages_after || [], hasMoreBefore: data.has_more_before, hasMoreAfter: data.has_more_after } } : prev);
                        } catch {}
                      }}>±{n} 轮</button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default BookmarkPanel;
