import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { composeMusic, musicApi, playSong, refreshMusic, useMusic } from './api';
import { statusName } from './MusicCard';
import type { Playlist, Song } from './types';
import './music.css';
import { useMusicDialog } from './useMusicDialog';
import PlaylistSync, { shelfNames } from './PlaylistSync';
export default function MusicHub({ section, onClose, songToAdd }: { section: string; songToAdd?: Song; onClose: () => void }) {
  const dialog = useMusicDialog(onClose);
  const { data, error: connectionError } = useMusic();
  const [tab, setTab] = useState(section === 'playlists' ? 'playlists' : section === 'history' ? 'history' : section === 'shared' ? 'shared' : 'player');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState('');
  const [name, setName] = useState('');
  const [subject, setSubject] = useState('shared');
  const [selected, setSelected] = useState<Playlist | null>(null);
  const [renameName, setRenameName] = useState('');
  const [songs, setSongs] = useState<Song[]>([]);
  const [search, setSearch] = useState('');
  const [found, setFound] = useState<Song[]>([]);
  const [history, setHistory] = useState<any>(null);
  const [sharedSongs, setSharedSongs] = useState<Song[]>([]);
  const [sharedSearch, setSharedSearch] = useState('');
  const [qr, setQr] = useState('');
  const [syncOpen, setSyncOpen] = useState(false);
  const [shelf, setShelf] = useState('all');
  const [playlistMode, setPlaylistMode] = useState('loop');
  const session = data?.session;
  const live = session && !['ended', 'external', 'failed'].includes(session.status);
  const canNext = !!session && session.status !== 'waiting_next'
    && session.song?.origin !== 'manual_in_shared_session' && session.queue.length > 1
    && (session.queue_index + 1 < session.queue.length || session.mode === 'loop');
  async function run(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await fn(); await refreshMusic(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function loadPlaylist(p: Playlist) {
    const result = await musicApi('/playlists/' + p.id);
    const binding = data?.playlists.find(b => b.id === p.id) || p;
    const item = { ...result.playlist, ...binding, name: result.playlist.name };
    setSelected(item); setRenameName(item.name); setSongs(result.songs || []);
  }
  function selectedPlaylistCard(): Song | null {
    if (!selected) return null;
    return {
      id: selected.id,
      playlist_id: selected.id,
      kind: 'playlist',
      name: selected.name,
      artist: '',
      cover: selected.cover,
      duration: 0,
      track_count: songs.length,
      track_preview: songs.slice(0, 8).map(item => `${item.name}—${item.artist}`).join('；'),
      play_mode: playlistMode === 'one' ? 'one' : playlistMode === 'list' ? 'list' : 'loop',
      link: `https://music.163.com/playlist?id=${selected.id}`,
    };
  }
  async function control(action: string) { if (session) await musicApi('/control', { action, session_id: session.id }); }
  async function setting(patch: Record<string, unknown>) { if (session) await musicApi('/session', { ...patch, session_id: session.id }, 'PATCH'); }
  useEffect(() => { if (tab === 'history') musicApi('/history').then(setHistory).catch(e => setError(e.message)); }, [tab]);
  useEffect(() => { if (tab === 'shared') musicApi('/shared-songs').then(result => setSharedSongs(result.songs || [])).catch(e => setError(e.message)); }, [tab]);
  return createPortal(<div className="music-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <section ref={dialog} className="music-hub music-dialog" role="dialog" aria-modal="true" aria-label="音乐中枢">
      <header><div><small className="music-eyebrow">OUR LITTLE RECORD ROOM</small><h2>把日子，听成一首歌</h2><p>你的循环，他的唱片，还有一起听过的时光。</p></div><button className="music-close" aria-label="关闭音乐中枢" onClick={onClose}>×</button></header>
      <nav className="music-tabs">{[['player','此刻一起听'],['playlists','我们的歌单'],['shared','共享歌库'],['history','听歌手记']].map(([id,label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}</nav>
      {(error || connectionError) && <p className="music-error" role="alert">{error || connectionError}</p>}
      {session?.error && <p className="music-error" role="status">{session.error}</p>}
      {notice && <p className="music-notice" role="status">{notice}</p>}
      <div className="music-hub-scroll">
      {tab === 'player' && <>
        <div className="music-now">
          <div className="music-record"><div className="music-record-label">{session?.song?.cover ? <img src={session.song.cover} alt="" referrerPolicy="no-referrer" /> : <span>♪</span>}</div></div>
          <small className="music-eyebrow">{live ? '此刻的旋律' : '留一点时间给音乐'}</small>
          <h3>{session?.song?.name || '今天，想听哪一首？'}</h3><p>{session?.song?.artist || '从歌单开始，或分享一首喜欢的歌。'}</p>
          {session && <><p className="music-muted">{connectionError ? '连接中断 · 状态待确认' : statusName(session.status)} · {session.device === 'mobile' ? '手机网易云' : '电脑网易云'}</p><progress value={session.position_ms || 0} max={session.song?.duration || 1} aria-label="已确认的播放进度" /><small>{Math.floor((session.position_ms || 0) / 60000)}:{String(Math.floor((session.position_ms || 0) / 1000) % 60).padStart(2, '0')} / {Math.floor((session.song?.duration || 0) / 60000)}:{String(Math.floor((session.song?.duration || 0) / 1000) % 60).padStart(2, '0')}</small></>}
          {live ? <div className="music-player-actions">{session.status === 'waiting_next' ? <span className="music-muted">这首已停；在网易云选下一首，确认播放后继续一起听。</span> : session.song?.origin === 'manual_in_shared_session' ? <span className="music-muted">这首由你在网易云选定，播放控制留在网易云。</span> : <><button className="music-button primary" disabled={busy || !!connectionError} onClick={() => run(() => control(session.status === 'playing' ? 'pause' : 'resume'))}>{session.status === 'playing' ? '暂停' : '继续播放'}</button><button className="music-button" disabled={busy || !canNext || !!connectionError} title={canNext ? '' : '当前队列没有下一首'} onClick={() => run(() => control('next'))}>下一首</button></>}<button className="music-button" disabled={busy || !!connectionError} onClick={() => run(() => control('end'))}>结束一起听</button></div> : <button className="music-button primary" onClick={() => { onClose(); composeMusic(); }}>分享一首歌</button>}
        </div>
        {live && <div className="music-preferences"><label>播放方式<select value={session.mode} disabled={busy || session.status === 'waiting_next' || session.song?.origin === 'manual_in_shared_session'} onChange={e => run(() => setting({ mode: e.target.value }))}><option value="single">单曲曲尾停止</option><option value="list">列表顺序播放</option><option value="loop">列表循环</option><option value="one">单曲循环</option></select></label><label>暂停多久后结束共同听歌<select value={session.pause_timeout_seconds ?? 1800} disabled={busy} onChange={e => run(() => setting({ pause_timeout_seconds: Number(e.target.value) }))}><option value={300}>5 分钟</option><option value={900}>15 分钟</option><option value={1800}>30 分钟</option><option value={3600}>1 小时</option><option value={0}>不自动结束</option></select></label><label className="music-quiet"><span><strong>持续一起听</strong><small>单曲曲尾停止时等你选下一首；曲中手动换歌也会在确认播放后跟随。</small></span><input type="checkbox" checked={!!session.follow_external} disabled={busy} onChange={e => run(() => setting({ follow_external: e.target.checked }))} /></label><label className="music-quiet"><span><strong>安静陪听</strong><small>少一点主动打扰，思绪与陪伴继续。</small></span><input type="checkbox" checked={session.quiet} disabled={busy} onChange={e => run(() => setting({ quiet: e.target.checked }))} /></label></div>}
        <p className="music-footnote">从 MIRROW 发起的播放属于共同听歌。默认单曲播完便结束；开启“持续一起听”后，手动在网易云换歌也会继续共享，等待或暂停超过所选时间则结束。未开启时，网易云手动换歌仍交还给你。为防止串播，部分设备的曲尾可能提前约 1 秒暂停。</p>
      </>}
      {tab === 'playlists' && <>
        <div className="music-library-heading"><h3>一张张，慢慢收藏</h3><button className="music-button" disabled={busy} onClick={() => setSyncOpen(true)}>选择歌单同步</button></div>
        {syncOpen && <PlaylistSync onClose={() => setSyncOpen(false)} />}
        <nav className="music-tabs" aria-label="歌单归属">{Object.entries({ all: '全部', ...shelfNames }).map(([id, name]) => <button key={id} className={shelf === id ? 'active' : ''} onClick={() => setShelf(id)}>{name}</button>)}</nav>
        <div className="music-shelves">{data?.playlists?.filter(p => shelf === 'all' || p.subject === shelf).map(p => <button key={p.id} className={'music-shelf' + (selected?.id === p.id ? ' active' : '')} onClick={() => run(() => loadPlaylist(p))}><span>♫</span><strong>{p.name}</strong><small>{shelfNames[p.subject || 'shared']}</small></button>)}</div>
        {!data?.playlists?.length && <p className="music-empty">这里还空着。添加一张网易云歌单，或为彼此新建一张。</p>}
        <details className="music-library-editor"><summary>添加或新建歌单</summary><label>放在哪一格<select value={subject} onChange={e => setSubject(e.target.value)}><option value="shared">一起收藏</option><option value="owner">使用者的收藏册</option><option value="k">K 的唱片架</option></select></label><div className="music-input-row"><input value={link} placeholder="已有网易云歌单链接" onChange={e => setLink(e.target.value)} /><button className="music-button" disabled={busy || !link.trim()} onClick={() => run(async () => { await musicApi('/playlists/import', { text: link, subject }); setLink(''); setNotice('歌单已放进收藏架'); })}>添加</button></div><div className="music-input-row"><input value={name} placeholder="给新歌单起个名字" maxLength={100} onChange={e => setName(e.target.value)} /><button className="music-button primary" disabled={busy || !name.trim()} onClick={() => run(async () => { await musicApi('/playlists', { name, subject, privacy: true }); setName(''); setNotice('已在网易云创建歌单'); })}>新建</button></div><p className="music-footnote">新建与加减歌曲需要网易云账号授权。默认创建私密歌单。</p><button className="music-button" onClick={() => run(async () => { const result = await musicApi('/account/login', {}); setQr(result.qr_image || ''); setNotice(result.status === 'success' ? '账号已连接，无需重复扫码' : '请用网易云扫描二维码；同一手机可保存二维码后从相册识别'); })}>连接网易云账号</button>{qr && <><img className="music-login-qr" src={qr} alt="网易云登录二维码" /><button className="music-button" disabled={busy} onClick={() => run(async () => { const result = await musicApi('/account/login'); setNotice(({success:'账号已连接，可以同步歌单',scanned:'已扫码，请在网易云确认',expired:'二维码已过期，请重新获取',waiting:'等待网易云扫码'} as Record<string,string>)[result.status] || '等待确认'); if (result.status === 'success' || result.status === 'expired') setQr(''); })}>我已扫码，检查授权</button></>}</details>
        {songToAdd && <p className="music-notice">待收藏：《{songToAdd.name}》— {songToAdd.artist}。先选一张歌单。</p>}
        {selected && <section className="music-tracklist"><div className="music-library-heading"><h3>{selected.name}</h3><div className="music-playlist-start"><select aria-label="歌单播放方式" value={playlistMode} disabled={busy} onChange={e => setPlaylistMode(e.target.value)}><option value="list">顺序播放</option><option value="loop">列表循环</option><option value="one">单曲循环</option></select><button className="music-button" disabled={busy || !songs.length} onClick={() => { const card = selectedPlaylistCard(); if (card) { onClose(); composeMusic(card); } }}>分享歌单</button><button className="music-button primary" disabled={busy || !songs.length} onClick={() => run(async () => { const card = selectedPlaylistCard(); if (card) await playSong(card); setTab('player'); })}>整张一起听</button></div></div>
          <div className="music-input-row music-playlist-rename"><input aria-label="编辑歌单名" value={renameName} maxLength={100} onChange={e => setRenameName(e.target.value)} /><button className="music-button" disabled={busy || !renameName.trim() || renameName.trim() === selected.name} onClick={() => run(async () => { const result = await musicApi('/playlists/' + selected.id, { name: renameName.trim() }, 'PATCH'); const renamed = { ...selected, ...result.playlist }; setSelected(renamed); setRenameName(renamed.name); setNotice('歌单已经改名为「' + renamed.name + '」'); })}>保存名字</button></div>
          <label>这张歌单属于<select aria-label="当前歌单归属" value={data?.playlists.find(p => p.id === selected.id)?.subject || selected.subject || 'shared'} disabled={busy} onChange={e => run(async () => { await musicApi('/playlists/' + selected.id + '/binding', { subject: e.target.value }, 'PATCH'); setNotice('已调整 MIRROW 归属，网易云所有权不变'); })}>{Object.entries(shelfNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
          <p className="music-footnote">归属是 MIRROW 的收藏分类；网易云实际编辑权限仍由登录账号决定。</p>
          {(data?.playlists.find(p => p.id === selected.id)?.subject || selected.subject) === 'k' && <button className="music-button" disabled={busy} onClick={() => run(async () => { const current = data?.playlists.find(p => p.id === selected.id)?.wander_default || selected.wander_default; await musicApi('/playlists/' + selected.id + '/wander-default', { selected: !current }, 'PATCH'); setSelected({ ...selected, wander_default: !current }); setNotice(current ? '已取消漫想默认收藏处' : '漫想时想留下的歌会优先收进这里'); })}>{(data?.playlists.find(p => p.id === selected.id)?.wander_default || selected.wander_default) ? '✓ 漫想默认收藏处' : '设为漫想默认收藏处'}</button>}
          {(data?.playlists.find(p => p.id === selected.id)?.subject || selected.subject) === 'k' && <button className="music-button music-danger" disabled={busy} onClick={() => { if (confirm('永久删除 K 的网易云歌单「' + selected.name + '」？歌单内的收藏关系也会一起删除，这不是仅从 MIRROW 移出。')) void run(async () => { await musicApi('/playlists/' + selected.id, undefined, 'DELETE'); setSelected(null); setSongs([]); setNotice('网易云已确认删除这张 K 的歌单'); }); }}>删除 K 的网易云歌单</button>}
          <button className="music-button music-unbind" disabled={busy} onClick={() => { if (confirm('只从 MIRROW 收藏架移出「' + selected.name + '」？网易云里的歌单和歌曲都会保留。')) void run(async () => { await musicApi('/playlists/' + selected.id + '/binding', {}, 'DELETE'); setSelected(null); setSongs([]); setNotice('已从 MIRROW 移出；网易云原歌单保持不变'); }); }}>移出 MIRROW</button>
          {songToAdd && <button className="music-button" disabled={busy} onClick={() => run(async () => { await musicApi('/playlists/' + selected.id + '/tracks', {song_ids:[songToAdd.id],operation:'add'}); await loadPlaylist(selected); setNotice('已收进「' + selected.name + '」'); })}>＋ 收进《{songToAdd.name}》</button>}
          {songs.map((s,i) => <div className="music-track" key={s.id + ':' + i}><small>{String(i+1).padStart(2,'0')}</small><div><strong>{s.name}</strong><span>{s.artist}</span></div><button aria-label={'播放' + s.name + '并开始单曲一起听'} title="单曲一起听" disabled={busy} onClick={() => run(async () => { await playSong(s); setTab('player'); })}>▶</button><button aria-label={'从歌单移除' + s.name} disabled={busy} onClick={() => { if (confirm('从「' + selected.name + '」移除「' + s.name + '」？')) void run(async () => { await musicApi('/playlists/' + selected.id + '/tracks', { song_ids: [s.id], operation: 'remove' }); await loadPlaylist(selected); }); }}>−</button></div>)}
          <div className="music-input-row"><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜一首歌，加入这张歌单" /><button className="music-button" disabled={busy || !search.trim()} onClick={() => run(async () => { const result = await musicApi('/search?q=' + encodeURIComponent(search)); setFound(result.songs || []); })}>搜索</button></div>
          <div className="music-results">{found.map(s => <button key={s.id} disabled={busy} onClick={() => run(async () => { await musicApi('/playlists/' + selected.id + '/tracks', { song_ids: [s.id], operation: 'add' }); await loadPlaylist(selected); setNotice('已添加「' + s.name + '」'); })}><strong>{s.name}</strong><span>{s.artist}{s.cached_share ? ` · 曾共享 ${s.share_count || 1} 次` : ''} · ＋</span></button>)}</div>
        </section>}
      </>}
      {tab === 'shared' && <section className="music-shared-library">
        <div className="music-library-heading"><div><small className="music-eyebrow">SHARED AGAIN, REMEMBERED ONCE MORE</small><h3>共享过的歌</h3></div><span>{sharedSongs.length} 首</span></div>
        <p className="music-muted">这里保存真正寄出过的歌曲与次数。重复分享会保留为新的相遇，不会制造重复播放记录。</p>
        <div className="music-input-row"><input value={sharedSearch} onChange={e => setSharedSearch(e.target.value)} placeholder="按歌名或歌手找" /><button className="music-button" disabled={busy} onClick={() => run(async () => { const result = await musicApi('/shared-songs?q=' + encodeURIComponent(sharedSearch)); setSharedSongs(result.songs || []); })}>检索</button></div>
        <div className="music-shared-grid">{sharedSongs.map(song => <article key={song.id} className="music-shared-song"><div className="music-cover">{song.cover ? <img src={song.cover} alt="" referrerPolicy="no-referrer" /> : '♪'}</div><div><strong>{song.name}</strong><span>{song.artist}</span><small>已经共享 {song.share_count || 1} 次</small></div><button className="music-play" disabled={busy} aria-label={'播放' + song.name} onClick={() => run(async () => { await playSong(song); setTab('player'); })}>▶</button><button className="music-button" onClick={() => { onClose(); composeMusic({ ...song, kind: 'song' }); }}>再递一次</button></article>)}</div>
        {!sharedSongs.length && <p className="music-empty">还没有共享过的歌。真正寄出一张歌曲卡片后，它会安静地留在这里。</p>}
      </section>}
      {tab === 'history' && <div className="music-journal"><small className="music-eyebrow">LISTENING DIARY</small><h3>有音乐的日子，才写一页</h3><p className="music-muted">播放、共同听歌与 K 的音乐分析各自注明来源；反复循环不等于反复产生新的偏好。</p>{!history ? <p>正在读取手记…</p> : <>{(history.days || []).map((day: any) => <article key={day.date}><small>{day.date}</small><strong>{({completed:'已整理',error:'整理未完成，可重试',running:'整理中',pending:'待整理'} as Record<string,string>)[day.status] || day.status}</strong><p>{day.summary || day.error || '音乐经历已保存，等待每日整理。'}</p>{day.status === 'error' && <button className="music-button" disabled={busy} onClick={() => run(async () => { await musicApi('/daily/retry', { date: day.date }); setHistory(await musicApi('/history')); })}>重试整理</button>}</article>)}{!(history.days || []).length && <p className="music-empty">还没有每日音乐手记。听过歌后，会在次日整理。</p>}</>}</div>}
      </div>
    </section>
  </div>, document.body);
}
