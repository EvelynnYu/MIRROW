import { useEffect, useState } from 'react';
import { getApiBase } from '../config';
import ReadingIllustrations from '../components/ReadingIllustrations';
import ChatMarkdown from '../components/ChatMarkdown';
import { EssayIllustration, type Essay } from '../components/EssayCard';
import './EssayLibrary.css';

export default function EssayLibrary({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<Essay[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const [article, setArticle] = useState<Essay | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    const path = selected ? `/${encodeURIComponent(selected)}` : `?${new URLSearchParams({ query, offset: String(offset), limit: '24' })}`;
    fetch(`${getApiBase()}/api/wander/essays${path}`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error('随笔暂时无法读取'); return response.json(); })
      .then(data => { if (controller.signal.aborted) return; if (selected) setArticle(data); else { setItems(data.items); setTotal(data.total); } })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取失败'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selected, offset, query, retry]);
  return <section className="essay-library" aria-label="AI 的随笔书册">
    <header><button type="button" onClick={() => { if (selected) { setSelected(''); setArticle(null); } else onBack(); }}>‹ {selected ? '目录' : '媒体'}</button><span>随 笔 · AI</span></header>
    {error ? <p role="alert">{error} <button type="button" onClick={() => setRetry(retry + 1)}>重试</button></p> : loading ? <p role="status">正在翻页…</p> : null}
    {!selected && <><h2>随手写下的日子</h2><p className="essay-library-intro">心得、故事，以及偶然落在纸上的想象。</p>
      <input aria-label="搜索随笔" placeholder="按题目、关键词或正文查找" value={query} onChange={e => { setQuery(e.target.value); setOffset(0); }} />
      {!loading && !error && !items.length && <p>{query ? '没有找到相符的随笔，换个词试试。' : '这里还没有随笔。写成的作品会按日期留在这里。'}</p>}
      <ol className="essay-contents">{items.map(item => <li key={item.id}><button type="button" onClick={() => { setArticle(null); setSelected(item.id); }}><time>{item.created_at.slice(0, 10)}</time><strong>{item.title}</strong><span>{item.keyword}</span></button></li>)}</ol>
      <nav aria-label="随笔目录翻页"><button type="button" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 24))}>上一页</button><small>{total} 篇</small><button type="button" disabled={offset + 24 >= total || loading} onClick={() => setOffset(offset + 24)}>下一页</button></nav></>}
    {selected && article && !loading && !error && <article className="essay-page"><time>{article.created_at.slice(0, 16).replace('T', ' ')}</time><h2>{article.title}</h2><ChatMarkdown content={article.content || ''} /><EssayIllustration picture={article.illustration} /><ReadingIllustrations key={article.id} workId={article.id} /><footer>✦ AI 的作品 · {article.keyword}</footer></article>}
  </section>;
}
