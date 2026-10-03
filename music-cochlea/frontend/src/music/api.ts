import { useSyncExternalStore } from 'react';
import { getApiBase, getPlatform, getAccessHeaders } from '../shared/config';
import type { MusicStatus, Song } from './types';
export async function musicApi<T = any>(path: string, data?: unknown, method?: string): Promise<T> {
  const response = await fetch(getApiBase() + '/api/music/v2' + path, {
    method: method || (data === undefined ? 'GET' : 'POST'),
    headers: { ...getAccessHeaders(), ...(data === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: data === undefined ? undefined : JSON.stringify(data),
    signal: AbortSignal.timeout(30000),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '这次音乐操作没有完成，请重试');
  // Apply the actual command receipt immediately, including while the WebView is hidden.
  // An older in-flight status poll must not overwrite it and cause another /play.
  if (data !== undefined && Object.prototype.hasOwnProperty.call(result, 'session')) {
    revision++;
    snapshot = { data: { ...(snapshot.data || { playlists: [], capabilities: {} }), session: result.session }, error: '' };
    listeners.forEach(fn => fn());
  }
  return result as T;
}
type Snapshot = { data: MusicStatus | null; error: string };
let snapshot: Snapshot = { data: null, error: '' };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;
let pending: Promise<void> | undefined;
let revision = 0;
export function refreshMusic(): Promise<void> {
  if (pending) return pending;
  const observedRevision = revision;
  pending = (async () => {
    try { const data = await musicApi<MusicStatus>('/status'); if (observedRevision === revision) snapshot = { data, error: '' }; }
    catch (error) { if (observedRevision === revision) snapshot = { data: snapshot.data, error: (error as Error).message }; }
    finally { pending = undefined; listeners.forEach(fn => fn()); }
  })();
  return pending;
}
function onVisible() { if (!document.hidden) void refreshMusic(); }
function subscribe(fn: () => void) {
  listeners.add(fn);
  if (!timer) { void refreshMusic(); timer = setInterval(() => { if (!document.hidden) void refreshMusic(); }, 2500); document.addEventListener('visibilitychange', onVisible); }
  return () => { listeners.delete(fn); if (!listeners.size && timer) { clearInterval(timer); timer = undefined; document.removeEventListener('visibilitychange', onVisible); } };
}
export function useMusic() { return useSyncExternalStore(subscribe, () => snapshot); }
export async function playSong(song: Song) {
  const device = getPlatform() === 'mobile' ? 'mobile' : 'computer';
  try {
    if (song.kind === 'playlist') {
      await musicApi('/play', { playlist_id: song.playlist_id || song.id, device, mode: song.play_mode || 'loop' });
    } else {
      await musicApi('/play', { song_id: song.id, device, mode: 'single' });
    }
  } catch (error) {
    // Android backgrounds the WebView while NetEase opens.  The browser-side
    // fetch can be suspended even though the backend already committed the
    // command receipt.  Reconcile once before showing a failure or redirecting.
    const matchesCommittedSession = () => {
      const session = snapshot.data?.session;
      const matches = song.kind === 'playlist'
        ? session?.playlist_id === (song.playlist_id || song.id)
        : session?.song?.id === song.id;
      return !!matches && session?.device === device
        && !['ended', 'external', 'failed'].includes(session?.status || '');
    };
    await refreshMusic();
    // The first refresh may itself be an older poll that began before /play.
    // Once it settles, make one fresh read before treating the dispatched
    // command as failed.  This is especially common when Android hides the
    // WebView while opening NetEase.
    if (!matchesCommittedSession()) await refreshMusic();
    if (!matchesCommittedSession()) throw error;
  }
  await refreshMusic();
}
export function openMusic(section = 'player', song?: Song) { window.dispatchEvent(new CustomEvent('mirrow:music-open', { detail: { section, song } })); }
export function composeMusic(song?: Song) { window.dispatchEvent(new CustomEvent('mirrow:music-compose', { detail: { song } })); }
