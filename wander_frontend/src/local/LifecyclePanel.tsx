import { useState } from 'react';
import { request, type LifecycleState } from './api';

const labels: Record<string, string> = {unknown:'未声明',idle:'空闲',coding:'写代码',gaming:'游戏',out:'外出',
  eating:'吃饭',bathing:'洗澡',napping:'小憩',sleeping:'睡眠',other:'其他'};
const stages: Record<string, string> = {memory:'记忆与日结',cognition:'认知书',diary:'日记'};
const states: Record<string, string> = {pending:'待处理',running:'处理中',completed:'已完成',error:'失败，可重试',review_required:'需要核对'};

export default function LifecyclePanel({value, onSaved}: {value: LifecycleState; onSaved: () => Promise<void>}) {
  const [prefs, setPrefs] = useState(value.preferences);
  const [status, setStatus] = useState(value.user_status.status);
  const [detail, setDetail] = useState(value.user_status.detail);
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState('');
  const [books, setBooks] = useState<Record<string, unknown[]> | null>(null);
  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setMessage('');
    try { await fn(); await onSaved(); setMessage('已更新'); }
    catch(e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="local-form lifecycle"><h2>日常与整理</h2>
    <p>当前活跃日：{value.active_day?.date || '尚未开始'} · 当前状态：{labels[value.user_status.status] || value.user_status.status}</p>
    <form onSubmit={e => {e.preventDefault(); void act(() => request('/api/local/lifecycle/status', {status,detail}, 'PUT'));}}>
      <label>我的状态<select value={status} onChange={e => setStatus(e.target.value)}>{Object.entries(labels).map(([id,label]) => <option value={id} key={id}>{label}</option>)}</select></label>
      <label>状态补充<input maxLength={1000} value={detail} onChange={e => setDetail(e.target.value)}/></label><button disabled={busy}>保存状态</button>
    </form>
    <h3>日结约定</h3>
    <form onSubmit={e => {e.preventDefault(); void act(() => request('/api/local/lifecycle/settings',prefs,'PUT'));}}>
      <label>约定词（留空关闭关键词触发）<input maxLength={80} value={prefs.covenant_word} onChange={e => setPrefs({...prefs,covenant_word:e.target.value})}/></label>
      <div className="lifecycle-grid"><label>生效开始<input type="time" required value={prefs.window_start} onChange={e => setPrefs({...prefs,window_start:e.target.value})}/></label>
        <label>生效结束<input type="time" required value={prefs.window_end} onChange={e => setPrefs({...prefs,window_end:e.target.value})}/></label>
        <label>触发后等待（分钟）<input type="number" min={1} max={1440} required value={prefs.wait_minutes} onChange={e => setPrefs({...prefs,wait_minutes:Number(e.target.value)})}/></label>
        <label>兜底换日时间（整点）<input type="number" min={0} max={23} required value={prefs.day_change_hour} onChange={e => setPrefs({...prefs,day_change_hour:Number(e.target.value)})}/></label></div>
      <p>支持跨午夜时段，例如 22:00–03:00。新消息会取消旧倒计时；再次命中约定词会重新计时。午夜前结束会预约至午夜，凌晨对话在收尾前仍属于同一活跃日。</p>
      <label className="check"><input type="checkbox" checked={prefs.automatic_digest} onChange={e => setPrefs({...prefs,automatic_digest:e.target.checked})}/>日结束后自动整理记忆、认知与日记</label>
      <label className="check"><input type="checkbox" checked={prefs.autonomous_wander} onChange={e => setPrefs({...prefs,autonomous_wander:e.target.checked})}/>闲时自动进入漫想</label>
      <label>聊天空闲多久后启动（分钟）<input type="number" min={1} max={1440} required value={prefs.idle_minutes} onChange={e => setPrefs({...prefs,idle_minutes:Number(e.target.value)})}/></label>
      <label className="check"><input type="checkbox" checked={prefs.sentinel_enabled} onChange={e => setPrefs({...prefs,sentinel_enabled:e.target.checked})}/>开启哨兵主动联系</label>
      <p>以上后台任务会调用你配置的模型并产生费用。漫想启动后的正常等待由模型规划；哨兵默认只接入声明状态和本页面可见性，GPS 等设备来源需按文档接入。</p>
      <button disabled={busy}>保存日常设置</button>
    </form>
    {value.reservation && <p role="status">已预约 {new Date(value.reservation.due_at).toLocaleString()} 收尾{value.reservation.deferred_to_midnight && '（延至午夜）'}</p>}
    <button disabled={busy || !value.active_day} onClick={() => void act(() => request('/api/local/lifecycle/end',{}))}>今天聊到这里</button>
    <button disabled={busy || !value.reservation} onClick={() => void act(() => request('/api/local/lifecycle/cancel',{}))}>取消收尾预约</button>
    <p role="status">{busy ? '处理中…' : message || value.error}</p>
    <h3>日结记录</h3>
    {value.days.length === 0 && <p>开始聊天后会出现第一个活跃日。</p>}
    {value.days.map(day => <article key={day.date}><h4>{day.date} · {day.state === 'open' ? '保留原始对话' : '已结束'}</h4>
      {day.jobs.filter(job => job != null).map(job => <div key={job.stage}><p>{stages[job.stage]}：{states[job.state] || job.state}</p>
        {job.stage === 'diary' && Boolean(job.result.text) && <details><summary>查看日记</summary><p className="diary-text">{String(job.result.text)}</p></details>}
        {job.state === 'review_required' && <small>本步尚未通过全部校验，原始聊天继续保留。</small>}</div>)}
      {day.state === 'closed' && day.jobs.some(job => job && job.state !== 'completed') && <button disabled={busy || day.jobs.some(job => job?.state === 'running')} onClick={() => void act(() => request('/api/local/lifecycle/days/'+day.date+'/retry',{}))}>继续 / 重试日结</button>}
    </article>)}
    <h3>认知书</h3><button disabled={busy} onClick={() => void act(async () => setBooks(await request('/api/local/lifecycle/cognition')))}>读取认知书</button>
    {books && Object.entries(books).map(([domain, entries]) => <details key={domain}><summary>{{self:'自我',other:'他人',world:'世界'}[domain]} · {entries.length} 条</summary><pre>{JSON.stringify(entries,null,2)}</pre></details>)}
  </section>;
}
