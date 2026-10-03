"""
网易云音乐 API 封装：搜索 + 歌曲信息 + 音频 URL
直接复用自 AionsHome 开源项目 (music.py)，适配 MIRROW 的 settings_manager 和 logger。
上游 death34018-hue/AionsHome，Copyright (c) 2026 death34018-hue，MIT。
完整许可随包见 LICENSES/AionsHome-MIT.txt。

优先使用 pyncm（支持 VIP + 高音质），pyncm 不可用时回退到 httpx 公开 API。
支持 MUSIC_U Cookie 登录（VIP 可播放付费歌曲），未配置时退回匿名登录。
会话每 2 小时自动刷新，获取音频失败时自动重试一次。
"""

import logging
import threading
import time

import httpx
from settings_manager import get_setting

log = logging.getLogger(__name__)

# ── pyncm 优先 ──
_PYNCM_AVAILABLE = False
try:
    from pyncm.apis.login import LoginViaAnonymousAccount, LoginViaCookie
    from pyncm.apis.cloudsearch import GetSearchResult
    from pyncm.apis.track import GetTrackDetail, GetTrackAudio
    _PYNCM_AVAILABLE = True
    log.info("netease_api: pyncm 已加载 (VIP+高音质)")
except ImportError:
    log.info("netease_api: pyncm 不可用，回退到 httpx 公开 API (仅免费音质)")

# ── httpx 公开 API 回退 ──
SEARCH_URL = "http://music.163.com/api/search/get/web"
DETAIL_URL = "https://music.163.com/api/song/detail"
OUTER_URL = "https://music.163.com/song/media/outer/url"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

_init_lock = threading.Lock()
_inited = False
_last_login_time = 0.0
_SESSION_TTL = 2 * 3600  # 会话有效期：2小时


def is_available() -> bool:
    """API 是否可用（始终可用，pyncm 不可用时有 httpx 回退）"""
    return True


def is_vip_available() -> bool:
    """VIP/高音质是否可用（仅 pyncm 支持）"""
    return _PYNCM_AVAILABLE


# ── pyncm 登录（仅 pyncm 需要）──

def _ensure_login():
    """确保已登录且会话未过期（优先 MUSIC_U Cookie，否则匿名）"""
    global _inited, _last_login_time
    if not _PYNCM_AVAILABLE:
        return  # httpx 回退不需要登录

    now = time.time()
    if _inited and (now - _last_login_time < _SESSION_TTL):
        return
    with _init_lock:
        now = time.time()
        if _inited and (now - _last_login_time < _SESSION_TTL):
            return
        try:
            music_u = (get_setting("netease_music_u") or "").strip()
            if music_u:
                LoginViaCookie(MUSIC_U=music_u)
                _inited = True
                _last_login_time = now
                log.info("pyncm MUSIC_U Cookie 登录成功（VIP）")
            else:
                LoginViaAnonymousAccount()
                _inited = True
                _last_login_time = now
                log.info("pyncm 匿名登录成功（未配置 MUSIC_U）")
        except Exception as e:
            log.error("pyncm 登录失败: %s", e)
            raise


def _force_relogin():
    """强制重新登录（会话可能已失效）"""
    global _inited, _last_login_time
    with _init_lock:
        _inited = False
        _last_login_time = 0
    _ensure_login()


def reload_login():
    """重新登录（settings 更新 MUSIC_U 后调用）"""
    if _PYNCM_AVAILABLE:
        _force_relogin()


# ── Cookie 辅助 ──

def _get_cookies() -> dict | None:
    """获取 MUSIC_U Cookie（供 httpx 回退使用）"""
    music_u = (get_setting("netease_music_u") or "").strip()
    if music_u:
        return {"MUSIC_U": music_u}
    return None


# ── 搜索 ──

def _search_via_pyncm(keyword: str, limit: int) -> list[dict]:
    """pyncm 搜索"""
    _ensure_login()
    resp = GetSearchResult(keyword, limit=limit)
    songs = resp.get("result", {}).get("songs", [])
    results = []
    for s in songs:
        artists = [a["name"] for a in s.get("ar", [])]
        album_info = s.get("al", {})
        results.append({
            "id": s["id"],
            "name": s["name"],
            "artists": artists,
            "artist": " / ".join(artists),
            "album": album_info.get("name", ""),
            "cover": (album_info.get("picUrl") or "") + "?param=200y200",
            "duration": s.get("dt", 0),
        })
    return results


def _search_via_httpx(keyword: str, limit: int) -> list[dict]:
    """httpx 公开 API 搜索（兜底）"""
    try:
        resp = httpx.get(
            SEARCH_URL,
            params={"s": keyword, "type": 1, "limit": limit},
            headers=HEADERS,
            cookies=_get_cookies(),
            timeout=10.0,
        )
        data = resp.json()
    except Exception as e:
        log.warning(f"httpx 搜索失败: {e}")
        return []

    songs = data.get("result", {}).get("songs", [])
    results = []
    for s in songs:
        artists = [a["name"] for a in s.get("artists", s.get("ar", []))]
        album_info = s.get("album", s.get("al", {}))
        results.append({
            "id": s["id"],
            "name": s["name"],
            "artists": artists,
            "artist": " / ".join(artists) if artists else "未知",
            "album": album_info.get("name", ""),
            "cover": (album_info.get("picUrl") or album_info.get("img1v1Url") or "") + "?param=200y200",
            "duration": s.get("duration", s.get("dt", 0)),
        })
    return results


def search_songs(keyword: str, limit: int = 5) -> list[dict]:
    """搜索歌曲，返回精简结果列表"""
    if _PYNCM_AVAILABLE:
        try:
            return _search_via_pyncm(keyword, limit)
        except Exception as e:
            log.warning(f"pyncm 搜索失败，回退 httpx: {e}")
    return _search_via_httpx(keyword, limit)


# ── 歌曲详情 ──

def _detail_via_pyncm(song_id: int) -> dict | None:
    """pyncm 获取歌曲详情"""
    _ensure_login()
    resp = GetTrackDetail([song_id])
    songs = resp.get("songs", [])
    if not songs:
        return None
    s = songs[0]
    artists = [a["name"] for a in s.get("ar", [])]
    album_info = s.get("al", {})
    return {
        "id": s["id"],
        "name": s["name"],
        "artists": artists,
        "artist": " / ".join(artists),
        "album": album_info.get("name", ""),
        "cover": (album_info.get("picUrl") or "") + "?param=200y200",
        "duration": s.get("dt", 0),
    }


def _detail_via_httpx(song_id: int) -> dict | None:
    """httpx 公开 API 获取歌曲详情（兜底）"""
    try:
        resp = httpx.get(
            DETAIL_URL,
            params={"ids": f"[{song_id}]"},
            headers=HEADERS,
            cookies=_get_cookies(),
            timeout=10.0,
        )
        data = resp.json()
    except Exception as e:
        log.warning(f"httpx 获取歌曲详情失败: {e}")
        return None

    songs = data.get("songs", [])
    if not songs:
        return None
    s = songs[0]
    artists = [a["name"] for a in s.get("ar", [])]
    album_info = s.get("al", {})
    return {
        "id": s["id"],
        "name": s["name"],
        "artists": artists,
        "artist": " / ".join(artists),
        "album": album_info.get("name", ""),
        "cover": (album_info.get("picUrl") or "") + "?param=200y200",
        "duration": s.get("dt", 0),
    }


def get_song_detail(song_id: int) -> dict | None:
    """获取单曲详情"""
    if _PYNCM_AVAILABLE:
        try:
            result = _detail_via_pyncm(song_id)
            if result:
                return result
        except Exception as e:
            log.warning(f"pyncm 详情失败，回退 httpx: {e}")
    return _detail_via_httpx(song_id)


# ── 音频 URL ──

def _audio_via_pyncm(song_id: int, retry: bool = True) -> str | None:
    """pyncm GetTrackAudio 获取播放 URL（VIP 歌曲可获取高音质）"""
    _ensure_login()
    resp = GetTrackAudio([song_id])
    for d in resp.get("data", []):
        url = d.get("url")
        if url:
            return url
    # 可能会话过期，强制重新登录后重试
    if retry:
        log.info("get_audio_url(%s) pyncm 返回空，尝试重新登录重试", song_id)
        _force_relogin()
        return _audio_via_pyncm(song_id, retry=False)
    return None


def _audio_via_httpx(song_id: int) -> str | None:
    """httpx 公开 outer/url 接口获取 CDN 重定向目标（仅免费音质）"""
    try:
        resp = httpx.head(
            f"{OUTER_URL}?id={song_id}.mp3",
            headers={**HEADERS, "Accept": "*/*"},
            cookies=_get_cookies(),
            follow_redirects=False,
            timeout=10.0,
        )
        location = resp.headers.get("Location", "")
        if location and "music.163.com/404" not in location:
            return location
        if resp.status_code == 200:
            return f"{OUTER_URL}?id={song_id}.mp3"
        return None
    except Exception as e:
        log.warning(f"httpx 获取音频 URL 失败 (song_id={song_id}): {e}")
        return None


def get_audio_url(song_id: int) -> str | None:
    """尝试获取播放 URL。pyncm 优先（VIP+高音质），httpx 兜底（免费音质）。"""
    if _PYNCM_AVAILABLE:
        try:
            url = _audio_via_pyncm(song_id)
            if url:
                return url
        except Exception as e:
            log.warning(f"pyncm 获取音频 URL 失败，回退 httpx: {e}")
    return _audio_via_httpx(song_id)
