import type { WanderStatus } from '../types';

import activityCatalog from '../../shared/presence/activity-visuals.json';

import shelfCatalog from '../../shared/presence/shelf-visuals.json';

const shelf: Record<string, string[]> = shelfCatalog;
const activities: Record<string, string[]> = activityCatalog;

export function plainActivity(text: string) {
  return text.trim().replace(/^(?:\p{Extended_Pictographic}\uFE0F?\s*)+/u, '');
}

export function activityVisual(id?: string, name = '', sources: string[] = []) {
  const label = plainActivity(name);
  const entry = activities[id || ''] ?? Object.values(activities).find(([, ...names]) => names.includes(label) || names.includes(id || ''));
  if (entry === activities.browse_bookmarks) {
    const kinds = [...new Set(sources)];
    const oldBookmark = ['翻收藏夹', '翻看收藏夹'].includes(label || id || '');
    const material = kinds.length === 1 ? shelf[kinds[0]] : !kinds.length && oldBookmark ? shelf.bookmarks : undefined;
    if (material) return { icon: material[0], label: material[1] };
  }
  return { icon: entry?.[0] ?? '💭', label: entry?.[1] ?? (label || '漫想活动') };
}

function dueTime(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';
}

export function wanderPresence(status: Partial<WanderStatus> | null) {
  const state = status?.runtime_state;
  const activity = activityVisual(status?.current_event_type || '', '', status?.shelf_sources);
  const phase = status?.current_activity_state || status?.active_run_state;
  const badge = (icon: string, label: string, detail = '') => ({ icon, label, detail });
  if (state === 'blocked') return badge('⚠️', '漫想暂时受阻');
  if (state === 'planning') return badge('🗓️', '在安排接下来的活动');
  if (state === 'active') {
    if (phase === 'settling') return badge(activity.icon, '在整理活动感想', activity.label);
    if (phase === 'node_review') return badge(activity.icon, '在回顾刚才的活动', activity.label);
    if (phase === 'active_wait') {
      if (status?.current_event_type === 'sleep') return badge(activity.icon, '休息中', dueTime(status.timer_ends_at));
      return badge('⏳', '活动间隔', [activity.label, dueTime(status?.next_node_at)].filter(Boolean).join(' · '));
    }
    return badge(activity.icon, status?.current_event_type ? '正在' + activity.label : '漫想进行中');
  }
  if (!status) return badge('●', '状态待同步');
  if (status.mode === 'offline') return badge('●', '此刻未在漫想');
  if (state === 'scheduled') {
    const retry = /error|failed|timeout|cancelled/.test(status.wake_reason || '');
    return badge(retry ? '⏳' : '🕰️', retry ? '等待重试' : '漫想间隔', dueTime(status.next_plan_at));
  }
  if (state === 'not_initialized') return badge('⏳', '漫想准备中');
  return badge('💭', status.mode === 'brainstorming' ? '漫想进行中' : '等待下一次漫想');
}
