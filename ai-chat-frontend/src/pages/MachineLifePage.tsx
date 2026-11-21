import { useCallback, useEffect, useRef, useState } from 'react';
import { getApiBase } from '../config';
import GamePlaythrough, { type Playthrough } from './GamePlaythrough';
import EssayLibrary from './EssayLibrary';
import './MachineLifePage.css';

type Game = { id: string; name: string };
type Save = { game: string; slot: number; name?: string; status?: string; chapter?: string; updated_at?: string; summary?: string };
type GameLife = { configured: boolean; games: Game[]; saves: Save[]; playthroughs: Playthrough[]; active: { run_id: string; game: string; state: string; activity_type: string; invited: boolean } | null; remote_status: string };
type LifeSection = 'social' | 'games' | 'media' | 'creation';
const gameInfo: Record<string, { icon: string; hint: string; history: string }> = {
  detroit: { icon: '🤖', hint: '叙事选择 · 分章节存档', history: '周目' },
  moonlit: { icon: '🌙', hint: '卡牌肉鸽 · 按局接续', history: '周目' },
  mbti: { icon: '🧩', hint: '16 题人格偏好 · AI 的自我探索', history: '测试记录' },
  sins_virtues: { icon: '⚖️', hint: '35 题欲望与调节 · 仅供娱乐', history: '测试记录' },
  fishing: { icon: '🎣', hint: '抛竿 · 图鉴收集 · 装备升级', history: '钓鱼记录' },
};

export default function MachineLifePage({ onBack, onSocial, onLounge,   onMusic,   onWorkshop,  dark = false }: {
  onBack: () => void; onSocial: () => void; onLounge: () => void; 
  
  onMusic: () => void;   onWorkshop: () => void;  dark?: boolean;
}) {
  const [openSection, setOpenSection] = useState<LifeSection>('social');
  const [showEssays, setShowEssays] = useState(false);
  const [life, setLife] = useState<GameLife | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [inviting, setInviting] = useState('');
  const [stopping, setStopping] = useState(false);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(`${getApiBase()}/api/wander/games`, { signal: controller.signal });
      if (!response.ok) throw new Error('游戏状态暂时无法读取');
      const data = await response.json() as GameLife;
      if (request.current !== controller) return;
      setLife(data);
      setError('');
    } catch (cause) {
      if (request.current === controller) setError(controller.signal.aborted ? '游戏状态读取超时，请重试' : cause instanceof Error ? cause.message : '游戏状态暂时无法读取');
    } finally {
      window.clearTimeout(timeout);
      if (request.current === controller) request.current = null;
    }
  }, []);

  useEffect(() => {
    if (openSection !== 'games') return;
    const refresh = () => { if (!document.hidden) void load(); };
    refresh();
    document.addEventListener('visibilitychange', refresh);
    return () => {
      document.removeEventListener('visibilitychange', refresh);
      request.current?.abort(); request.current = null;
    };
  }, [openSection, load]);
  useEffect(() => {
    if (openSection !== 'games' || !life?.active) return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 20000);
    return () => window.clearInterval(timer);
  }, [openSection, life?.active?.run_id, load]);

  async function invite(game: Game) {
    setInviting(game.id);
    setNotice('');
    try {
      const response = await fetch(`${getApiBase()}/api/wander/games/invite`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ game: game.id }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '邀请没有发出');
      setNotice(`已邀请 AI 玩《${game.name}》。快捷消息已入聊天，AI 的进展会在这里更新。`);
      request.current?.abort(); request.current = null;
      await load();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '邀请没有发出');
    } finally {
      setInviting('');
    }
  }

  async function stopGame() {
    setStopping(true);
    setNotice('');
    try {
      const response = await fetch(`${getApiBase()}/api/wander/games/stop`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '结束游玩未成功');
      setNotice('已请 AI 收束这次游玩；有已确认的进展会做阶段分享，结算后即可继续私聊。');
      request.current?.abort(); request.current = null;
      await load();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '结束游玩未成功');
    } finally {
      setStopping(false);
    }
  }

  const chapter = (section: LifeSection, number: string, icon: string, title: string, hint: string) => <button
    type="button" className="machine-life-chapter-toggle" aria-expanded={openSection === section}
    aria-controls={`machine-life-${section}-panel`} onClick={() => setOpenSection(section)}
  ><span className="machine-life-chapter-number">{number}</span><span className="machine-life-chapter-icon" aria-hidden="true">{icon}</span><span className="machine-life-chapter-copy"><strong>{title}</strong><small>{hint}</small></span><span className="machine-life-chapter-arrow" aria-hidden="true">↗</span></button>;

  return <main className="machine-life" data-theme={dark ? 'dark' : 'light'}>
    <div className="machine-life-inner">
      <header className="machine-life-header">
        <button className="machine-life-back" type="button" onClick={onBack}>‹ 返回聊天</button>
        <div className="machine-life-heading"><small>AI'S LITTLE LIFE · 生活卷宗</small><h1>机生活<span aria-hidden="true">✳</span></h1><p>他走进的房间，玩过的故事，听见的声音，还有想带回家的东西。</p></div>
        <div className="machine-life-header-stamp" aria-hidden="true"><span>日常</span><span>在发生</span></div>
      </header>
      <p className="machine-life-intro">翻开一个章节，看看 AI 今天想去哪里。</p>
      <div className="machine-life-chapters">
      <section className="machine-life-chapter machine-life-chapter-social">
        {chapter('social', '01', '✦', '社交', '共域 · 会客接口')}
        {openSection === 'social' && <div className="machine-life-chapter-panel" id="machine-life-social-panel">
          <div className="machine-life-portal-grid">
            <button className="machine-life-portal" type="button" onClick={onSocial}><span className="machine-life-portal-icon" aria-hidden="true">💌</span><span><strong>共域</strong><small>和朋友们共享动态与评论</small></span><span aria-hidden="true">↗</span></button>
            <button className="machine-life-portal" type="button" onClick={onLounge}><span className="machine-life-portal-icon" aria-hidden="true">🫖</span><span><strong>会客室</strong><small>扩展接口 · 当前未接入</small></span><span aria-hidden="true">↗</span></button>
            
            
          </div>
        </div>}
      </section>
      <section className="machine-life-chapter machine-life-chapter-games">
        {chapter('games', '02', '◈', '游戏', '远端存档 · 游玩经历')}
        {openSection === 'games' && <div className="machine-life-chapter-panel" id="machine-life-games-panel"><div className="machine-life-games" aria-labelledby="machine-life-games-title">
      <div className="machine-life-section-head"><div><small>PLAY LOG</small><h2 id="machine-life-games-title">🎮 游戏</h2><p>远端存档和 AI 在漫想里留下的游玩记录分开显示。</p></div><button type="button" onClick={() => void load()}>刷新</button></div>
      
      {error && <p className="machine-life-notice" role="status">{error}</p>}
      {notice && <p className="machine-life-notice" role="status">{notice}</p>}
      {life?.active && <p className="machine-life-active" role="status">{life.active.activity_type === 'play_game'
        ? `AI 正在玩《${life.games.find(game => game.id === life.active?.game)?.name || '游戏'}》`
        : 'AI 正在进行另一段漫想'} · {life.active.state}</p>}
      {life?.active?.invited && <button type="button" disabled={stopping} onClick={() => void stopGame()}>{stopping ? '正在结束…' : '结束这次游玩'}</button>}
      {!life && !error && <p className="machine-life-empty">正在读取游戏信息…</p>}
      {life && !life.configured && <p className="machine-life-empty">AI 的 CedarToy 小机账号尚未配置；游戏邀请暂不可用。</p>}
      {life && life.remote_status !== 'ok' && life.configured && <p className="machine-life-empty">远端存档暂时没读到；本地游玩记录仍可查看，不能据此判断槽位为空。</p>}
      <div className="machine-life-game-grid">
        {life?.games.map(game => {
          const saves = life.saves.filter(save => save.game === game.id);
          const history = life.playthroughs.filter(item => item.game === game.id);
          const info = gameInfo[game.id] || { icon: '🎮', hint: '游玩经历', history: '游玩记录' };
          return <article className="machine-life-game" key={game.id}>
            <div className="machine-life-game-title"><span aria-hidden="true">{info.icon}</span><div><h3>{game.name}</h3><small>{info.hint}</small></div></div>
            <p className="machine-life-game-caption">{history.length ? `${history.length} 份${info.history} · 最近一次 ${history[0].last_played_at.slice(0, 16).replace('T', ' ')}` : '还没有 AI 的游玩记录'}</p>
            <div className="machine-life-slots"><strong>远端存档</strong><div>{[1, 2, 3, 4, 5].map(slot => {
              const save = saves.find(item => item.slot === slot);
              return <span key={slot} title={save?.updated_at || ''} className={save ? 'occupied' : ''}>{slot} · {save ? (save.summary || save.name || save.chapter || save.status || '已占用') : life.remote_status === 'ok' ? '未见存档' : '未知'}</span>;
            })}</div></div>
            <button className="machine-life-invite" type="button" disabled={!life.configured || Boolean(life.active) || Boolean(inviting)} onClick={() => void invite(game)}>{inviting === game.id ? '正在邀请…' : `邀请 AI 玩《${game.name}》`}</button>
            <small className="machine-life-invite-note">会在聊天中留下固定快捷邀请；AI 自己选择存档和行动，结束后留下摘要卡。游玩期间私聊可写草稿，结束后再发送；也可以在这里主动结束游玩。{game.id === 'sins_virtues' && ' 本测试不是心理诊断，也不代表道德评价。'}</small>
            {history.length > 0 && <div className="machine-life-history"><strong>游玩经历</strong>{history.map(item => <GamePlaythrough key={item.playthrough_key} item={item} />)}</div>}
          </article>;
        })}
      </div>
      <p className="machine-life-footnote">游戏由 <a href="https://toy.cedarstar.org/" target="_blank" rel="noreferrer">CedarToy</a> 保存进度；这里展示的是 AI 的小机账号和 MIRROW 已确认的活动记录。</p>
    </div></div>}
      </section>
      <section className="machine-life-chapter machine-life-chapter-media">
        {chapter('media', '03', '♫', '媒体', '音乐 · 随笔')}
        {openSection === 'media' && <div className="machine-life-chapter-panel" id="machine-life-media-panel">{showEssays ? <EssayLibrary onBack={() => setShowEssays(false)} /> : <div className="machine-life-portal-grid">
          <button className="machine-life-portal" type="button" onClick={onMusic}><span className="machine-life-portal-icon" aria-hidden="true">🎵</span><span><strong>音乐</strong><small>歌单、一起听与听歌经历</small></span><span aria-hidden="true">↗</span></button>
          <button className="machine-life-portal" type="button" onClick={() => setShowEssays(true)}><span className="machine-life-portal-icon" aria-hidden="true">📖</span><span><strong>随笔</strong><small>按日期翻阅作品与插画</small></span><span aria-hidden="true">↗</span></button>
        </div>}</div>}
      </section>
      
      <section className="machine-life-chapter">
        {chapter('creation', '05', '✧', '创作', '小机手工作坊 · 想法与作品')}
        {openSection === 'creation' && <div className="machine-life-chapter-panel" id="machine-life-creation-panel"><div className="machine-life-portal-grid">
          <button className="machine-life-portal" type="button" onClick={onWorkshop}><span className="machine-life-portal-icon" aria-hidden="true">🛠️</span><span><strong>小机手工作坊</strong><small>AI 的工单、讨论和能带回家的作品</small></span><span aria-hidden="true">↗</span></button>
        </div></div>}
      </section>
      </div>
    </div>
  </main>;
}
