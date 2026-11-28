import ChatMarkdown from './ChatMarkdown';
import { getApiBase } from '../config';
import './EssayCard.css';

type Illustration = { status: 'ready'; url: string; scene: string } | { status: 'failed' | 'interrupted' | 'requested' };
type EssayLink = { id: string; title: string; created_at: string };
export type Essay = EssayLink & { keyword: string; content?: string; illustration?: Illustration; continued_from?: EssayLink; continuations?: EssayLink[] };

export function EssayIllustration({ picture }: { picture?: Illustration }) {
  if (!picture) return null;
  if (picture.status === 'ready') {
    if (typeof picture.url !== 'string' || !/^\/static\/essay_illustrations\/[a-f0-9]{32}\.(?:png|jpe?g|webp)$/.test(picture.url)) {
      return <small className="essay-picture-status">配图地址无效</small>;
    }
    return <img className="essay-illustration" src={getApiBase() + picture.url} alt={picture.scene} loading="lazy" />;
  }
  const label = { failed: '这篇的配图未完成', interrupted: '配图中断', requested: '已选配图，尚未收到成图' };
  return <small className="essay-picture-status">{label[picture.status]}</small>;
}
export default function EssayCard({ card }: { card: Essay }) {
  return <details className="essay-share-card">
    <summary><span aria-hidden="true">✍️</span><span><strong>{card.title}</strong><small>{card.created_at.slice(0, 10)} · AI 的随笔</small></span><span aria-hidden="true" /></summary>
    <div className="essay-share-body"><ChatMarkdown content={card.content || ''} /><EssayIllustration picture={card.illustration} /></div>
  </details>;
}
