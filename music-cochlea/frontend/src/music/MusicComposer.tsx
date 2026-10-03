import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { LongTextCard } from '../shared/types';
import { fromCard, toCard, type Song } from './types';
import { musicApi } from './api';
import './music.css';
import { useMusicDialog } from './useMusicDialog';
export default function MusicComposer({ initial, initialSong, onClose, onSave, saveLabel = '添加到输入框' }: { saveLabel?: string; initial?: LongTextCard | null; initialSong?: Song; onClose: () => void; onSave: (card: LongTextCard) => void }) {
  const dialog = useMusicDialog(onClose);
  const [text, setText] = useState(initial?.link || initialSong?.link || '');
  const [note, setNote] = useState(initial?.body === '分享一首歌' ? '' : initial?.body || '');
  const [song, setSong] = useState<Song | null>(initial?.kind === 'music' ? fromCard(initial) : initialSong || null);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<Song[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); return () => { generation.current++; }; }, []);
  async function resolve(value = text) {
    if (!value.trim()) return;
    const version = ++generation.current; setBusy(true); setError(''); setSong(null);
    try {
      const result = await musicApi('/resolve', { text: value });
      if (version !== generation.current) return;
      if (result.kind === 'playlist' && result.playlist) {
        const playlist = result.playlist;
        setSong({ id: playlist.id, playlist_id: playlist.id, kind: 'playlist', name: playlist.name,
          artist: '', cover: playlist.cover, duration: 0, link: playlist.link,
          track_count: playlist.songs?.length || playlist.track_count || 0,
          track_preview: (playlist.songs || []).slice(0, 8).map((item: Song) => `${item.name}—${item.artist}`).join('；'), play_mode: 'loop' });
      } else if (result.kind === 'song' && result.song) setSong({ ...result.song, kind: 'song' });
      else throw new Error('没有识别到歌曲或歌单');
    } catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function search() {
    const version = ++generation.current; setBusy(true); setError('');
    try { const result = await musicApi('/search?q=' + encodeURIComponent(query)); if (version === generation.current) setMatches(result.songs || []); }
    catch (e) { if (version === generation.current) setError((e as Error).message); }
    finally { if (version === generation.current) setBusy(false); }
  }
  async function save() {
    if (!song || busy) return;
    setBusy(true); setError('');
    try {
      let ready = song;
      if (song.kind !== 'playlist') {
        const result = await musicApi('/material', { song_id: song.id });
        ready = result.song || song;
      }
      onSave(toCard(ready, note, initial?.id));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return createPortal(<div className="music-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <section ref={dialog} className="music-dialog" role="dialog" aria-modal="true" aria-label="分享歌曲">
      <header><div><small className="music-eyebrow">MUSIC LETTER</small><h2>把这首歌，递给 K</h2><p>一段旋律，也可以是一句话。</p></div><button className="music-close" onClick={onClose} aria-label="关闭">×</button></header>
      <label>网易云链接<div className="music-input-row"><input ref={input} value={text} placeholder="粘贴歌曲链接或分享文字" onChange={e => { generation.current++; setBusy(false); setText(e.target.value); setSong(null); }} onPaste={e => { const value = e.clipboardData.getData('text'); if (value) { e.preventDefault(); setText(value); void resolve(value); } }} /><button className="music-button" onClick={() => resolve()} disabled={busy}>识别</button></div></label>
      {busy && <p className="music-muted" role="status">正在找这首歌…</p>}
      {error && <p className="music-error" role="alert">{error}</p>}
      {song && <div className="music-selected"><div className="music-cover">{song.cover ? <img src={song.cover} alt="" referrerPolicy="no-referrer" /> : '♪'}</div><div><strong>{song.name}</strong><p>{song.kind === 'playlist' ? `${song.track_count || 0} 首歌` : song.artist}</p><small>{song.kind === 'playlist' ? '已识别歌单 · 发出后可以一起听' : '已识别 · 可在下方搜索换曲纠错 · 保存时会附上材料'}</small></div></div>}
      <details><summary>识别不对？搜索换一首</summary><div className="music-input-row"><input value={query} onChange={e => setQuery(e.target.value)} placeholder="歌名 / 歌手" /><button className="music-button" disabled={busy || !query.trim()} onClick={search}>搜索</button></div><div className="music-results">{matches.map(s => <button key={s.id} onClick={() => { generation.current++; setBusy(false); setSong(s); setText(s.link || 'https://music.163.com/#/song?id=' + s.id); setMatches([]); }}><strong>{s.name}</strong><span>{s.artist}{s.cached_share ? ` · 曾共享 ${s.share_count || 1} 次` : ''}</span></button>)}</div></details>
      <label>想附上的话 <small>可选</small><textarea value={note} maxLength={2000} onChange={e => setNote(e.target.value)} placeholder="这首歌让我想起了…" rows={3} /></label>
      <footer className="music-dialog-actions"><button className="music-button" onClick={onClose}>取消</button><button className="music-button primary" disabled={!song || busy} onClick={() => void save()}>{busy && song ? '正在装入材料…' : saveLabel}</button></footer>
    </section>
  </div>, document.body);
}
