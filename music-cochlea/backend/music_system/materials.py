"""Cached song material keyed by provider ID; repeated cards do not call an LLM."""
from __future__ import annotations

import json
import re
import asyncio
from pathlib import Path

from .service import get_service

LYRICS_EXCERPT_MAX = 1200
MELODY_SUMMARY_MAX = 500
_analysis_tasks: dict[str, asyncio.Task] = {}


def lyric_excerpt(value: str, limit: int = LYRICS_EXCERPT_MAX) -> str:
    """Remove LRC clocks/duplicates and keep a bounded context attachment."""
    lines: list[str] = []
    for raw in str(value or "").splitlines():
        clean = re.sub(r"\[[^\]]*\]", "", raw).strip()
        if not clean or clean in lines:
            continue
        lines.append(clean)
        if len("\n".join(lines)) >= limit:
            break
    return "\n".join(lines)[:limit].rstrip()


def _objective_summary(data: dict) -> str:
    bpm = data.get("bpm")
    key = str(data.get("key") or data.get("key_name") or "").strip()
    segments = data.get("segments") or data.get("energy_segments") or []
    if isinstance(segments, str):
        try:
            segments = json.loads(segments)
        except (TypeError, ValueError):
            segments = []
    energies = [float(item.get("avgEnergy")) for item in segments if isinstance(item, dict) and item.get("avgEnergy") is not None]
    facts = []
    if bpm:
        try:
            facts.append(f"约 {round(float(bpm))} BPM")
        except (TypeError, ValueError):
            pass
    if key:
        facts.append(f"调性 {key}")
    if len(energies) >= 2:
        first = sum(energies[:max(1, len(energies) // 3)]) / max(1, len(energies) // 3)
        last_count = max(1, len(energies) // 3)
        last = sum(energies[-last_count:]) / last_count
        if last > first * 1.35:
            facts.append("截取段落的能量整体向后抬升")
        elif first > last * 1.35:
            facts.append("截取段落的能量整体向后收束")
        else:
            facts.append("截取段落的能量相对平稳")
    return "；".join(facts)[:MELODY_SUMMARY_MAX]


def _legacy(song_id: str) -> dict:
    try:
        from music_cochlea.cache import SongCache
        row = SongCache().get_meta_by_netease_id(int(song_id)) or {}
        return {
            "lyrics": row.get("lyrics") or row.get("lyrics_snippet") or "",
            "melody_summary": row.get("melody_summary") or _objective_summary(row),
            "bpm": row.get("bpm"),
            "key": row.get("key_name") or "",
            "energy_segments": row.get("energy_segments") or "",
        }
    except Exception:
        return {}


def _preanalysis(song_id: str) -> dict:
    path = Path(__file__).resolve().parents[1] / "data" / "melody_cache" / f"{int(song_id)}_preanalysis.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {
        "bpm": data.get("bpm"), "key": data.get("key") or "",
        "energy_segments": data.get("segments") or [],
        "melody_summary": _objective_summary(data),
    }


def project(song: dict, material: dict | None) -> dict:
    material = material or {}
    excerpt = lyric_excerpt(material.get("lyrics") or material.get("lyrics_excerpt") or "")
    melody = str(material.get("melody_summary") or "")[:MELODY_SUMMARY_MAX]
    return {
        **song,
        "lyrics_available": bool(excerpt),
        "lyrics_excerpt": excerpt,
        "melody_summary": melody,
        "material_status": "ready" if excerpt and melody else "partial" if excerpt or melody else "missing",
    }


async def prepare(song: dict, *, analyze: bool = False) -> dict:
    """Prepare lyrics immediately and optionally run objective audio analysis.

    Audio analysis never calls an LLM.  Long analysis is only requested by a
    background lifecycle task; card creation can return with a truthful partial
    material receipt instead of blocking the conversation.
    """
    service = get_service()
    song_id = str(song["id"])
    result = {"song": song, **(service.store.material(song_id) or {})}
    legacy = _legacy(song_id)
    preanalysis = _preanalysis(song_id)
    for source in (legacy, preanalysis):
        for key, value in source.items():
            if value not in (None, "", []) and result.get(key) in (None, "", []):
                result[key] = value
    if not str(result.get("lyrics") or "").strip():
        def fetch():
            from pyncm.apis import track
            payload = track.GetTrackLyrics(int(song_id))
            lyrics = (payload.get("lrc") or {}).get("lyric") or ""
            translation = (payload.get("tlyric") or {}).get("lyric") or ""
            return lyrics + ("\n" + translation if translation else "")
        try:
            result["lyrics"] = await service.provider._call(fetch)
            result["source"] = "网易云歌词接口"
        except Exception:
            result["lyrics"] = ""
    if analyze and not result.get("melody_summary"):
        try:
            from music_cochlea.enricher import analyze_melody
            from music_cochlea.models import SongIdentity
            identity = SongIdentity(title=song.get("name") or "", artist=song.get("artist") or "")
            analyzed = await analyze_melody(identity, song_id=int(song_id))
            if isinstance(analyzed, dict):
                result.update({key: value for key, value in analyzed.items() if value not in (None, "", [])})
                result["melody_summary"] = result.get("melody_summary") or _objective_summary(analyzed)
        except Exception:
            # Lyrics and identity remain valid material even when audio is not obtainable.
            pass
    result["lyrics_excerpt"] = lyric_excerpt(result.get("lyrics") or "")
    result["melody_summary"] = str(result.get("melody_summary") or "")[:MELODY_SUMMARY_MAX]
    if result.get("lyrics") or result.get("melody_summary"):
        service.store.save_material(song_id, result)
    return result


def schedule_analysis(song: dict) -> bool:
    """Start one deduplicated objective analysis task and return pending state."""
    song_id = str(song.get("id") or "")
    if not song_id:
        return False
    cached = get_service().store.material(song_id) or {}
    if cached.get("melody_summary"):
        return False
    existing = _analysis_tasks.get(song_id)
    if existing and not existing.done():
        return True

    async def run() -> None:
        await prepare(song, analyze=True)

    task = asyncio.create_task(run())
    _analysis_tasks[song_id] = task
    def finished(completed: asyncio.Task, sid: str = song_id) -> None:
        _analysis_tasks.pop(sid, None)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            # Optional material remains partial; playback/share facts are untouched.
            pass
    task.add_done_callback(finished)
    return True


def analysis_pending(song_id: str) -> bool:
    task = _analysis_tasks.get(str(song_id))
    return bool(task and not task.done())


async def stop() -> None:
    tasks = list(_analysis_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _analysis_tasks.clear()
