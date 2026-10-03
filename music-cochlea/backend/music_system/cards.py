"""Pure message attachment projection. Sharing a song is not a playback fact."""
import re
from urllib.parse import urlparse

def normalize_music_fields(value):
    music_type = "playlist" if value.get("music_type") == "playlist" or value.get("playlist_id") else "song"
    sid = str(value.get("playlist_id") if music_type == "playlist" else value.get("song_id") or "")
    if not re.fullmatch(r"[0-9]{1,20}", sid):
        raise ValueError("音乐卡片缺少有效的网易云 ID")
    artist = str(value.get("artist") or "")
    if len(artist) > 500 or len(str(value.get("body") or "")) > 2000:
        raise ValueError("歌曲卡片文字过长")
    cover = str(value.get("cover") or "")
    parsed = urlparse(cover)
    if cover and (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".music.126.net") or parsed.username or parsed.port not in {None,443}):
        cover = ""
    duration = str(value.get("duration") or "0")
    if not duration.isdigit() or int(duration) > 86400000:
        duration = "0"
    share_number = str(value.get("share_number") or "0")
    share_count = str(value.get("share_count") or "0")
    if not share_number.isdigit(): share_number = "0"
    if not share_count.isdigit(): share_count = "0"
    lyrics_excerpt = str(value.get("lyrics_excerpt") or "")[:1200]
    melody_summary = str(value.get("melody_summary") or "")[:500]
    common = {"kind": "music", "music_type": music_type, "artist": artist, "cover": cover,
              "duration": duration, "share_number": share_number, "share_count": share_count}
    if music_type == "playlist":
        count = str(value.get("track_count") or "0")
        if not count.isdigit(): count = "0"
        mode = str(value.get("play_mode") or "loop")
        if mode not in {"list", "loop", "one"}: mode = "loop"
        return {**common, "playlist_id": sid, "track_count": count,
                "track_preview": str(value.get("track_preview") or "")[:1500],
                "play_mode": mode, "link": f"https://music.163.com/playlist?id={sid}"}
    return {**common, "song_id": sid, "link": f"https://music.163.com/song?id={sid}",
            "lyrics_available": "1" if value.get("lyrics_available") in {True, "1", 1} or lyrics_excerpt else "0",
            "lyrics_excerpt": lyrics_excerpt, "melody_summary": melody_summary,
            "material_status": str(value.get("material_status") or ("ready" if lyrics_excerpt and melody_summary else "partial" if lyrics_excerpt or melody_summary else "missing"))}

def card_context(card):
    if card.get("music_type") == "playlist" or card.get("playlist_id"):
        lines = [
            "【使用者分享的网易云歌单卡片；这是分享资料，不是设备播放记录】",
            f"歌单：{card.get('title') or '未填写歌单名'}；歌曲数：{card.get('track_count') or '未知'}",
            f"网易云歌单 ID：{card.get('playlist_id')}",
            f"建议播放方式：{'顺序播放' if card.get('play_mode') == 'list' else '单曲循环' if card.get('play_mode') == 'one' else '列表循环'}",
        ]
        if card.get("track_preview"):
            lines.append(f"曲目预览：{card['track_preview']}")
        lines.append(f"卡片附言：{card.get('body') or '无'}")
        return "\n".join(lines)
    occurrence = int(card.get("share_number") or 0)
    lines = [
        "【使用者分享的歌曲卡片；这是分享资料，不是设备播放记录】",
        f"歌曲：{card.get('title') or '未填写歌名'}；歌手：{card.get('artist') or '未填写'}",
        f"网易云歌曲 ID：{card.get('song_id')}",
    ]
    if occurrence:
        lines.append(f"这是这首歌在 MIRROW 中第 {occurrence} 次被分享。")
    lyrics = str(card.get("lyrics_excerpt") or "")
    melody = str(card.get("melody_summary") or "")
    if not lyrics or not melody:
        try:
            from .service import get_service
            from .materials import lyric_excerpt
            material = get_service().store.material(str(card.get("song_id") or "")) or {}
            lyrics = lyrics or lyric_excerpt(material.get("lyrics") or material.get("lyrics_excerpt") or "")
            melody = melody or str(material.get("melody_summary") or "")[:500]
        except Exception:
            pass
    lines.append(f"歌词材料：{lyrics or '当前没有取得歌词材料'}")
    lines.append(f"旋律分析：{melody or '尚未完成客观旋律分析'}")
    lines.append(f"卡片附言：{card.get('body') or '无'}")
    return "\n".join(lines)
