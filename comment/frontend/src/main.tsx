import React from 'react';
import { createRoot } from 'react-dom/client';
import SocialFeedPage from './pages/SocialFeedPage';
import SocialInbox from './components/SocialInbox';

// For integration smoke/preview only. Mount SocialFeedPage directly in the host App.
const ready = !!window.MirrowCommentHost?.apiBase;
createRoot(document.getElementById('root')!).render(<React.StrictMode>{ready
  ? <><nav aria-label="共域通知" style={{display:'flex',justifyContent:'flex-end',padding:8}}><SocialInbox onOpenFeed={() => document.getElementById('comment-page')?.scrollIntoView()} /></nav><div id="comment-page"><SocialFeedPage onBack={() => window.MirrowCommentHost?.onBack?.()} /></div></>
  : <main style={{padding:24}}><h1>共域内站接入</h1><p>此组件未配置本家后端。请按 UI_PARITY.md 接入宿主路由，不用访客网页替代内站页面。</p></main>
}</React.StrictMode>);
