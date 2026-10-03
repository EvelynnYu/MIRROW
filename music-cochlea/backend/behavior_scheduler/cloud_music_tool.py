import re
"""Active v2 music tool. Retired desktop helpers are omitted."""
from .base_tool import BaseTool, ToolResult, ToolStatus

class CloudMusicTool(BaseTool):
    name = 'cloud_music'
    description = '按当前消息来源控制电脑或手机上的网易云音乐 App，支持搜索播放、推荐和播放控制。'
    flash_description = '网易云歌曲卡片与歌单——分享推荐、按来源设备单曲播放、读取已绑定歌单曲目、建歌单加减歌、安静陪听、读取歌词和保存感想'
    pro_guide = 'share/recommend_by_style(query=具体歌名和歌手，或song_id) 异步寄出歌曲卡片，不自动播放；playlist_share(playlist_id，mode=list/loop/one) 异步寄出可一起听的歌单卡片；search_play(song_id 或 query，mode=single/one) 点播，默认单曲播完停止；resume 只恢复当前暂停的共听会话。playlists 查询已绑定歌单及ID；playlist_tracks(playlist_id 或唯一歌单名 query，可选 subject=owner/k/shared、offset/limit，每页最多30首) 读取已绑定歌单的真实曲目 ID、歌名、歌手，可根据使用者歌单中的曲目推荐；playlist_create(query=歌单名,subject=owner/k/shared) 建私密歌单；playlist_rename(query=新名,playlist_id) 改名；playlist_delete(playlist_id) 只删除 K 唱片架中且由当前账号创建的网易云歌单；playlist_add/playlist_remove 传 playlist_id，并用歌曲卡片中的 song_id 或明确歌名 query 定位歌曲，K 与共有歌单可编辑。session_mode(mode=single/list/loop/one) 调整当前 MIRROW 共听播放策略；session_follow(follow_external=true/false) 开关持续一起听。playlist_wander_default(playlist_id,selected=true) 指定 K 漫想时的默认收藏处。想把卡片里的歌存进歌单或设置循环时，需实际调用相应动作；没有成功回执只能表达想法，不能称已保存或已设置。独立操作成功仅展示回执；需要用结果继续行动（如查歌单曲目后推歌）传 continue_after=true。查询和失败结果回注。inspect(song_id 或具体 query) 读该歌曲歌词，不能只传歌单归属；note 用 reaction 保存材料感想；quiet 设置安静偏好；end 结束共同播放。'
    ui_only_actions = frozenset({'share', 'recommend_by_style', 'playlist_share', 'playlist_create', 'playlist_rename', 'playlist_delete', 'playlist_add', 'playlist_remove', 'playlist_play', 'playlist_wander_default', 'session_mode', 'session_follow', 'play', 'resume', 'pause', 'next', 'end', 'quiet', 'like', 'search_play'})

    def get_result_delivery(self, **kwargs):
        result = kwargs.get('_result')
        if kwargs.get('_execution_failed') or (result is not None and result.status != ToolStatus.SUCCESS):
            return 'model'
        if kwargs.get('continue_after'):
            return 'model'
        return super().get_result_delivery(**kwargs)
    parameters_schema = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['search_play', 'now_playing', 'recommend_by_style', 'music_login', 'resume', 'pause', 'next', 'like', 'share', 'playlist_share', 'inspect', 'note', 'quiet', 'end', 'playlists', 'playlist_tracks', 'playlist_create', 'playlist_rename', 'playlist_delete', 'playlist_add', 'playlist_remove', 'playlist_play', 'playlist_wander_default', 'session_mode', 'session_follow']}, 'query': {'type': 'string'}, 'song_id': {'type': 'string'}, 'playlist_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30}, 'subject': {'type': 'string', 'enum': ['owner', 'k', 'shared']}, 'mode': {'type': 'string', 'enum': ['single', 'list', 'loop', 'one']}, 'quiet': {'type': 'boolean'}, 'follow_external': {'type': 'boolean'}, 'selected': {'type': 'boolean'}, 'reaction': {'type': 'string'}, 'reason': {'type': 'string', 'maxLength': 300, 'description': '可选：K 此次建歌单或改歌单的简短理由，作为 K 的感想留存；没有就留空'}, 'continue_after': {'type': 'boolean', 'description': '本次结果还用于后续行动（如建歌单后继续加歌）时设 true；否则独立操作成功仅展示 UI 回执'}}, 'required': ['action']}

    def get_user_facing_description(self, **kwargs) -> str:
        action = kwargs.get('action', '')
        query = kwargs.get('query', '')
        m = {'play': '正在播放', 'resume': '正在恢复播放', 'pause': '暂停播放', 'next': '切到下一首', 'playlist_tracks': '正在查看歌单曲目', 'like': '已收藏', 'search_play': f'正在搜索「{query}」并播放' if query else '正在搜索播放', 'now_playing': '正在查看当前播放', 'recommend_by_style': '正在为你推歌', 'playlist_delete': '正在删除 K 的网易云歌单', 'session_mode': '正在调整 MIRROW 共听播放方式', 'session_follow': '正在调整持续一起听', 'music_login': '正在准备登录'}
        return m.get(action, f'正在使用网易云音乐: {action}')

    async def execute(self, action: str, query: str='', **kwargs) -> ToolResult:
        if action in {'share', 'recommend_by_style', 'playlist_share'}:
            from music_system.sharing import enqueue
            return await enqueue(query, kwargs.get('song_id'), kwargs.get('_session_id', ''), kwargs.get('playlist_id') if action == 'playlist_share' else None, mode=kwargs.get('mode'), action='playlist_share' if action == 'playlist_share' else 'share')
        from music_system.tools import execute
        result = await execute(action, query, **kwargs)
        terminal = {'playlist_create', 'playlist_rename', 'playlist_delete', 'playlist_add', 'playlist_remove', 'playlist_play', 'playlist_wander_default', 'session_mode', 'session_follow', 'play', 'resume', 'pause', 'next', 'end', 'quiet', 'like', 'search_play'}
        if result.status == ToolStatus.SUCCESS and action in terminal and (not kwargs.get('continue_after', False)):
            result.delivery = 'ui_only'
        return result

    def set_push_reload(self, callback):
        from music_system.sharing import configure
        configure(callback)

    @staticmethod
    def _select_best_song(query: str, songs: list[dict]) -> dict | None:
        """Prefer candidates whose explicit title/artist facts occur in the query."""
        if not songs:
            return None

        def normalize(value: str) -> str:
            return re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', str(value or '').lower())
        normalized_query = normalize(query)

        def score(song: dict) -> tuple[int, int]:
            name = normalize(song.get('name', ''))
            artist = normalize(song.get('artist', ''))
            value = 0
            if name and name == normalized_query:
                value += 120
            elif name and name in normalized_query:
                value += 70
            if artist and artist in normalized_query:
                value += 60
            if name and artist and (name + artist in normalized_query):
                value += 30
            return (value, -songs.index(song))
        return max(songs, key=score)
