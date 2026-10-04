import type {TimelineMoment} from '../components/SocialTimelineCard';
export type TimelinePage = {items:TimelineMoment[];has_more:boolean;next_cursor:string|null;
  unavailable:{site_name:string}[];omitted_sources:number};
export type TimelineEvent = {type:'begin';total_sources:number;omitted_sources:number}
  | {type:'source';items:TimelineMoment[];finished_sources:number;total_sources:number}
  | {type:'complete';page:TimelinePage};

// Bounded NDJSON; cancellation closes the response even during a source wait.
export async function readTimelineStream(response: Response, signal: AbortSignal,
  onEvent: (event:TimelineEvent)=>void): Promise<TimelinePage> {
  if (!response.body) throw Error('时间线进度流暂不可用。');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', total = 0, result: TimelinePage | null = null;
  const cancel = () => { void reader.cancel().catch(()=>{}); };
  signal.addEventListener('abort', cancel, {once:true});
  const line = (text:string) => {
    if (!text.trim()) return;
    const event = JSON.parse(text) as TimelineEvent;
    if (!['begin','source','complete'].includes(event.type) || result) throw Error('时间线进度格式不匹配。');
    if (event.type === 'complete') result = event.page;
    onEvent(event);
  };
  try {
    if (signal.aborted) throw new DOMException('Aborted','AbortError');
    while (true) {
      const {done,value} = await reader.read();
      if (signal.aborted) throw new DOMException('Aborted','AbortError');
      if (done) { buffer += decoder.decode(); if (buffer) line(buffer); break; }
      total += value.byteLength;
      if (total > 40 * 1024 * 1024) throw Error('时间线页超过安全大小，请单独进入共域。');
      buffer += decoder.decode(value,{stream:true});
      if (buffer.length > 2 * 1024 * 1024) throw Error('时间线进度记录过大。');
      let split: number;
      while ((split = buffer.indexOf('\n')) >= 0) {line(buffer.slice(0,split)); buffer=buffer.slice(split+1);}
    }
    if (!result) throw Error('部分内容已到达，但本页尚未读完；请刷新重试。');
    return result;
  } finally {signal.removeEventListener('abort',cancel); await reader.cancel().catch(()=>{});reader.releaseLock();}
}
