import { getApiBase } from '../config';
export type Profile = { human_name: string; agent_name: string; persona: string; relationship_started_at: string };
export type LifecycleState = {
  preferences: {covenant_word:string; window_start:string; window_end:string; wait_minutes:number; day_change_hour:number; automatic_digest:boolean; autonomous_wander:boolean; idle_minutes:number; sentinel_enabled:boolean};
  user_status: {status:string;detail:string}; active_day: {date:string} | null;
  reservation: {due_at:string;deferred_to_midnight:boolean} | null; error:string;
  days: Array<{date:string;state:string;jobs:Array<{stage:string;state:string;result:Record<string,unknown>} | null>}>;
};
export type LocalState = { configured: boolean; profile: Profile; messages: Array<{
  message_id: string; role: string; content: string; timestamp: string; event_type: string; tool_calls?: string;
}>; unfinished_requests?: Array<{request_id: string; state: string; error: string}>;
  lifecycle?: LifecycleState;
  wander: {running: boolean; stopping: boolean; error: string}; };
// Never retry a write: reconnect/reload reads authoritative state first.
export async function request(path: string, body?: unknown, method = 'POST') {
  const response = await fetch(getApiBase() + path, body === undefined ? {} : {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '请求未完成，请刷新核对结果';
    try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; } catch { /* no response body */ }
    throw Object.assign(new Error(detail), {status: response.status});
  }
  return response.json();
}
