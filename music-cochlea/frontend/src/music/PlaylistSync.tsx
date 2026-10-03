import { useEffect, useState } from 'react';
import { musicApi, refreshMusic } from './api';
import type { Playlist } from './types';

export const shelfNames: Record<string, string> = { owner: '使用者的', k: 'K 的', shared: '共有的' };

export default function PlaylistSync({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<Playlist[]>([]);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [subjects, setSubjects] = useState<Record<string, string>>({});
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    musicApi('/playlists').then(result => {
      if (!alive) return;
      setItems(result.playlists || []);
      setSubjects(Object.fromEntries((result.bindings || []).map((p: Playlist) => [p.id, p.subject || 'owner'])));
    }).catch(e => { if (alive) setError(e.message); }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, []);
  async function save() {
    setBusy(true); setError('');
    try {
      await musicApi('/playlists/sync', { selections: Object.entries(choices).map(([id, subject]) => ({ id, subject })) });
      await refreshMusic(); onClose();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <section className="music-sync" aria-label="选择同步歌单">
    <div className="music-library-heading"><h3>只带回喜欢的那几张</h3><button className="music-button" disabled={busy} onClick={onClose}>取消</button></div>
    <p className="music-footnote">读取列表不会导入。勾选后选择归属；取消勾选不会移除已导入歌单，也不会修改网易云。</p>
    <input aria-label="筛选网易云歌单" placeholder="找一张歌单…" value={query} onChange={e => setQuery(e.target.value)} />
    {error && <p className="music-error" role="alert">{error}</p>}
    {busy && <p role="status">正在与网易云确认…</p>}
    <div className="music-sync-list">{items.filter(p => p.name.toLowerCase().includes(query.toLowerCase())).map(p => <div className="music-sync-row" key={p.id}>
      <label><input type="checkbox" checked={p.id in choices} disabled={busy} onChange={e => setChoices(old => { const next = { ...old }; if (e.target.checked) next[p.id] = subjects[p.id] || 'owner'; else delete next[p.id]; return next; })} /><span>{p.name}<small>{subjects[p.id] ? '已在 MIRROW · ' + shelfNames[subjects[p.id]] : '尚未导入'}</small></span></label>
      <select aria-label={p.name + '的归属'} disabled={busy || !(p.id in choices)} value={choices[p.id] || subjects[p.id] || 'owner'} onChange={e => setChoices(old => ({ ...old, [p.id]: e.target.value }))}>{Object.entries(shelfNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
    </div>)}</div>
    <button className="music-button primary" disabled={busy || !Object.keys(choices).length} onClick={save}>同步选中的 {Object.keys(choices).length} 张</button>
  </section>;
}
