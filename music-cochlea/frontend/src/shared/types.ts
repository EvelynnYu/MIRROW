/**
 * 前端共享类型（开源精简版）
 *
 * 只保留音乐组件实际消费的类型。你接入自己的系统时，把这里的类型替换成
 * 你自己的消息/卡片模型即可——字段名保持一致，组件无需改动。
 */

/**
 * 长文本卡片：消息气泡里可展开的结构化卡片。
 *
 * 音乐相关字段（kind === 'music'）：
 * - music_type      'song' 单曲 | 'playlist' 歌单
 * - song_id / playlist_id  网易云 ID
 * - cover/duration/artist  展示用
 * - track_count / track_preview  歌单卡片预览
 * - play_mode       播放模式 list | loop | one
 * - lyrics_available / lyrics_excerpt / melody_summary  材料状态（歌词、旋律摘要）
 * - material_status 材料准备状态
 * - share_number / share_count  分享序号登记
 */
export type LongTextCard = {
  id: string;
  title: string;
  body: string;
  kind?: 'music' | 'xhs';
  music_type?: 'song' | 'playlist';
  song_id?: string;
  playlist_id?: string;
  artist?: string;
  cover?: string;
  duration?: string;
  link?: string;
  post_key?: string;
  link_status?: string;
  source_status?: string;
  node_id?: string;
  comment_draft?: string;
  share_number?: string;
  share_count?: string;
  track_count?: string;
  track_preview?: string;
  play_mode?: 'list' | 'loop' | 'one';
  lyrics_available?: string;
  lyrics_excerpt?: string;
  melody_summary?: string;
  material_status?: string;
};
