import { getPlatform } from '../shared/config';
import { useEffect, useState } from 'react';
import { fromCard, type Song } from './types';
import type { LongTextCard } from '../shared/types';
import { musicApi, openMusic, playSong, refreshMusic, useMusic } from './api';
import './music.css';
export default function MusicCard({ card, song: supplied, note }: { card?: LongTextCard; song?: Song; note?: string }) {
  const projected = supplied || (card ? fromCard(card) : null);
  const [material, setMaterial] = useState<Partial<Song>>({});
  const song = projected ? { ...projected, ...material, id: projected.id, name: projected.name, artist: projected.artist } : null;
  const { data, error: disconnected } = useMusic();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    setMaterial({});
    if (!projected || projected.kind === 'playlist' || projected.melody_summary) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const poll = async () => {
      try {
        const result = await musicApi('/material/' + projected.id);
        if (cancelled) return;
        const next = result.song || {};
        setMaterial({ lyrics_available: next.lyrics_available || projected.lyrics_available,
          lyrics_excerpt: next.lyrics_excerpt || projected.lyrics_excerpt,
          melody_summary: next.melody_summary || projected.melody_summary,
          material_status: next.material_status === 'missing' ? projected.material_status : next.material_status });
        if (result.analysis_pending && !next.melody_summary && attempts++ < 12) timer = setTimeout(poll, 5000);
      } catch { /* The card identity remains usable when optional material polling fails. */ }
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [projected?.id, projected?.kind, projected?.melody_summary]);
  if (!song) return null;
  const session = data?.session;
  const shareNumber = Number(song.share_number || card?.share_number || 0);
  const letterLabel = shareNumber
    ? (card ? `第 ${shareNumber} 次，把这首歌递给 K` : `第 ${shareNumber} 次，把这首歌递给你`)
    : (card ? '一首歌，递给 K' : '一首歌，递给你');
  const expectedId = song.playlist_id || song.id;
  const sessionMatches = song.kind === 'playlist' ? session?.playlist_id === expectedId : session?.song?.id === song.id;
  const current = session?.device === (getPlatform() === 'mobile' ? 'mobile' : 'computer') && sessionMatches && !['ended', 'external', 'failed'].includes(session.status);
  const playing = current && session?.status === 'playing' && !disconnected;
  async function toggle() {
    if (!song) return;
    setBusy(true); setError('');
    try {
      if (current && session && session.status !== 'waiting_next') await musicApi('/control', { action: playing ? 'pause' : 'resume', session_id: session.id });
      else await playSong(song);
      await refreshMusic();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <article className="music-card">
    <div className="music-card-main">
      <div className="music-cover">{song.cover ? <img src={song.cover} alt="" loading="lazy" referrerPolicy="no-referrer" onError={e => { e.currentTarget.style.display = 'none'; }} /> : <span>♪</span>}</div>
      <div className="music-card-copy"><small>{song.kind === 'playlist' ? '一张歌单，邀请一起听' : letterLabel}</small><strong>{song.name}</strong><span>{song.kind === 'playlist' ? `${song.track_count || 0} 首 · ${song.play_mode === 'list' ? '顺序播放' : song.play_mode === 'one' ? '单曲循环' : '列表循环'}` : song.artist}</span></div>
      <button className="music-play" type="button" disabled={busy} onClick={toggle} aria-label={playing ? '暂停' : '播放'}>{busy ? '⋯' : playing ? 'Ⅱ' : '▶'}</button>
    </div>
    {(note || (card?.body !== '分享一首歌' && card?.body !== '分享一张歌单' && card?.body)) && <p className="music-card-note">{note || card?.body}</p>}
    {song.track_preview && <p className="music-card-preview">{song.track_preview}</p>}
    {song.kind !== 'playlist' && <div className="music-material-badges"><span>{song.lyrics_available ? '歌词材料已附' : '歌词材料暂缺'}</span><span>{song.melody_summary ? '旋律分析已附' : '旋律分析待补'}</span></div>}
    <footer><span>{current ? disconnected ? '播放状态待确认' : (session?.device === 'mobile' ? '手机' : '电脑') + ' · ' + statusName(session?.status || '') : song.kind === 'playlist' ? '轻触开始一起听' : '轻触播放 · 单曲曲尾停止'}</span>{song.kind === 'playlist' ? <button onClick={() => openMusic('playlists')}>打开歌单 ›</button> : <button onClick={() => openMusic('playlists', song)}>收进歌单 ›</button>}</footer>
    {error && <p className="music-error" role="alert">{error}</p>}
  </article>;
}
export function statusName(status: string) { return ({ playing: '一起听着', paused: '已暂停', waiting_next: '等你选下一首', starting: '指令已送达 · 等待播放器确认', unknown: '状态待确认', ended: '已结束', external: '已由网易云接管', failed: '播放未完成' } as Record<string, string>)[status] || status; }
