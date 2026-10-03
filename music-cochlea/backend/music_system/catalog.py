"""Shared-song catalogue projections.

The catalogue remembers card deliveries, not playback.  Message and
generation identities make repeat accounting idempotent across retries.
"""
from __future__ import annotations

from typing import Any

from .service import get_service


def song_from_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(card.get("song_id") or ""),
        "name": str(card.get("title") or ""),
        "artist": str(card.get("artist") or ""),
        "album": "",
        "cover": str(card.get("cover") or ""),
        "duration": int(card.get("duration") or 0),
        "link": str(card.get("link") or ""),
    }


def record_user_card(card: dict[str, Any], message_id: str) -> dict[str, Any]:
    """Attach the stable occurrence number to one persisted user card."""
    if card.get("kind") != "music" or card.get("music_type") == "playlist" or card.get("playlist_id"):
        return card
    song = get_service().store.record_song_share(
        song_from_card(card), f"message:{message_id}", "owner"
    )
    return {
        **card,
        "share_number": str(song["share_number"]),
        "share_count": str(song["share_count"]),
    }
