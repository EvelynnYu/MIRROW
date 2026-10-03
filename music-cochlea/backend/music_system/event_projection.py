"""Project confirmed music-library mutations into the chat event timeline.

The music journal remains authoritative.  The stable event ID and watermark
make a failed or interrupted projection safe to retry without replaying a
NetEase write.  Pre-fix track rows have no before/after proof and stay out.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable


_WATERMARK = "system:library_event_projection"
_ACTIONS = {
    "created": "创建了歌单",
    "renamed": "把歌单改名为",
    "deleted": "删除了歌单",
    "tracks_added": "把歌曲加入歌单",
    "tracks_removed": "从歌单移除歌曲",
}


def persisted_action_actor(message_id: str, event_type: str, content: str) -> str:
    """Decode only this producer's fixed actor/action header, including old rows.

    This preserves the immutable source text and does not consult mutable song
    data or infer an actor from quotations elsewhere in the body.
    """
    if event_type != 'music_library_event' or not re.fullmatch(r'music_library_event_[0-9]+', message_id):
        return ''
    text = content.removeprefix('[事件记录] ')
    for actor, label in (('k', 'K'), ('owner', '使用者')):
        if any(text.startswith(label + '已确认' + action) for action in _ACTIONS.values()):
            return actor
    return ''


def event_fact(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(event.get("event_type") or "")
    details = event.get("details") or {}
    if kind not in _ACTIONS:
        return None
    if kind in {"tracks_added", "tracks_removed"} and not details.get("verified_change"):
        return None
    actor = "K" if details.get("source") in {"wander", "k_tool"} else "使用者"
    name = str(details.get("name") or details.get("after") or "")[:100]
    song_name = str(details.get("song_title") or "")[:160]
    song_artist = str(details.get("song_artist") or "")[:120]
    song_ids = [str(value) for value in (details.get("song_ids") or [])[:5]]
    if kind in {"tracks_added", "tracks_removed"}:
        song = f"《{song_name}》" + (f"—{song_artist}" if song_artist else "") if song_name else (
            "歌曲 ID " + "、".join(song_ids) if song_ids else "一首歌曲"
        )
        content = f"{actor}已确认{_ACTIONS[kind]}：{song}；歌单《{name}》。"
    elif kind == "renamed":
        content = f"{actor}已确认{_ACTIONS[kind]}《{name}》（原名《{str(details.get('before') or '')[:100]}》）。"
    else:
        content = f"{actor}已确认{_ACTIONS[kind]}《{name}》。"
    reason = str(details.get("reason") or "").strip()[:300]
    if reason:
        content += f"{actor}当时的感想：{reason}（自述，不是操作或播放证据）。"
    return {
        "event_id": f"music_library_event_{event['id']}",
        "occurred_at": str(event.get("occurred_at") or ""),
        "content": content,
        "playlist_id": str(event.get("playlist_id") or ""),
        "node_id": str(details.get("node_id") or ""),
        "source_event_id": int(event["id"]),
        "actor_id": "k" if actor == "K" else "owner",
    }


async def project_pending(store: Any, write_event: Callable[[dict[str, Any]], Any]) -> int:
    """Advance only after each destination commit; skipped old rows also advance."""
    watermark = int((store.material(_WATERMARK) or {}).get("last_id") or 0)
    projected = 0
    for event in store.library_events_after(watermark):
        fact = event_fact(event)
        if fact is not None:
            result = write_event(fact)
            if inspect.isawaitable(result):
                result = await result
            if result is not True:
                break
            projected += 1
        watermark = int(event["id"])
        store.save_material(_WATERMARK, {"last_id": watermark})
    return projected
