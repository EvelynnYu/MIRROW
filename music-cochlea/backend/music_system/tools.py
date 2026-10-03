"""K's music actions share the same state and provider as the UI."""
import json
from .service import get_service
from .errors import MusicSystemError, MusicNotFound, SessionConflict
from .materials import prepare

async def execute(action, query="", **kwargs):
    from behavior_scheduler.base_tool import ToolResult, ToolStatus
    from behavior_scheduler.execution_context import get_request_platform
    service = get_service()
    device = "mobile" if get_request_platform() == "mobile" else "computer"
    aliases = {"search_p":"search_play","search":"search_play","recommend":"recommend_by_style","推歌":"recommend_by_style",
               "播放":"play","暂停":"pause","下一首":"next","daily":"daily_recommend"}
    action = aliases.get(action,action)
    # Historical `play` calls sometimes carried a song identity even though
    # the action only resumed a session. Preserve that intent deterministically.
    if action == "play":
        action = "search_play" if kwargs.get("song_id") or str(query).strip() else "resume"
    try:
        sid = kwargs.get("song_id")
        pid = kwargs.get("playlist_id")
        reason = str(kwargs.get("reason") or "")[:300]
        subject = kwargs.get("subject","k")
        if subject not in {"owner","k","shared"}: raise MusicNotFound("歌单归属无效")
        if action == "now_playing":
            from .context import build_context
            return ToolResult(ToolStatus.SUCCESS,build_context() or "没有 MIRROW 共同播放会话")
        if action in {"resume","pause","next","end"}:
            session = service.status()["session"]
            if not session: raise MusicNotFound("没有当前播放会话；点播需要歌曲名或 ID")
            if session["device"] != device: raise SessionConflict("当前音乐在另一台设备；未跨设备控制")
            state = await service.control(action,session["id"])
            return ToolResult(ToolStatus.SUCCESS,"设备操作已确认：" + (state["status"] if state else "共同播放已结束"))
        if action == "quiet":
            session = service.status()["session"]
            if not session: raise MusicNotFound("没有当前音乐会话")
            await service.update_session(session["id"],quiet=bool(kwargs.get("quiet", True)))
            return ToolResult(ToolStatus.SUCCESS,"当前共同播放的安静偏好已更新")
        if action in {"session_mode", "session_follow"}:
            session = service.status()["session"]
            if not session: raise MusicNotFound("没有当前共同播放会话")
            if session["device"] != device: raise SessionConflict("当前音乐在另一台设备；未跨设备控制")
            if action == "session_mode":
                mode = str(kwargs.get("mode") or "")
                if mode not in {"single", "list", "loop", "one"}:
                    raise MusicNotFound("需要明确的播放方式")
                if session["song"].get("origin") == "manual_in_shared_session":
                    raise MusicNotFound("这首歌由网易云手动选择，不能设置 MIRROW 队列循环")
                await service.update_session(session["id"], mode=mode)
                return ToolResult(ToolStatus.SUCCESS, "MIRROW 共同播放方式已设置为：" + {
                    "single": "单曲播完停止", "list": "顺序播放", "loop": "列表循环", "one": "单曲循环",
                }[mode])
            if not isinstance(kwargs.get("follow_external"), bool):
                raise MusicNotFound("需要明确开启或关闭持续一起听")
            await service.update_session(session["id"], follow_external=kwargs["follow_external"])
            return ToolResult(ToolStatus.SUCCESS, "持续一起听已" + ("开启" if kwargs["follow_external"] else "关闭"))
        if action == "playlists":
            return ToolResult(ToolStatus.SUCCESS,json.dumps(service.store.bindings(),ensure_ascii=False))
        if action == "playlist_tracks":
            bindings = service.store.bindings()
            if pid:
                matches = [item for item in bindings if str(item["id"]) == str(pid)]
            else:
                name = str(query).strip().casefold()
                if not name: raise MusicNotFound("需要明确歌单 ID 或唯一歌单名")
                matches = [item for item in bindings if name in item["name"].casefold()]
            if kwargs.get("subject") is not None:
                matches = [item for item in matches if item["subject"] == subject]
            if len(matches) != 1:
                raise MusicNotFound("只可查看已绑定且唯一匹配的歌单；请先用 playlists 查询 ID")
            binding = matches[0]
            playlist = await service.provider.playlist(binding["id"])
            songs = playlist.get("songs") or []
            try:
                offset = int(kwargs.get("offset") or 0)
                limit = int(kwargs.get("limit") or 30)
            except (TypeError, ValueError):
                raise MusicNotFound("歌单分页参数无效") from None
            if offset < 0 or limit < 1 or limit > 30:
                raise MusicNotFound("歌单分页参数无效；每次最多 30 首")
            page = songs[offset:offset + limit]
            return ToolResult(ToolStatus.SUCCESS, json.dumps({
                "id": binding["id"], "name": playlist.get("name") or binding["name"],
                "subject": binding["subject"], "total": len(songs), "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(songs) else None,
                "songs": [{"id": item.get("id"), "name": item.get("name"),
                           "artist": item.get("artist")} for item in page],
            }, ensure_ascii=False))
        if action == "playlist_create":
            from .library import create
            p = await create(query,subject,privacy=True,source="k_tool",reason=reason)
            return ToolResult(ToolStatus.SUCCESS,"已在网易云创建私密歌单：" + p["name"] + "；ID " + p["id"])
        if action == "playlist_rename":
            if not pid: raise MusicNotFound("需要明确目标歌单 ID")
            from .library import rename
            p = await rename(pid, query,source="k_tool",reason=reason)
            return ToolResult(ToolStatus.SUCCESS,"网易云重新读取已确认歌单改名为：" + p["name"])
        if action == "playlist_delete":
            if not pid: raise MusicNotFound("需要明确目标歌单 ID")
            from .library import delete
            p = await delete(pid, source="k_tool",reason=reason)
            return ToolResult(ToolStatus.SUCCESS,"网易云已确认删除 K 的歌单：" + p["name"])
        if action in {"playlist_add","playlist_remove","like"}:
            if not pid:
                candidates = [p for p in service.store.bindings() if p["subject"] == subject]
                if len(candidates) != 1: raise MusicNotFound("需要明确目标歌单 ID")
                pid = candidates[0]["id"]
            if not sid:
                if not query.strip(): raise MusicNotFound("需要明确歌曲 ID 或歌名")
                if action == "playlist_remove":
                    source_songs = (await service.provider.playlist(pid)).get("songs") or []
                else:
                    source_songs = await service.search_songs(query)
                compact = lambda value: "".join(
                    char for char in str(value or "").casefold() if char.isalnum()
                )
                query_key = compact(query)
                source_songs = [
                    item for item in source_songs
                    if compact(item.get("name"))
                    and (compact(item.get("name")) in query_key or query_key == compact(item.get("name")))
                ]
                from behavior_scheduler.cloud_music_tool import CloudMusicTool
                matched = CloudMusicTool._select_best_song(query, source_songs)
                if not matched: raise MusicNotFound("没有找到可确认的匹配歌曲")
                sid = matched["id"]
            from .library import change_tracks
            p = await change_tracks(
                pid, [sid], "remove" if action == "playlist_remove" else "add",
                source="k_tool", reason=reason,
            )
            if not p.get("change_applied"):
                state = "歌曲原本就在歌单里" if action == "playlist_add" else "歌曲原本不在歌单里"
                return ToolResult(ToolStatus.SUCCESS, "网易云重新读取已确认无需修改：" + state + "；" + p["name"])
            return ToolResult(ToolStatus.SUCCESS,"网易云重新读取已确认歌单修改：" + p["name"])
        if action == "playlist_unbind":
            if not pid: raise MusicNotFound("需要明确目标歌单 ID")
            from .library import unbind
            binding = unbind(pid)
            return ToolResult(
                ToolStatus.SUCCESS,
                "已将歌单移出 MIRROW；网易云原歌单未删除：" + binding["name"],
            )
        if action == "playlist_wander_default":
            if not pid: raise MusicNotFound("需要明确目标歌单 ID")
            from .library import set_wander_default
            binding = set_wander_default(pid, bool(kwargs.get("selected", True)))
            return ToolResult(ToolStatus.SUCCESS,"漫想默认收藏处已更新：" + binding["name"])
        if action == "playlist_play":
            if not pid: raise MusicNotFound("需要明确歌单 ID")
            p = await service.provider.playlist(pid)
            if not p["songs"]: raise MusicNotFound("歌单为空")
            state = await service.start_song(
                p["songs"][0], device, kwargs.get("mode") or "loop", p["songs"],
                playlist_id=pid,
            )
            return ToolResult(ToolStatus.SUCCESS,"歌单播放状态：" + state["status"])
        if action == "playlist_share":
            if not pid and query and ("http://" in query or "https://" in query):
                resolved = await service.provider.resolve(query)
                if resolved["kind"] != "playlist": raise MusicNotFound("这不是网易云歌单链接")
                playlist = resolved["playlist"]
            else:
                if not pid:
                    matches = [p for p in service.store.bindings() if query.strip() and query.strip() in p["name"]]
                    if len(matches) != 1: raise MusicNotFound("需要明确歌单 ID 或唯一歌单名")
                    pid = matches[0]["id"]
                playlist = await service.provider.playlist(pid)
            songs = playlist.get("songs") or []
            preview = "；".join(
                f"{item.get('name') or '未命名'}—{item.get('artist') or '未知'}"
                for item in songs[:8]
            )
            card = {
                **playlist, "kind": "playlist", "playlist_id": str(playlist["id"]),
                "track_count": len(songs) or playlist.get("track_count") or 0,
                "track_preview": preview, "play_mode": kwargs.get("mode") or "loop",
            }
            return ToolResult(
                ToolStatus.SUCCESS,
                "已生成歌单卡片：《" + playlist["name"] + "》；分享并未开始播放",
                extra_data={"music_card": card},
            )
        if action == "music_login":
            return ToolResult(ToolStatus.NEED_USER_INPUT,"请在 MIRROW 音乐中枢连接网易云账号，二维码只在本地界面展示",need_user_action=True)
        if action in {"toggle_music_mode","prev","daily_recommend"}:
            raise MusicNotFound("旧音乐模式和不受控原生队列已退役；请使用明确歌曲或歌单")
        if action in {"share","recommend_by_style","search_play","inspect","note"}:
            if sid:
                song = service.store.shared_song(sid) if action == "share" else None
                if not song:
                    song = await service.provider.song(sid)
            elif query:
                if "http://" in query or "https://" in query:
                    resolved = await service.provider.resolve(query)
                    if resolved["kind"] != "song": raise MusicNotFound("这是歌单链接")
                    song = resolved["song"]
                else:
                    songs = await service.search_songs(query)
                    if not songs: raise MusicNotFound("未找到匹配歌曲")
                    from behavior_scheduler.cloud_music_tool import CloudMusicTool
                    song = CloudMusicTool._select_best_song(query,songs)
            else: raise MusicNotFound("需要具体歌名、歌曲 ID 或链接")
            if action == "search_play":
                mode = str(kwargs.get("mode") or "single")
                if mode not in {"single", "one"}:
                    raise MusicNotFound("单曲点播只支持播完停止或单曲循环")
                state = await service.start_song(song,device,mode)
                return ToolResult(ToolStatus.SUCCESS,"设备回执：" + state["status"],extra_data={"music_card":song})
            if action in {"inspect","note"}:
                material = await prepare(song)
                if action == "inspect":
                    return ToolResult(ToolStatus.SUCCESS,"歌曲文本材料；不是收听记录：\n" + json.dumps(material,ensure_ascii=False),extra_data={"music_card":song})
                reaction = str(kwargs.get("reaction") or "").strip()
                if not reaction or not material.get("lyrics"): raise MusicNotFound("需要实际歌曲材料与 K 的明确感想")
                from cognition.music import record
                from .store import now_iso
                record({"subject_id":"k","mode":"analysis","title":song["name"],"artist":song["artist"],"reaction":reaction,"dimensions":{}},
                       source_id="music-analysis:" + song["id"] + ":" + now_iso()[:10],source="K 阅读网易云歌词材料后的自主感想")
                return ToolResult(ToolStatus.SUCCESS,"K 的歌曲材料感想已保存；不冒充音频收听经历")
            return ToolResult(ToolStatus.SUCCESS,"已生成歌曲卡片：《" + song["name"] + "》— " + song["artist"] + "；分享并未开始播放",extra_data={"music_card":song})
        raise MusicNotFound("不支持的音乐操作")
    except MusicSystemError as error:
        return ToolResult(ToolStatus.ERROR,"",error=str(error))
    except Exception:
        return ToolResult(ToolStatus.ERROR,"",error="音乐操作未完成，未记录为成功")
