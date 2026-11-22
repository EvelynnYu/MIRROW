import type { ToolCallRecord } from '../types';

export type ReceiptData = Record<string, unknown>;
export const record = (value: unknown): ReceiptData => value && typeof value === 'object' && !Array.isArray(value) ? value as ReceiptData : {};
export const records = (value: unknown): ReceiptData[] => Array.isArray(value) ? value.map(record) : [];
export const prose = (value: unknown): string => typeof value === 'string' ? value.trim() : '';
const unique = (values: string[]) => [...new Set(values.filter(Boolean))];
export const receiptStateLabels: Record<string, string> = {
  success: '成功', succeeded: '成功', completed: '已完成', failure: '失败', failed: '失败',
  partial: '部分取得', skipped: '已跳过', interrupted: '已中断', uncertain: '尚不确定',
  unavailable: '不可用', unknown: '未知', confirmed: '已确认', not_assessed: '尚未判断',
  not_attempted: '未采集', pending: '待处理', timeout: '等待超时',
};
export const receiptStatus = (value: unknown) => receiptStateLabels[prose(value)] || '未确认';

const shelfKinds: Record<string, string> = { essays: '随笔', bookmarks: '收藏的对话' };

// Older cards froze the completed drawing as this exact, tool-written suffix.
// Pictures encountered in a book, a plan to draw, and a reflection are separate facts.
function finishedPictureWorks(material: ReceiptData) {
  const description = prose(material.description);
  return records(record(material.reading).items).filter(item =>
    item.source === 'essays' && prose(item.title) &&
    description.endsWith(`；为《${item.title}》画了一幅读后插画`));
}

function conversationKind(reading: ReceiptData) {
  return reading.source_mode === 'existing_bookmark' ? shelfKinds.bookmarks : '旧日对话记录';
}

export function bookshelfActivityTitle(call: ToolCallRecord) {
  const experience = record(call.extra_data?.memory_experience);
  if (experience.activity_type !== 'browse_bookmarks') return '';
  const kinds: string[] = [], drawings: string[] = [];
  for (const material of records(experience.materials)) {
    const reading = record(material.reading);
    if (material.kind === 'reading' && records(reading.messages).length) kinds.push(conversationKind(reading));
    if (material.kind !== 'bookshelf') continue;
    for (const item of records(reading.items)) kinds.push(shelfKinds[prose(item.source)] || '');
    drawings.push(...finishedPictureWorks(material).map(item => `《${item.title}》`));
  }
  const sources = unique(kinds);
  if (!sources.length) return '';
  const drawn = unique(drawings);
  const state = prose(record(call.extra_data?.execution_overview).state);
  const ending = state === 'interrupted' || state === 'aborted' ? '，随后中断'
    : call.status === 'failure' || call.status === 'partial' ? '，部分步骤未完成' : '';
  return '翻看了' + sources.join('、') + (drawn.length ? '，为' + drawn.join('、') + '画了插画' : '') + ending;
}

function shelfFacts(material: ReceiptData) {
  const reading = record(material.reading);
  if (material.kind === 'reading') {
    const date = prose(reading.active_date);
    return [`回看了${date ? date + ' 的' : ''}${conversationKind(reading)}，读了 ${records(reading.messages).length} 条消息摘录。`];
  }
  const facts = records(reading.items).map(item => {
    const verb = item.depth === 'deep' ? '细读' : '翻看';
    const title = prose(item.title);
    if (item.source === 'essays') {
      const keyword = prose(item.keyword);
      const details = [prose(item.coverage), keyword && `关键词「${keyword}」`].filter(Boolean);
      return `${verb}随笔《${title}》` + (details.length ? `（${details.join('，')}）` : '');
    }
    if (item.source === 'bookmarks') {
      const owner = item.collected_by === 'k' ? '自己' : item.collected_by === 'user' ? '人类伙伴' : '';
      const date = prose(item.created_at).slice(0, 10);
      return `${verb}${owner}收藏的对话${date ? '（' + date + '）' : ''}，读了 ${records(item.messages).length} 条消息摘录。`;
    }
    return '';
  }).filter(Boolean);
  if (!facts.length) return [prose(material.description)];
  const unread = records(reading.unread_items).length;
  if (unread) facts.push(`另有 ${unread} 项未读到。`);
  for (const title of unique(finishedPictureWorks(material).map(item => prose(item.title)))) facts.push(`为《${title}》画了一幅读后插画。`);
  return facts;
}

// Mirrors retention_tier in context_builder/wander_event_projection.py.
// Keep this local: expanding a receipt must not wait for context-view.
export function wanderRetention(call: ToolCallRecord) {
  let saved = record(call.extra_data).emotion_effect;
  if (typeof saved === 'string') {
    try { saved = JSON.parse(saved); } catch { saved = undefined; }
  }
  const effect = record(saved);
  if (!Object.keys(effect).length) return {
    tier: 'normal', label: '普通', reason: '未保存有效的心情变化字段，按当前规则沿用普通档。',
  };
  if (effect.changed && effect.persistence === 'lingering') return {
    tier: 'strong', label: '强', reason: '记录了心情变化，影响标记为持续。',
  };
  if (effect.changed) return {
    tier: 'normal', label: '普通', reason: '记录了心情变化，未标记持续影响。',
  };
  return {tier: 'weak', label: '弱', reason: '未标记心情变化。'};
}

// UI projection of frozen evidence only. Absence is not an emotion diagnosis.
export function wanderReceipt(call: ToolCallRecord) {
  const extra = record(call.extra_data);
  const experience = record(extra.memory_experience);
  const overview = record(extra.execution_overview);
  const materials = records(experience.materials);
  const counts = record(overview.execution_status_counts);
  const feelings = unique([
    prose(experience.retained_impression), prose(record(extra.retained_experience).impression),
    ...materials.flatMap(item => [prose(item.reflection),
      item.kind === 'checkup' && !item.reflection_is_fact_projection ? prose(item.recorded_reflection) : '']),
    ...materials.filter(item => item.kind === 'game').flatMap(item => records(item.chapters).map(chapter => prose(chapter.reflection))),
  ]);
  const attempts = Array.isArray(overview.memory_lookup_attempts) ? overview.memory_lookup_attempts.map(prose) : [];
  const facts = unique([...attempts, ...materials.flatMap(item => {
    if (item.kind === 'checkup') return [];
    if (item.kind === 'music' || item.kind === 'music_listening_attempt') {
      const execution = record(item.music_execution);
      const verb = item.completion_signal === 'listening_finished' ? '听完了' : item.completion_signal === 'analysis_complete' ? '分析了歌曲材料' : '听歌记录';
      return [prose(execution.fact) || `${verb}《${prose(item.title) || '曲名未记录'}》${item.artist ? ` · ${prose(item.artist)}` : ''}`];
    }
    if (item.kind === 'xhs') return [`${item.material_delivery === 'executor_only' ? '手机取得了' : '读了'}小红书帖子《${prose(item.title) || '标题未记录'}》`];
    if (item.kind === 'reading' || item.kind === 'bookshelf') return shelfFacts(item);
    if (item.kind === 'game') return records(item.chapters).map(chapter => `游玩了第 ${chapter.number ?? '?'} 章《${prose(chapter.title) || '章节未命名'}》`);
    return [prose(item.description)];
  })]);
  const recalled = prose(experience.settlement_reflection);
  // Checkup's old result repeats the structured observations, sometimes as JSON.
  const result = materials.some(item => item.kind === 'checkup') ? recalled : prose(call.result) || recalled || prose(call.error);
  const review = result && !feelings.includes(result) && !facts.includes(result) ? result : '';
  let noFeeling = '';
  if (!feelings.length) {
    const observation = '这张回执没有单独保存感想。';
    if (overview.state === 'interrupted') noFeeling = `${observation}本次活动被中断；记录没有说明未留感想的具体原因。`;
    else if (Number(counts.failed) > 0 || overview.state === 'aborted') noFeeling = `${observation}本次存在未完成或失败的步骤。${review ? '具体情况见活动回顾。' : '未记录具体原因。'}`;
    else if (Number(counts.skipped) > 0 && !Number(counts.succeeded)) noFeeling = `${observation}本次执行被跳过。${review ? '具体情况见活动回顾。' : '未记录具体原因。'}`;
    else if (experience.activity_type === 'sleep') noFeeling = `${observation}本次记录的是休息经过。`;
    else if (experience.essay_index) noFeeling = `${observation}此卡保留的是随笔索引，作品正文在书柜中。`;
    else if (materials.some(item => item.kind === 'material') && (review || facts.length)) noFeeling = '这条记录只保存了活动正文，未单列感想。请以上方原文为准。';
    else noFeeling = `${observation}未记录具体原因。`;
  }
  const stopReason = prose(overview.stop_reason) || prose(experience.stop_reason);
  const pictures = materials.flatMap(m => finishedPictureWorks(m).filter(item => prose(m.source_node_id) && prose(item.id))
    .map(item => ({nodeId: prose(m.source_node_id), workId: prose(item.id), source: item.source as 'essays'})));
  return { materials, facts, pictures, feelings, review, noFeeling, stopReason, retention: wanderRetention(call) };
}
