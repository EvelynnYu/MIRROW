import { useEffect, useRef, useState } from 'react';
import { composeMusic, openMusic } from './api';
import './music.css';
export default function ConversationQuickTools({ onSearch, revealToken }: { onSearch: () => void; revealToken?: string | null }) {
  const [visible, setVisible] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [menu, setMenu] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let start: { x: number; y: number; time: number } | null = null;
    const down = (e: PointerEvent) => { start = { x: e.clientX, y: e.clientY, time: Date.now() }; };
    const up = (e: PointerEvent) => {
      const el = e.target instanceof Element ? e.target : null;
      if (!el || !start || Math.hypot(e.clientX - start.x, e.clientY - start.y) > 7 || Date.now() - start.time > 350 || window.getSelection()?.toString()) return;
      if (root.current?.contains(el)) return;
      setExpanded(false); setMenu(false);
      if (el.closest('[data-conversation-surface]') && !el.closest('button,a,input,textarea,select,[role="button"],.message,.music-card,.message-search-panel')) setVisible(v => !v);
    };
    document.addEventListener('pointerdown', down); document.addEventListener('pointerup', up);
    return () => { document.removeEventListener('pointerdown', down); document.removeEventListener('pointerup', up); };
  }, []);
  useEffect(() => { if (revealToken) setVisible(true); }, [revealToken]);
  return <div ref={root} className={'music-quick' + (!visible ? ' concealed' : '')}>
    <button className="music-dot" onClick={() => setExpanded(v => !v)} aria-label="展开搜索与音乐" aria-expanded={expanded} tabIndex={visible ? 0 : -1}><span /></button>
    {visible && expanded && <div className="music-quick-rail"><button aria-label="搜索消息" onClick={() => { onSearch(); setExpanded(false); }}>🔍</button><button aria-label="音乐快捷菜单" aria-expanded={menu} onClick={() => setMenu(v => !v)}>🎶</button></div>}
    {visible && expanded && menu && <div className="music-quick-menu">
      <button onClick={() => { composeMusic(); setExpanded(false); }}>分享一首歌 <span>制作歌曲小卡片</span></button>
      <button onClick={() => { openMusic('player'); setExpanded(false); }}>正在一起听 <span>播放与安静陪听</span></button>
      <button onClick={() => { openMusic('playlists'); setExpanded(false); }}>我的歌单 <span>选一张，慢慢听</span></button>
      <button onClick={() => { openMusic('shared'); setExpanded(false); }}>共享歌库 <span>找回寄过的歌与次数</span></button>
      <button onClick={() => { openMusic('home'); setExpanded(false); }}>打开音乐中枢 <span>歌单与听歌经历</span></button>
    </div>}
  </div>;
}
