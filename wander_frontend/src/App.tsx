import { useEffect, useState } from 'react';
import WanderLogPanel from './components/WanderLogPanel';
import WishBoardModal from './components/WishBoardModal';

export default function App() {
  const [panel, setPanel] = useState<'logs' | 'wishes' | null>('logs');
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setPanel(null); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
  return <main className="app public-wander-shell">
    <header><p className="eyebrow">MIRROW / WANDER</p><h1>想法，有迹可循。</h1>
      <p>看见每次漫想的经历、感想，以及留在心里的愿望。</p></header>
    <nav aria-label="漫想面板"><button onClick={() => setPanel('logs')}>💭 漫想日志</button>
      <button onClick={() => setPanel('wishes')}>🌠 许愿板</button></nav>
    <section className="connection-note"><h2>本地模块 · 真实运行记录</h2>
      <p>此页面读取你自己的漫想数据库，不会自动启动模型、设备或主动发送消息。</p>
      <p>会客 · 未接入（仅接口）　/　逛淘宝 · 未接入（仅接口）</p>
      <small>这两个外部集成移植自 AionsHome，专属页面和实现未开放。若宿主另行接入，通用日志仍可展示其真实结果。</small>
    </section>
    {panel === 'logs' && <WanderLogPanel onClose={() => setPanel(null)} onOpenWishBoard={() => setPanel('wishes')} />}
    <WishBoardModal open={panel === 'wishes'} onClose={() => setPanel(null)} />
  </main>;
}
