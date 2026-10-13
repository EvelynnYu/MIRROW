import { useState } from 'react';
import { request } from './api';
type Hit = {id: string; summary: string; sources: Array<{message_id: string; role: string; content: string; timestamp: string}>};
export default function MemoryPanel() {
  const [query, setQuery] = useState(''); const [day, setDay] = useState('');
  const [hits, setHits] = useState<Hit[]>([]); const [message, setMessage] = useState(''); const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<void>) {
    setBusy(true); setMessage('');
    try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  return <section className="local-form"><h2>记忆与来源</h2>
    <form onSubmit={e => {e.preventDefault(); void run(async () => {
      const data = await request('/api/local/memory?q=' + encodeURIComponent(query)); setHits(data.hits);
      setMessage(data.hits.length ? '' : '没有命中已编码记忆。原始聊天仍保留在本地。');
    });}}><label>搜索<input value={query} onChange={e => setQuery(e.target.value)}/></label><button disabled={busy}>查找记忆</button></form>
    {hits.map(hit => <article key={hit.id}><p>{hit.summary}</p><details><summary>核对原始来源</summary>
      {hit.sources.map(source => <blockquote key={source.message_id}><small>{source.role} · {source.timestamp}</small><p>{source.content}</p></blockquote>)}
    </details></article>)}
    <h3>手动整理已结束日期</h3><p>会调用你配置的模型并产生费用。当前日保留原始对话，不自动整理；此公开宿主按本机自然日划分。没有来源或校验失败不会生成成功记录。</p>
    <label>日期<input type="date" value={day} onChange={e => setDay(e.target.value)}/></label>
    <button disabled={busy || !day} onClick={() => void run(async () => {
      const data = await request('/api/local/memory/encode', {date: day}); setMessage('记忆整理状态：' + data.status);
    })}>整理记忆</button>
    <button disabled={busy || !day} onClick={() => void run(async () => {
      const data = await request('/api/cognition/maintenance/run', {source_date: day}); setMessage('认知整理状态：' + data.status);
    })}>整理认知书</button><p role="status">{busy ? '处理中，请勿重复提交…' : message}</p>
  </section>;
}
