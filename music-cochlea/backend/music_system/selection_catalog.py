"""Factual candidate provenance and bounded variety for K's music choice."""
from __future__ import annotations

from collections import deque

SUBJECT_LABELS = {"owner": "使用者", "k": "K", "shared": "共有"}


def memberships(song: dict) -> list[dict[str, str]]:
    result = []
    seen = set()
    for item in song.get("playlist_memberships") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        subject = str(item.get("subject") or "").strip()
        if not pid or subject not in SUBJECT_LABELS or pid in seen:
            continue
        seen.add(pid)
        result.append({"id": pid, "name": str(item.get("name") or "")[:100], "subject": subject})
    return result


def merge_shelf_candidates(cached: list[dict], shelf: list[dict]) -> list[dict]:
    """Keep cache material while attaching playlist ownership by exact song ID."""
    result = [dict(song) for song in cached]
    by_id = {str(song.get("netease_song_id")): song for song in result if song.get("netease_song_id")}
    for candidate in shelf:
        sid = str(candidate.get("netease_song_id") or "")
        if not sid:
            continue
        current = by_id.get(sid)
        if current is None:
            current = dict(candidate)
            result.append(current)
            by_id[sid] = current
        else:
            combined = memberships(current) + memberships(candidate)
            current["playlist_memberships"] = combined
        unique = {item["id"]: item for item in memberships(current)}
        current["playlist_memberships"] = list(unique.values())
        current["playlist_ids"] = list(unique)
        current["playlist_names"] = [item["name"] for item in unique.values()]
        current["music_shelf"] = bool(unique)
    return result


def preview_candidates(cached: list[dict], *, recent_fingerprints: set[str] | None = None,
                       limit: int = 30) -> list[dict]:
    """Show each available shelf and ordinary cache without forcing K's choice."""
    recent = recent_fingerprints or set()
    buckets: dict[str, list[dict]] = {key: [] for key in ("owner", "shared", "k", "cache")}
    for song in cached:
        owners = {item["subject"] for item in memberships(song)}
        for subject in owners or {"cache"}:
            buckets[subject].append(song)
    queues = {}
    for subject, songs in buckets.items():
        queues[subject] = deque(
            [song for song in songs if str(song.get("fingerprint") or "") not in recent]
            + [song for song in songs if str(song.get("fingerprint") or "") in recent]
        )
    result = []
    seen = set()
    while len(result) < max(0, limit) and any(queues.values()):
        for subject in ("owner", "shared", "k", "cache"):
            queue = queues[subject]
            while queue:
                song = queue.popleft()
                identity = str(song.get("netease_song_id") or song.get("fingerprint") or "")
                if identity and identity not in seen:
                    seen.add(identity)
                    result.append(song)
                    break
            if len(result) >= limit:
                break
    return result


def selected_origin(selection: dict, song: dict) -> tuple[str, dict[str, str] | None]:
    """A song's membership alone cannot reveal which source K chose."""
    if selection.get("from_cache") is not True:
        return "search_result", None
    source_id = str(selection.get("source_playlist_id") or "").strip()
    source = next((item for item in memberships(song) if item["id"] == source_id), None)
    if source:
        return source["subject"] + "_playlist_candidate", source
    return "cache_candidate", None


def annotate_selection(song: dict, selection: dict, shelf: list[dict]) -> dict:
    """Persist the chosen path separately from exact-ID playlist membership."""
    result = dict(song)
    sid = str(result.get("netease_song_id") or "")
    known = next((item for item in shelf if str(item.get("netease_song_id") or "") == sid), None)
    # Only the current, explicitly bound shelf can substantiate ownership.
    # Fetched/cache metadata may be stale or belong to an unbound playlist.
    known_memberships = memberships(known) if known else []
    result["playlist_memberships"] = known_memberships
    result["playlist_ids"] = [item["id"] for item in known_memberships]
    result["playlist_names"] = [item["name"] for item in known_memberships]
    origin, source = selected_origin(selection, result)
    result["selection_origin"] = origin
    result["source_playlist_id"] = source["id"] if source else ""
    result["source_playlist_name"] = source["name"] if source else ""
    result["source_playlist_subject"] = source["subject"] if source else ""
    return result
