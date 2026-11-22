import type { ToolCallRecord } from '../types';
import { wanderReceipt } from '../lib/wanderReceipt';
import ReadingIllustrations from './ReadingIllustrations';
import CheckupExperienceDetails from './CheckupExperienceDetails';
import ReadableReceiptValue from './ReadableReceiptValue';

export default function WanderReceiptDetails({ toolCall }: { toolCall: ToolCallRecord }) {
  const view = wanderReceipt(toolCall);
  return <div className="wander-receipt-details">
    <section aria-label="上下文保留档位">
      <p className="event-context-caption">上下文保留：{view.retention.label}</p>
      <p className="event-context-note">{view.retention.reason}</p>
      {view.stopReason && <p className="event-context-note">{view.stopReason}</p>}
    </section>
    {view.facts.length > 0 && <section><p className="event-context-caption">这次做了什么</p>
      {view.facts.map((fact, index) => <ReadableReceiptValue key={index} value={fact} />)}</section>}
    {view.pictures.map(picture => <ReadingIllustrations key={picture.nodeId + picture.workId} {...picture} />)}
    {view.materials.some(item => item.kind === 'checkup') && <CheckupExperienceDetails toolCall={toolCall} />}
    {view.review && <section><p className="event-context-caption">活动回顾</p><ReadableReceiptValue value={view.review} /></section>}
    <section><p className="event-context-caption">留下的感想</p>
      {view.feelings.length ? view.feelings.map((feeling, index) => <ReadableReceiptValue key={index} value={feeling} />)
        : <p className="event-context-note">{view.noFeeling}</p>}
    </section>
  </div>;
}
