import { getApiBase } from '../config';
export type Profile = { human_name: string; agent_name: string; persona: string; relationship_started_at: string };
export type LocalState = { configured: boolean; profile: Profile; messages: Array<{
  message_id: string; role: string; content: string; timestamp: string; event_type: string;
}>; unfinished_requests?: Array<{request_id: string; state: string; error: string}>;
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
