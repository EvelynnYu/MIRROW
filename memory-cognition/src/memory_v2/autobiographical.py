"""Read-side projection from structured diaries to event source anchors.

The diary remains Agent's subjective authority.  This module merely lets an event
document display and search the small, source-backed autobiographical fragment
produced in the same daily generation.  It never creates a second candidate or
writes back to either database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


def _chunks(values: Sequence[str], size: int = 500):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def load_recollections_by_message_id(
    authority_db_path: str | Path,
) -> Mapping[str, tuple[Dict[str, Any], ...]]:
    """Load only diary recollections whose source messages still exist."""
    connection = sqlite3.connect(str(authority_db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diary_entries'"
        ).fetchone()
        if table is None:
            return {}
        diary_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(diary_entries)")
        }
        source_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
        ).fetchone()
        if "structured_json" not in diary_columns or source_table is None:
            return {}
        rows = connection.execute(
            "SELECT date, structured_json FROM diary_entries ORDER BY date, id"
        ).fetchall()
        pending: list[Dict[str, Any]] = []
        all_ids: list[str] = []
        for row in rows:
            try:
                payload = json.loads(str(row["structured_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            for ordinal, item in enumerate(payload.get("agent_recollections") or []):
                if not isinstance(item, dict):
                    continue
                memory = str(item.get("memory") or "").strip()
                source_ids = tuple(dict.fromkeys(
                    str(value).strip()
                    for value in (item.get("source_message_ids") or [])
                    if str(value).strip()
                ))
                if not memory or not source_ids:
                    continue
                record = {
                    "date": str(row["date"] or ""),
                    "ordinal": ordinal,
                    "memory": memory,
                    "source_message_ids": source_ids,
                }
                pending.append(record)
                all_ids.extend(source_ids)

        valid_ids: set[str] = set()
        unique_ids = list(dict.fromkeys(all_ids))
        for group in _chunks(unique_ids):
            placeholders = ",".join("?" for _ in group)
            valid_ids.update(
                str(row[0])
                for row in connection.execute(
                    f"SELECT message_id FROM conversation_messages WHERE message_id IN ({placeholders})",
                    group,
                ).fetchall()
            )
    finally:
        connection.close()

    result: dict[str, list[Dict[str, Any]]] = {}
    for record in pending:
        clean_ids = tuple(value for value in record["source_message_ids"] if value in valid_ids)
        if not clean_ids:
            continue
        clean = dict(record)
        clean["source_message_ids"] = clean_ids
        for message_id in clean_ids:
            result.setdefault(message_id, []).append(clean)
    return {
        message_id: tuple(sorted(items, key=lambda item: (item["date"], item["ordinal"])))
        for message_id, items in result.items()
    }


def recollections_for_sources(
    source_message_ids: Sequence[str],
    by_message_id: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    limit: int = 3,
) -> tuple[Mapping[str, Any], ...]:
    """Return de-duplicated representative recollections for one event/thread."""
    unique: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for message_id in source_message_ids:
        for item in by_message_id.get(str(message_id), ()):  # type: ignore[arg-type]
            key = (
                str(item.get("date") or ""),
                int(item.get("ordinal") or 0),
                str(item.get("memory") or ""),
            )
            unique.setdefault(key, item)
    ordered = [unique[key] for key in sorted(unique)]
    if limit <= 0 or len(ordered) <= limit:
        return tuple(ordered)
    # Long event threads keep their beginning, one middle turn and latest state.
    indices = (0, len(ordered) // 2, len(ordered) - 1)
    return tuple(ordered[index] for index in dict.fromkeys(indices))[:limit]


__all__ = ["load_recollections_by_message_id", "recollections_for_sources"]
