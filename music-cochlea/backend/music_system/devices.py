"""Real native-device adapters. Metadata and media-session receipts are the authority."""
import asyncio
import base64
import json
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from .errors import PlaybackUnavailable

_pc_guard = None
_pc_end = None
_pc_owned_song = None

async def _mobile(action, song=None):
    from shared_state import get_mobile_relay_callback
    relay = get_mobile_relay_callback()
    if not relay:
        raise PlaybackUnavailable("手机后台未连接，请检查 MIRROW Relay")
    params = {"action": action}
    if song:
        params.update(song_id=song["id"], title=song["name"], artist=song["artist"],
                      title_aliases=song.get("title_aliases", []),
                      duration_ms=song["duration"], guard_token=uuid.uuid4().hex)
    try:
        result = await asyncio.wait_for(
            relay("music_" + uuid.uuid4().hex, "mobile_music_control", params), 12
        )
    except asyncio.TimeoutError as error:
        raise PlaybackUnavailable("手机 Relay 已连接，但网易云操作回执超时") from error
    if not result or not result.get("success"):
        detail = str((result or {}).get("error") or (result or {}).get("content") or "").strip()
        raise PlaybackUnavailable(detail or "手机网易云没有确认操作，请检查通知访问、后台连接与歌曲播放权限")
    data = result.get("data") or {}
    return data.get("observed") or data

async def _pc_session():
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    except ImportError:
        raise PlaybackUnavailable("电脑缺少 Windows 媒体会话组件，尚不能保证曲终停止")
    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    for session in manager.get_sessions():
        source = str(session.source_app_user_model_id).lower()
        if "cloudmusic" in source or "netease" in source:
            return session
    return None

async def _pc_read(session=None):
    session = session or await _pc_session()
    if not session:
        return {"available": False}
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status
    metadata = await session.try_get_media_properties_async()
    state = session.get_playback_info()
    timeline = session.get_timeline_properties()
    available = state.playback_status in {Status.PLAYING, Status.PAUSED, Status.STOPPED}
    data = {"available": available, "playing": state.playback_status == Status.PLAYING,
            "title": metadata.title, "artist": metadata.artist,
            "position_ms": int(timeline.position.total_seconds()*1000),
            "duration_ms": int(timeline.end_time.total_seconds()*1000)}
    if _pc_end and _pc_end["title"] == metadata.title:
        data.update(end_of_track=True, end_token=_pc_end["token"])
    return data

def _orpheus(payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    webbrowser.open("orpheus://" + encoded)

async def _pc_fence(song, token):
    global _pc_end
    try:
        while True:
            session = await _pc_session()
            if session:
                data = await _pc_read(session)
                from .service import MusicService
                if data.get("title") and not MusicService._matches(song, data["title"], str(data.get("artist") or "")):
                    return  # A manual song selection belongs to 使用者.
                if data.get("playing"):
                    timeline = session.get_timeline_properties()
                    position = data["position_ms"]
                    updated = timeline.last_updated_time
                    if isinstance(updated, datetime):
                        position += max(0, int((datetime.now(timezone.utc) - updated).total_seconds()*1000))
                    if data["duration_ms"] > 0 and position >= data["duration_ms"] - 200:
                        if await session.try_pause_async():
                            for _ in range(12):
                                confirmed = await _pc_read(session)
                                if confirmed.get("available") and confirmed.get("playing") is False:
                                    _pc_end = {"title": confirmed["title"], "token": token}
                                    return
                                await asyncio.sleep(.1)
            await asyncio.sleep(.1)
    except asyncio.CancelledError:
        raise
    except Exception:
        # No end receipt from a failed fence. Observation will become unknown.
        return False

async def adapter(action, device, song=None):
    global _pc_guard, _pc_end, _pc_owned_song
    if device == "mobile":
        if action == "play":
            capability = await _mobile("capabilities")
            if not capability.get("single_stop_supported"):
                raise PlaybackUnavailable("手机需要安装新版 MIRROW 才能可靠地单曲停止")
        if action == "resume" and song:
            await _mobile("arm_guard", song)
        data = await _mobile({"play":"play_song","resume":"play","observe":"now_playing"}.get(action,action), song if action in {"play","pause","resume"} else None)
        if action in {"pause","resume"} and not data.get("available"):
            raise PlaybackUnavailable("手机媒体会话状态待确认")
        if action == "play" and not data.get("available") and not data.get("dispatched"):
            raise PlaybackUnavailable("手机点播没有取得送达回执")
        return data
    if device != "computer" or sys.platform != "win32":
        raise PlaybackUnavailable("当前电脑没有 Windows 网易云播放适配")
    session = await _pc_session()  # Verify component before launching audio.
    if action == "observe":
        data = await _pc_read(session)
        if _pc_guard and _pc_guard.done() and not _pc_guard.cancelled() and _pc_guard.result() is False and data.get("playing"):
            data["available"] = False
        return data
    if action == "play":
        if _pc_guard:
            _pc_guard.cancel()
            await asyncio.gather(_pc_guard, return_exceptions=True)
        _pc_end = None
        _orpheus({"type":"song","id":song["id"],"cmd":"play"})
        for _ in range(24):
            await asyncio.sleep(.25)
            data = await _pc_read()
            from .service import MusicService
            if data.get("playing") and MusicService._matches(song, data.get("title", ""), str(data.get("artist") or "")):
                _pc_guard = asyncio.create_task(_pc_fence(song, uuid.uuid4().hex))
                _pc_owned_song = song
                return data
        _orpheus({"cmd":"pause"})
        raise PlaybackUnavailable("电脑网易云未暴露可确认的播放状态；已发送暂停，请检查 App")
    if not session:
        raise PlaybackUnavailable("未找到电脑网易云的活动媒体会话")
    if song:
        from .service import MusicService
        observed = await _pc_read(session)
        if not MusicService._matches(song, observed.get("title", ""), str(observed.get("artist") or "")):
            raise PlaybackUnavailable("网易云已换歌，未控制手动接管后的歌曲")
    method = session.try_pause_async if action == "pause" else session.try_play_async
    if not await method():
        raise PlaybackUnavailable("电脑网易云未接受播放控制")
    for _ in range(12):
        data = await _pc_read(session)
        if data.get("available") and data.get("playing") is (action == "resume"):
            if action == "resume" and song:
                if _pc_guard: _pc_guard.cancel()
                _pc_end = None
                _pc_guard = asyncio.create_task(_pc_fence(song, uuid.uuid4().hex))
                _pc_owned_song = song
            return data
        await asyncio.sleep(.1)
    raise PlaybackUnavailable("电脑网易云未确认控制后的状态")

async def stop():
    global _pc_guard
    if _pc_guard:
        was_owned = not _pc_guard.done()
        _pc_guard.cancel()
        await asyncio.gather(_pc_guard, return_exceptions=True)
        _pc_guard = None
        if was_owned and _pc_owned_song:
            session = await _pc_session()
            if session:
                from .service import MusicService
                observed = await _pc_read(session)
                if MusicService._matches(_pc_owned_song, observed.get("title", ""), str(observed.get("artist") or "")):
                    await session.try_pause_async()
