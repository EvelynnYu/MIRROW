import { useEffect, useState } from 'react';
import { cognitionRequest } from '../shared/cognitionApi';
type MusicRecord = { id: string; title: string; artist: string; subject_id: string; source: string; mode: string; reaction: string; dimensions: Record<string, string>; created_at: string };

const subjects: Record<string, string> = { k: 'K', owner: '使用者', collaborator: '协作者', shared: 'K 与使用者', unknown: '未标注' };

export default function MusicExperiences({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const [records, setRecords] = useState<MusicRecord[]>([]);
  const [dimensions, setDimensions] = useState<string[]>([]);
  const [musicSubject, setMusicSubject] = useState('k');
  const [title, setTitle] = useState('');
  const [artist, setArtist] = useState('');
  const [reaction, setReaction] = useState('');
  const [notes, setNotes] = useState<Record<string, string>>({});

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  useEffect(() => {
    let active = true;
    setBusy(true);
    cognitionRequest('/music').then(data => { if (active) { setRecords(data.records); setDimensions(data.dimensions); } })
      .catch(e => { if (active) setMessage(e.message); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  async function saveMusic() {
    setBusy(true); setMessage('');
    try {
      const data = await cognitionRequest('/music', { title, artist, subject_id: musicSubject, reaction, dimensions: notes });
      onDirtyChange?.(false); setRecords(old => [data.record, ...old]); setTitle(''); setArtist(''); setReaction(''); setNotes({});
      setMessage('已保存记录；不会标记为实际播放，也不会覆盖自我书。');
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }


  return <section aria-label="音乐体验记录" onChangeCapture={() => onDirtyChange?.(true)}>
    {busy && <p role="status">处理中…</p>}
    {message && <p role="status">{message}</p>}

        <p>记录听过什么、有什么感受。</p>
        <details><summary>＋ 记录一次音乐体验</summary><label>谁的感受<select value={musicSubject} disabled={busy} onChange={e => setMusicSubject(e.target.value)}>{Object.entries(subjects).filter(([k]) => k !== 'collaborator').map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>歌曲<input aria-label="歌曲" value={title} disabled={busy} onChange={e => setTitle(e.target.value)} /></label>
        <label>歌手<input value={artist} disabled={busy} onChange={e => setArtist(e.target.value)} /></label>
        <label>想说的话<textarea value={reaction} disabled={busy} onChange={e => setReaction(e.target.value)} /></label>
        <details><summary>再记细一点（可不填）</summary><div className="cognition-diff">{dimensions.map(d => <label key={d}>{d}<input value={notes[d] || ''} disabled={busy} onChange={e => setNotes(old => ({ ...old, [d]: e.target.value }))} /></label>)}</div></details>
        <button disabled={busy || !title.trim()} onClick={saveMusic}>保存手动音乐记录</button></details>
        {records.map(r => <article key={r.id}><h3>{r.title} · {r.artist}</h3><p>{subjects[r.subject_id]} · {r.mode === 'observed_playback' ? '实际播放观察' : r.mode === 'analysis' ? '材料分析' : '手动记录'} · {r.created_at}</p><pre>{r.reaction}</pre>{Object.entries(r.dimensions).map(([k, v]) => <p key={k}>{k}：{v}</p>)}<small>{r.source}</small></article>)}
  </section>;
}
