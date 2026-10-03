"""Objective current music facts. No delivery strategy or invented sensations."""
import re

from .service import get_service

LYRICS_CONTEXT_LIMIT = 2400
MELODY_CONTEXT_LIMIT = 500


def lyrics_excerpt(lyrics: str, position_ms: int | None) -> str:
    """Bound lyrics context, preferring a small window around confirmed progress."""
    text = str(lyrics or "").strip()
    if len(text) <= LYRICS_CONTEXT_LIMIT:
        return text
    timed = []
    for line in text.splitlines():
        match = re.match(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", line.strip())
        if not match:
            continue
        fraction = (match.group(3) or "0")[:3].ljust(3, "0")
        stamp = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 + int(fraction)
        timed.append((stamp, line))
    if position_ms is not None and timed:
        centre = max((index for index, item in enumerate(timed) if item[0] <= int(position_ms)), default=0)
        excerpt = "\n".join(line for _, line in timed[max(0, centre - 5):centre + 9])
        if excerpt:
            return excerpt[:LYRICS_CONTEXT_LIMIT]
    return text[:LYRICS_CONTEXT_LIMIT].rstrip() + "\n[后续歌词未注入本轮上下文]"

def quiet_active():
    session = get_service().status().get("session")
    return bool(session and session.get("quiet") and session.get("status") not in {"ended", "external", "failed", "superseded"})

def build_context():
    service = get_service()
    session = service.status().get("session")
    if not session: return ""
    song = session["song"]
    state = {"playing":"设备确认播放中","paused":"设备确认暂停","starting":"点播等待确认",
             "waiting_next":"上一首已停，等待使用者在网易云选下一首","unknown":"设备状态未知"}.get(session["status"], session["status"])
    position = session.get("position_ms")
    lines = ["【MIRROW 音乐会话观察】",
             f"设备：{session['device']}；状态：{state}；最近回执：{session['observed_at']}。",
             f"歌曲：《{song['name']}》— {song['artist']}；网易云 ID：{song.get('id') or '未核验'}。",
             f"回执进度：{position} 毫秒。" if position is not None else "回执未提供进度。",
             f"本次安静陪听偏好：{'开启' if session['quiet'] else '关闭'}；播放方式：{session['mode']}；持续一起听：{'开启' if session.get('follow_external') else '关闭'}。",
             ("这首歌由使用者在网易云手动选择，MIRROW 依据设备播放状态跟随；曲目 ID 尚未核验。"
              if song.get("origin") == "manual_in_shared_session" else
              "当前曲目由 MIRROW 发起；歌词等歌曲材料是文本资料，不是麦克风听觉转录。")]
    material = service.store.material(song["id"]) if song.get("id") else None
    if material:
        excerpt = lyrics_excerpt(material.get("lyrics", ""), position)
        if excerpt:
            lines.append("歌曲资料节选（来源：网易云歌词接口；全文保存在本地材料库）：\n" + excerpt)
        melody = str(material.get("melody_summary") or "").strip()[:MELODY_CONTEXT_LIMIT]
        if melody:
            lines.append("已有旋律分析材料（来源：本地歌曲材料缓存；不是实时听觉回执）：" + melody)
    return "\n".join(lines)
