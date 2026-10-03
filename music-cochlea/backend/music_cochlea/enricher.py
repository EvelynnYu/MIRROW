# Adapted from sebastianevan200-stack/eryu; CC BY-NC-SA 4.0.
# MIRROW modifications: song metadata, cache and host-memory integration.
# See THIRD_PARTY_NOTICES.md and LICENSES/eryu-CC-BY-NC-SA-4.0.txt.
"""
歌曲信息丰富器 — pyncm 歌词 + Memory V2 记忆检索
"""

import asyncio
import re
import sys
import logging
from typing import Optional, List

import httpx

from .models import SongIdentity, SongMetadata

logger = logging.getLogger(__name__)

NETEASE_LYRIC_URL = "http://music.163.com/api/song/lyric"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
LYRICS_SNIPPET_MAX = 0     # 0=不截断，全量歌词
MEMORY_TOP_K = 3            # 记忆检索条数
MEMORY_CAP = 200            # 每条记忆截断字符数
MELODY_TIMEOUT = 300        # 旋律分析超时（秒）


async def enrich_song(identity: SongIdentity) -> SongMetadata:
    """
    丰富歌曲信息：
    1. 网易云搜索 → 获取 song_id + 歌词
    2. Memory V2 记忆检索 → 搜相关记忆
    """
    meta = SongMetadata(identity=identity)

    # 1. 网易云搜索
    try:
        from netease_api import search_songs
        results = search_songs(f"{identity.title} {identity.artist}", limit=3)
        if results:
            # 找最佳匹配（歌名 + 歌手都匹配的优先）
            best = _find_best_match(results, identity)
            if best:
                meta.netease_song_id = best.get("id")
                # 获取歌词
                lyrics = await _fetch_lyrics(meta.netease_song_id)
                meta.lyrics = lyrics
                meta.lyrics_snippet = lyrics if (not LYRICS_SNIPPET_MAX or len(lyrics) <= LYRICS_SNIPPET_MAX) else lyrics[:LYRICS_SNIPPET_MAX]
    except Exception as e:
        logger.warning(f"网易云搜索/歌词失败: {e}")

    # 1.5 网易云风格标签
    if meta.netease_song_id:
        try:
            tags = await _fetch_song_tags(meta.netease_song_id)
            if tags:
                meta.tags = tags
        except Exception:
            pass

    # 2. Memory V2 记忆检索（扩增：歌名+歌手+风格词+情感词）
    try:
        extra_terms = getattr(meta, 'tags', []) or []
        memory_snippets = await _search_memories(identity, extra_terms=extra_terms)
        meta.memory_snippets = memory_snippets
    except Exception as e:
        logger.warning(f"记忆检索失败: {e}")

    return meta


def _find_best_match(results: list, identity: SongIdentity) -> Optional[dict]:
    """在搜索结果中找最佳匹配"""
    title_lower = identity.title.lower().strip()
    artist_lower = identity.artist.lower().strip()

    # 优先：歌名 + 歌手都匹配
    for r in results:
        r_title = r.get("name", "").lower().strip()
        r_artist = r.get("artist", "").lower().strip()
        if title_lower in r_title and artist_lower in r_artist:
            return r

    # 其次：歌名匹配
    for r in results:
        r_title = r.get("name", "").lower().strip()
        if title_lower in r_title:
            return r

    # 兜底：返回第一个
    return results[0] if results else None


async def _fetch_lyrics(song_id: int) -> str:
    """从网易云 API 获取歌词，去时间轴返回纯文本"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                NETEASE_LYRIC_URL,
                params={"id": song_id, "lv": 1},
                headers=HEADERS,
            )
        data = resp.json()
        if data.get("code") != 200:
            return ""

        lrc = data.get("lrc", {}).get("lyric", "")
        tlyric = data.get("tlyric", {}).get("lyric", "")  # 翻译歌词

        lines = []
        for raw in [lrc, tlyric]:
            for line in raw.split("\n"):
                cleaned = re.sub(r"\[.*?\]", "", line).strip()
                if cleaned and cleaned not in lines:
                    lines.append(cleaned)
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"歌词获取失败 (song_id={song_id}): {e}")
        return ""


async def _fetch_song_tags(song_id: int) -> List[str]:
    """从网易云获取歌曲风格标签"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"http://music.163.com/api/song/detail",
                params={"id": song_id, "ids": f"[{song_id}]"},
                headers=HEADERS,
            )
        data = resp.json()
        songs = data.get("songs", [])
        if songs:
            song = songs[0]
            tags = song.get("tags", []) or []
            if isinstance(tags, list):
                return [t for t in tags if isinstance(t, str)][:5]
            # 有时 tags 是逗号分隔字符串
            if isinstance(tags, str) and tags:
                return [t.strip() for t in tags.split(",") if t.strip()][:5]
    except Exception:
        pass
    return []


async def _search_memories(identity: SongIdentity, extra_terms: List[str] = None) -> List[str]:
    """Retrieve attributed related experiences through the common V2 reader."""
    try:
        from memory_v2.retrieval import search_records
        terms = [identity.title, identity.artist, *(extra_terms or [])[:3]]
        records = await search_records(" ".join(terms), limit=MEMORY_TOP_K)
        return [item["content"][:MEMORY_CAP] for item in records if item["content"]]
    except Exception as exc:
        logger.debug("记忆检索不可用: %s", type(exc).__name__)
        return []


async def analyze_melody(identity: SongIdentity, flash_llm_func=None, timeout=MELODY_TIMEOUT, song_id: int = None) -> dict:
    """旋律分析 v2：下载音频 → librosa 频谱分析（BPM/调性/能量）→ Flash 总结。

    参考 eryu (CC BY-NC-SA 4.0) 的 analyze_song.py，用 librosa 替代 basic-pitch/TensorFlow。
    song_id 由调用方传入（复用 enricher 已查到的 ID），避免重复搜索。
    """
    if not song_id:
        return {}

    # 1. 下载音频
    try:
        from netease_api import get_audio_url
        url = get_audio_url(song_id)
        if not url:
            logger.warning(f"get_audio_url 返回空: song_id={song_id}")
            return {}
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            audio_bytes = resp.content
        if not audio_bytes or len(audio_bytes) < 10000:
            logger.warning(f"音频下载过短或为空: {len(audio_bytes) if audio_bytes else 0} bytes")
            return {}
    except Exception as e:
        logger.warning(f"音频下载失败: {e}")
        return {}

    # 2. 写音频到 cache_dir/{song_id}.mp3 → 调用 analyze_song.py（librosa 子进程）
    import tempfile, os, subprocess, json as _json
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "melody_cache")
    os.makedirs(cache_dir, exist_ok=True)

    audio_path = os.path.join(cache_dir, f"{song_id}.mp3")
    try:
        # 检查缓存：如果已分析过，直接读结果
        cached_result = os.path.join(cache_dir, f"{song_id}_preanalysis.json")
        if os.path.exists(cached_result):
            with open(cached_result, "r") as f:
                data = _json.load(f)
            logger.info(f"旋律分析缓存命中: {identity.display_name}")
            return await _extract_melody_data(data, identity, flash_llm_func)

        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyze_song.py")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, script, str(song_id), identity.title, identity.artist, cache_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout)

        if os.path.exists(cached_result):
            with open(cached_result, "r") as f:
                data = _json.load(f)
        else:
            data = {}
    except asyncio.TimeoutError:
        logger.warning(f"旋律分析超时: {identity.display_name}")
        data = {}
    except Exception as e:
        logger.warning(f"旋律分析失败: {e}")
        data = {}
    finally:
        try:
            os.unlink(audio_path)
        except Exception:
            pass

    if not data:
        return {}
    return await _extract_melody_data(data, identity, flash_llm_func)


async def _extract_melody_data(data: dict, identity: SongIdentity, flash_llm_func=None) -> dict:
    """从 analyze_song.py 的输出提取结构化数据 + Flash 总结"""
    bpm = data.get("bpm")
    key = data.get("key", "")
    segments = data.get("segments", [])
    result = {"bpm": bpm, "key": key, "energy_segments": segments}

    if flash_llm_func:
        seg_text = ", ".join(
            f"{s['start']:.0f}s-{s['end']:.0f}s E={s['avgEnergy']:.2f}"
            for s in (segments or [])[:6]
        )
        prompt = (
            f"《{identity.title}》- {identity.artist} 的音频分析结果：\n"
            f"BPM: {bpm or '?'} | 调性: {key or '?'}\n"
            + (f"能量曲线: {seg_text}\n" if seg_text else "")
            + f"请用1-2句话描述这首歌的旋律特征和情绪氛围。"
        )
        try:
            summary = await flash_llm_func(prompt)
            result["melody_summary"] = summary.strip() if summary else ""
        except Exception:
            result["melody_summary"] = ""
    return result
