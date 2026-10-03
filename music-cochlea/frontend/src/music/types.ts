import type { LongTextCard } from '../shared/types';
export type Song = { id: string; name: string; artist: string; album?: string; cover?: string; duration: number; origin?: 'manual_in_shared_session'; link?: string; kind?: 'song' | 'playlist'; playlist_id?: string; track_count?: number; track_preview?: string; play_mode?: 'list' | 'loop' | 'one'; cached_share?: boolean; share_count?: number; share_number?: number; shared_by?: 'owner' | 'k'; lyrics_available?: boolean; lyrics_excerpt?: string; melody_summary?: string; material_status?: string };
export type Playlist = { id: string; name: string; subject?: string; cover?: string; track_count?: number; owned?: boolean; wander_default?: boolean };
export type Playback = { id: string; device: string; mode: string; quiet: boolean; follow_external?: boolean; status: string; song: Song; queue: Song[]; queue_index: number; playlist_id?: string | null; position_ms: number; observed_at?: string; error?: string; pause_since?: string | null; pause_timeout_seconds?: number };
export type MusicStatus = { session: Playback | null; playlists: Playlist[]; capabilities: Record<string, any> };
export function toCard(song: Song, note = '', id?: string): LongTextCard {
  if (song.kind === 'playlist') return {
    id: id || globalThis.crypto?.randomUUID?.() || 'music_' + Date.now(), kind: 'music', music_type: 'playlist',
    title: song.name, body: note || '分享一张歌单', playlist_id: song.playlist_id || song.id,
    artist: '', cover: song.cover || '', duration: '0', track_count: String(song.track_count || 0),
    track_preview: song.track_preview || '', play_mode: song.play_mode || 'loop',
    link: 'https://music.163.com/#/playlist?id=' + (song.playlist_id || song.id),
  };
  return { id: id || globalThis.crypto?.randomUUID?.() || 'music_' + Date.now(), kind: 'music', title: song.name,
    body: note || '分享一首歌', song_id: song.id, artist: song.artist, cover: song.cover || '',
    duration: String(song.duration || 0), link: 'https://music.163.com/#/song?id=' + song.id,
    music_type: 'song', lyrics_available: song.lyrics_available ? '1' : '0',
    lyrics_excerpt: song.lyrics_excerpt || '', melody_summary: song.melody_summary || '',
    material_status: song.material_status || 'missing' };
}
export function fromCard(card: LongTextCard): Song {
  if (card.music_type === 'playlist' || card.playlist_id) return {
    id: card.playlist_id || '', playlist_id: card.playlist_id || '', kind: 'playlist', name: card.title,
    artist: '', cover: card.cover, duration: 0, link: card.link, track_count: Number(card.track_count || 0),
    track_preview: card.track_preview || '', play_mode: card.play_mode || 'loop', shared_by: 'owner',
  };
  return { id: card.song_id || '', name: card.title, artist: card.artist || '', cover: card.cover,
    duration: Number(card.duration || 0), link: card.link,
    kind: 'song', share_number: Number(card.share_number || 0), share_count: Number(card.share_count || 0), shared_by: 'owner',
    lyrics_available: card.lyrics_available === '1', lyrics_excerpt: card.lyrics_excerpt || '',
    melody_summary: card.melody_summary || '', material_status: card.material_status || 'missing' };
}
