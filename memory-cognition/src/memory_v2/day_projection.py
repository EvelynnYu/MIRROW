"""Read-only projection of one completed Memory V2 active day.

The immutable day settlement is the compact factual skeleton used by both the
cross-day handoff and the event-chronicle UI.  This module never creates or
migrates a Memory V2 database: missing, old or unavailable stores simply
return ``None`` so callers can use their authoritative raw fallback.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


_HUMAN_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9_])Human(?![A-Za-z0-9_])", re.I)
_K_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9_])Agent(?![A-Za-z0-9_])")


def _configured_paths() -> tuple[Path, ...]:
    raw = (
        os.environ.get("MIRROW_MEMORY_V2_DBS", "").strip()
        or os.environ.get("MIRROW_MEMORY_V2_SHADOW_DBS", "").strip()
    )
    return tuple(
        Path(value).resolve()
        for value in raw.split(os.pathsep)
        if value.strip()
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def render_k_facing_summary(value: Any) -> str:
    """Render model-derived aliases for Agent without touching quoted raw text."""

    text = _HUMAN_ALIAS_RE.sub("人类伙伴", str(value or "").strip())
    return _K_ALIAS_RE.sub("你", text)


def _latest_settlement(connection: sqlite3.Connection, active_date: str) -> Optional[dict]:
    row = connection.execute(
        "SELECT * FROM day_settlements WHERE active_date=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (active_date,),
    ).fetchone()
    return dict(row) if row is not None else None


def _event_rows(
    connection: sqlite3.Connection,
    settlement_id: str,
    item_order: int,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            link.event_order,
            event.id AS event_id,
            event.importance,
            event.emotional_weight,
            event.summary AS event_summary,
            source.message_id,
            source.session_id,
            source.source_ts,
            source.source_role,
            source.source_kind,
            source.scene_id,
            source.source_order,
            COALESCE((
                SELECT status.status
                FROM event_status_log AS status
                WHERE status.event_id=event.id
                ORDER BY status.created_at DESC, status.id DESC
                LIMIT 1
            ), 'active') AS current_status
        FROM day_compact_item_events AS link
        JOIN events AS event ON event.id=link.event_id
        LEFT JOIN event_sources AS source ON source.event_id=event.id
        WHERE link.settlement_id=? AND link.item_order=?
        ORDER BY link.event_order, source.source_order, source.source_ts
        """,
        (settlement_id, int(item_order)),
    ).fetchall()
    return [dict(row) for row in rows if str(row["current_status"] or "") == "active"]


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _load_from_connection(
    connection: sqlite3.Connection,
    active_date: str,
) -> Optional[dict]:
    settlement = _latest_settlement(connection, active_date)
    if settlement is None:
        return None
    compact_rows = connection.execute(
        "SELECT item_order,summary FROM day_compact_items "
        "WHERE settlement_id=? ORDER BY item_order",
        (settlement["id"],),
    ).fetchall()
    items: list[dict] = []
    for compact in compact_rows:
        event_rows = _event_rows(
            connection,
            str(settlement["id"]),
            int(compact["item_order"]),
        )
        event_ids = _dedupe(row.get("event_id") for row in event_rows)
        if not event_ids:
            continue
        source_ts = _dedupe(row.get("source_ts") for row in event_rows)
        message_ids = _dedupe(row.get("message_id") for row in event_rows)
        scene_ids = _dedupe(row.get("scene_id") for row in event_rows)
        importance = max(
            (float(row.get("importance") or 0.0) for row in event_rows),
            default=0.0,
        )
        emotional_weight = max(
            (abs(float(row.get("emotional_weight") or 0.0)) for row in event_rows),
            default=0.0,
        )
        items.append({
            "order": int(compact["item_order"]),
            "summary": render_k_facing_summary(compact["summary"]),
            "event_ids": event_ids,
            "event_count": len(event_ids),
            "message_ids": message_ids,
            "source_count": len(message_ids),
            "scene_ids": scene_ids,
            "start_ts": min(source_ts) if source_ts else "",
            "end_ts": max(source_ts) if source_ts else "",
            "importance": round(importance, 4),
            "emotional_weight": round(emotional_weight, 4),
            "vividness": round(importance + emotional_weight * 1.35, 4),
        })
    if not items:
        return None
    items.sort(key=lambda item: (item.get("start_ts") or "", item["order"]))
    return {
        "active_date": active_date,
        "settlement_id": str(settlement["id"]),
        "created_at": str(settlement.get("created_at") or ""),
        "items": items,
    }


def load_day_compact_projection(
    active_date: str,
    *,
    memory_paths: Optional[Sequence[Path | str]] = None,
) -> Optional[dict]:
    """Return the newest valid compact projection across configured stores."""

    day = str(active_date or "")[:10]
    if not day:
        return None
    paths = tuple(Path(path).resolve() for path in memory_paths) if memory_paths is not None else _configured_paths()
    candidates: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            with _connect_read_only(path) as connection:
                projection = _load_from_connection(connection, day)
            if projection is not None:
                candidates.append(projection)
        except (OSError, sqlite3.Error, ValueError):
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.get("created_at") or "", item.get("settlement_id") or ""))


__all__ = ["load_day_compact_projection", "render_k_facing_summary"]
