import { useEffect, useMemo, useState } from 'react';
import type { Essay } from '../components/EssayCard';
import EssayReader from './EssayReader';
import { groupEssays, loadEssayIndex, type EssaySeries } from './essaySeries';
import './EssayLibrary.css';

const PAGE_SIZE = 24;

export default function EssayLibrary({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<Essay[]>([]);
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [opened, setOpened] = useState<{ series: EssaySeries; initialId: string } | null>(null);
  const [matches, setMatches] = useState<Set<string> | null>(null);
  const [loadingIndex, setLoadingIndex] = useState(true);
  const [indexError, setIndexError] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoadingIndex(true);
    setIndexError(false);
    loadEssayIndex(controller.signal)
      .then(rows => { if (!controller.signal.aborted) setItems(rows); })
      .catch(() => { if (!controller.signal.aborted) setIndexError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoadingIndex(false); });
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    loadEssayIndex(controller.signal, query)
      .then(rows => { if (!controller.signal.aborted) setMatches(new Set(rows.map(row => row.id))); })
      .catch(() => { if (!controller.signal.aborted) setSearchError(true); });
    return () => controller.abort();
  }, [query, retry]);

  const series = useMemo(() => groupEssays(items), [items]);
  const visible = query ? series.filter(book => book.parts.some(part => matches?.has(part.id))) : series;
  const loading = loadingIndex || (!!query && !matches);
  const error = indexError || searchError;
  return <section className="essay-library" aria-label="随笔书册">
    <header><button type="button" onClick={() => {
      if (!opened) return onBack();
      setOpened(null);
      setRetry(value => value + 1);
    }}>‹ {opened ? '目录' : '媒体'}</button><span>随 笔</span></header>
    {opened ? <EssayReader series={opened.series} initialId={opened.initialId} /> : <>
      <h2>随手写下的日子</h2>
      <input aria-label="搜索随笔" placeholder="按题目、关键词或正文查找" value={query} onChange={event => {
        setQuery(event.target.value); setOffset(0); setMatches(null); setSearchError(false);
      }} />
      {error ? <p role="alert">随笔目录暂时无法读取。 <button type="button" onClick={() => { setSearchError(false); setRetry(value => value + 1); }}>重试</button></p>
        : loading ? <p role="status">正在翻页…</p>
        : !visible.length ? <p>{query ? '没有找到相符的随笔，换个词试试。' : '这里还没有随笔。'}</p>
        : <ol className="essay-contents">{visible.slice(offset, offset + PAGE_SIZE).map(book => {
          const hit = query && matches ? book.parts.find(part => matches.has(part.id)) : undefined;
          return <li key={book.root.id}><button type="button" onClick={() => setOpened({ series: book, initialId: hit?.id ?? book.root.id })}>
            <time>{book.latest.created_at.slice(0, 10)}</time><strong>{book.root.title}</strong>
            <span>{book.parts.length > 1 ? `共 ${book.parts.length} 篇` : book.root.keyword}
              {hit && hit.id !== book.root.id && ` · 匹配续篇《${hit.title}》`}</span>
          </button></li>;
        })}</ol>}
      {visible.length > PAGE_SIZE && !loading && !error && <nav aria-label="随笔目录翻页">
        <button type="button" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</button>
        <small>{offset / PAGE_SIZE + 1} / {Math.ceil(visible.length / PAGE_SIZE)}</small>
        <button type="button" disabled={offset + PAGE_SIZE >= visible.length} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
      </nav>}
    </>}
  </section>;
}
