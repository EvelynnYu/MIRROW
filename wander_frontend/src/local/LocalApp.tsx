import { useEffect, useState } from 'react';
import WanderLogPanel from '../components/WanderLogPanel';
import WishBoardModal from '../components/WishBoardModal';
import ProfileForm from './ProfileForm';
import MemoryPanel from './MemoryPanel';
import { request, type LocalState } from './api';
import './local.css';
export default function LocalApp({initial}: {initial: LocalState}) {
  const [state, setState] = useState(initial); const [tab, setTab] = useState('chat'); const [panel, setPanel] = useState('');
  const [text, setText] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const refresh = async () => setState(await request('/api/local/state'));
  useEffect(() => { const timer = setInterval(() => { void refresh().catch(() => setError('连接中断；后台操作结果未知，请恢复连接后核对。')); }, 5000); return () => clearInterval(timer); }, []);
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError('');
    try { await fn(); await refresh(); } catch (e) { setError((e as Error).message); void refresh().catch(() => {}); }
    finally { setBusy(false); }
  }
  return <main className="app public-wander-shell local-shell"><header><p className="eyebrow">MIRROW / LOCAL</p>
    <h1>在这里，慢慢延续。</h1><p>本地单用户 · 你们自己的身份、对话与记忆</p></header>
    <nav aria-label="主导航">{[['chat','聊天'],['memory','记忆'],['profile','设定']].map(([key,label]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}
      <button onClick={() => setPanel('logs')}>💭 漫想日志</button><button onClick={() => setPanel('wishes')}>🌠 许愿板</button></nav>
    {!state.configured && <p className="public-status">尚未配置模型。先按部署文档填写后端 .env，保存设定后即可开始；浏览页面不会调用模型。</p>}
    <p role="alert">{error}</p>
    {tab === 'profile' && <ProfileForm profile={state.profile} onSaved={refresh}/>}
    {tab === 'memory' && <MemoryPanel/>}
    {tab === 'chat' && <section>
      <div className="idle-controls"><span>漫想：{state.wander.running ? '运行中' : state.wander.stopping ? '收尾中' : '已停止'}{state.wander.error && ' · 运行失败，请查看日志'}</span>
        <button disabled={busy || !state.configured} onClick={() => void action(async () => {await request('/api/local/wander/' + (state.wander.running ? 'stop' : 'start'), {});})}>{state.wander.running ? '停止漫想' : '开启闲时漫想'}</button></div>
      <p className="local-hint">开启后会调用模型，自主规划可用活动与等待时间。发消息或修改设定会停止漫想；会客、淘宝及设备能力未接入。</p>
      <div className="chat-history" aria-label="聊天记录">{state.messages.length === 0 && <p>这里还没有消息。从属于你们的第一句话开始。</p>}
        {state.messages.map(row => <article className={'chat-message ' + row.role} key={row.message_id}>
          <small>{row.role === 'user' ? state.profile.human_name : state.profile.agent_name} · {new Date(row.timestamp).toLocaleString()}{row.event_type && ' · 漫想分享'}</small><p>{row.content}</p>
          {state.unfinished_requests?.some(request => request.request_id === row.message_id) && <small>回复状态：{state.unfinished_requests.find(request => request.request_id === row.message_id)?.state}（不自动重试）</small>}
        </article>)}</div>
      <form className="chat-compose" onSubmit={e => { e.preventDefault(); const content = text; void action(async () => {
        const result = await request('/api/local/chat', {request_id: crypto.randomUUID(), content});
        if (result.state !== 'complete') throw new Error('这次请求没有完成；不会自动重复发送。');
        setText('');
      }); }}><label htmlFor="chat-input">消息</label><textarea id="chat-input" rows={3} value={text} onChange={e => setText(e.target.value)}/>
        <button disabled={busy || !text.trim() || !state.configured}>{busy ? '处理中…' : '发送'}</button></form>
    </section>}
    {panel === 'logs' && <WanderLogPanel onClose={() => setPanel('')} onOpenWishBoard={() => setPanel('wishes')}/>}
    <WishBoardModal open={panel === 'wishes'} onClose={() => setPanel('')}/>
  </main>;
}
