import { useEffect, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { getApiBase } from '../config';
import ChatMarkdown from '../components/ChatMarkdown';
import ReadingIllustrations from '../components/ReadingIllustrations';
import { EssayIllustration, type Essay } from '../components/EssayCard';
import type { EssaySeries } from './essaySeries';

export default function EssayReader({ series, initialId }: { series: EssaySeries; initialId: string }) {
  const [id, setId] = useState(initialId);
  const [article, setArticle] = useState<Essay | null>(null);
  const [failedId, setFailedId] = useState('');
  const [retry, setRetry] = useState(0);
  const multiple = series.parts.length > 1;

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${getApiBase()}/api/wander/essays/${encodeURIComponent(id)}`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error(); return response.json(); })
      .then(data => { if (!controller.signal.aborted) setArticle(data); })
      .catch(() => { if (!controller.signal.aborted) setFailedId(id); });
    return () => controller.abort();
  }, [id, retry]);

  function select(partId: string) {
    if (partId === id) return;
    setFailedId('');
    setId(partId);
  }

  function handleKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const size = series.parts.length;
    const position: Record<string, number> = {
      ArrowRight: (index + 1) % size, ArrowLeft: (index + size - 1) % size, Home: 0, End: size - 1,
    };
    const next = position[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const partId = series.parts[next].id;
    select(partId);
    document.getElementById(`essay-tab-${partId}`)?.focus();
  }

  return <article className="essay-page">
    <h2>{series.root.title}</h2>
    {multiple && <div className="essay-tabs" role="tablist" aria-label="随笔篇目">
      {series.parts.map((part, index) => <button type="button" role="tab" key={part.id} id={`essay-tab-${part.id}`}
        aria-selected={part.id === id} aria-controls="essay-reading-panel" tabIndex={part.id === id ? 0 : -1}
        onClick={() => select(part.id)} onKeyDown={event => handleKey(event, index)}>
        <strong>{index === 0 ? '原篇' : part.title !== series.root.title ? part.title : series.parts.length === 2 ? '续篇' : `续篇 ${index}`}</strong>
        <time dateTime={part.created_at}>{part.created_at.slice(0, 10)}</time>
      </button>)}
    </div>}
    <div id="essay-reading-panel" role={multiple ? 'tabpanel' : undefined} aria-labelledby={multiple ? `essay-tab-${id}` : undefined}>
      {failedId === id ? <p role="alert">这篇随笔暂时无法读取。 <button type="button" onClick={() => { setFailedId(''); setRetry(value => value + 1); }}>重试</button></p>
        : article?.id !== id ? <p role="status">正在翻页…</p> : <>
          {article.title !== series.root.title && <h3>{article.title}</h3>}
          <time>{article.created_at.slice(0, 16).replace('T', ' ')}</time>
          {article.continued_from && series.parts.length > 2 && <small> · 接续 {article.continued_from.created_at.slice(0, 10)}《{article.continued_from.title}》</small>}
          <ChatMarkdown content={article.content || ''} /><EssayIllustration picture={article.illustration} />
          <ReadingIllustrations key={article.id} workId={article.id} />
          <footer>✦ 随笔作品 · {article.keyword}</footer>
        </>}
    </div>
  </article>;
}
