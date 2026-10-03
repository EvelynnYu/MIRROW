type MusicNode = {
  music?: {
    title?: string;
    artist?: string;
    song_id?: string;
    selection_origin?: string;
    source_playlist_name?: string;
    playlist_names?: string[];
    playlist_memberships?: Array<{ name: string; subject: 'owner' | 'k' | 'shared' }>;
    playback_requested?: boolean;
    played?: boolean;
    completion_signal?: string;
  } | null;
  music_library_action?: {
    status?: string;
    action?: string;
    playlist_id?: string;
    playlist_name?: string;
    playlist_subject?: string;
    song_id?: string;
    changed?: boolean;
    outcome?: string;
    error?: string;
  } | null;
};

const actionNames: Record<string, string> = {
  create_playlist: '新建 K 的歌单',
  rename_playlist: '修改歌单名',
  delete_playlist: '删除 K 的歌单',
  add_current_song: '把当前歌曲加入歌单',
  remove_song: '从歌单移除歌曲',
  set_default_playlist: '设置漫想默认歌单',
};

export default function WanderMusicReceipt({ node }: { node: MusicNode }) {
  if (!node.music) return null;
  const music = node.music;
  const action = node.music_library_action;
  const playback = music.played
    ? music.completion_signal === 'playback_finished' ? '真实播放及曲尾已确认' : '真实播放已确认'
    : music.playback_requested ? '已请求播放，未取得确认' : '仅分析歌曲材料，未外放';
  const originOwner = music.selection_origin === 'owner_playlist_candidate' ? '使用者'
    : music.selection_origin === 'shared_playlist_candidate' ? '共有'
      : music.selection_origin === 'k_playlist_candidate' ? 'K' : '';
  const selectedFrom = originOwner
    ? `从${originOwner}歌单${music.source_playlist_name ? `《${music.source_playlist_name}》` : ''}的候选中选出`
    : music.selection_origin === 'cache_candidate' ? '从听歌缓存中选出'
      : music.selection_origin === 'search_result' ? '自主搜索找到' : '具体来源未记录';
  const knownMembership = music.playlist_memberships?.length
    ? ` · 本地歌单记录：${music.playlist_memberships.slice(0, 3).map(item => `${item.subject === 'owner' ? '使用者' : item.subject === 'shared' ? '共有' : 'K'}《${item.name}》`).join('、')}`
    : '';
  const actionStatus = action
    ? `${actionNames[action.action || ''] || '歌单操作'}：${action.status === 'completed' && action.action === 'add_current_song' && action.changed === false ? '原本就在歌单，未新增' : action.status === 'completed' && action.action === 'add_current_song' && action.changed === true ? '网易云已确认新增' : action.status === 'completed' ? '已确认完成' : action.status === 'error' ? '失败' : action.status === 'rejected' ? '未获准执行' : '结果待确认'}${action.playlist_name ? ` · ${action.playlist_subject === 'shared' ? '共有歌单' : action.playlist_subject === 'k' ? 'K 的歌单' : '歌单'}《${action.playlist_name}》` : ''}${action.error ? ` · ${action.error}` : ''}`
    : '本次没有歌单操作。';
  return <div className="wander-log-node-source">
    <b>音乐事实</b>：{music.title || '未取得歌名'}{music.artist ? ` — ${music.artist}` : ''} · {playback}<br />
    <b>选歌来源</b>：{selectedFrom}{knownMembership}<br />
    <b>歌单行动</b>：{actionStatus}
  </div>;
}
