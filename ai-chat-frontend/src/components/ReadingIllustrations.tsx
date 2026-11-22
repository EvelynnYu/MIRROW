import { useEffect, useState } from 'react';
import { getApiBase } from '../config';
import { EssayIllustration } from './EssayCard';

type Picture = { node_id: string; title: string; chapter_title?: string; quote: string; scene: string; url: string; status: 'ready' };

export function ReadingPicture({ picture }: { picture: Picture }) {
  return <figure className="reading-picture">
    <figcaption>《{picture.title}》{picture.chapter_title && ' · ' + picture.chapter_title} · AI 的读后插画</figcaption>
    <blockquote>{picture.quote}</blockquote>
    <EssayIllustration picture={picture} />
  </figure>;
}

export default function ReadingIllustrations({ workId, nodeId }: { workId: string; nodeId?: string }) {
  const [pictures, setPictures] = useState<Picture[]>([]);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setPictures([]); setFailed(false);
    const query = new URLSearchParams({ source: 'essays', work_id: workId });
    fetch(getApiBase() + '/api/wander/reading-illustrations?' + query, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error(); return response.json(); })
      .then(data => { if (!controller.signal.aborted) setPictures(data.items); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [workId]);
  if (failed) return <small>读后插画暂时无法读取</small>;
  const visiblePictures = nodeId ? pictures.filter(picture => picture.node_id === nodeId) : pictures;
  if (!visiblePictures.length) return null;
  return <details><summary>夹在书里的插画 · {visiblePictures.length}</summary>
    {visiblePictures.map(picture => <ReadingPicture key={picture.node_id} picture={picture} />)}</details>;
}
