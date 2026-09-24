"""Shared source-lineage checks for completed active-day settlement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping, Sequence


ASSEMBLER_VERSION = "day-settlement-assembler-v2"


def effective_event_status_sql(alias: str) -> str:
    """Return the append-only effective-status expression used by both boundaries."""

    return (
        "COALESCE((SELECT status FROM event_status_log status_log "
        f"WHERE status_log.event_id={alias}.id "
        "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1),"
        "'active')"
    )


def has_event_thread_status_log(connection: sqlite3.Connection) -> bool:
    """Whether this read source has the schema-v9 thread correction log."""

    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='event_thread_status_log'"
    ).fetchone() is not None


def effective_event_thread_status_sql(
    alias: str,
    *,
    status_log_available: bool = True,
) -> str:
    """Return the append-only effective status for an immutable event thread."""

    if not status_log_available:
        return "'active'"
    return (
        "COALESCE((SELECT status FROM event_thread_status_log thread_status_log "
        f"WHERE thread_status_log.thread_id={alias}.id "
        "ORDER BY thread_status_log.created_at DESC, "
        "thread_status_log.id DESC LIMIT 1),'active')"
    )


def load_day_lineage_rows(
    connection: sqlite3.Connection,
    source_namespace: str,
    active_date: str,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Load the exact immutable batch/status projection covered by the digest."""

    batch_rows = connection.execute(
        "SELECT id, source_digest, status, event_count, from_message_row_id, "
        "to_message_row_id FROM encoding_batches "
        "WHERE source_namespace=? AND active_date=? "
        "ORDER BY from_message_row_id, to_message_row_id, id",
        (source_namespace, active_date),
    ).fetchall()
    status_sql = effective_event_status_sql("e")
    event_rows = connection.execute(
        "SELECT e.id, e.content_digest, "
        f"{status_sql} AS current_status FROM events e "
        "JOIN encoding_batches b ON b.id=e.batch_id "
        "WHERE b.source_namespace=? AND b.active_date=? AND b.status='completed' "
        "ORDER BY e.reported_at, e.id",
        (source_namespace, active_date),
    ).fetchall()
    return list(batch_rows), list(event_rows)


def day_source_digest(
    *,
    source_namespace: str,
    active_date: str,
    batch_rows: Sequence[Mapping[str, object]],
    event_status_rows: Sequence[Mapping[str, object]],
    assembler_version: str = ASSEMBLER_VERSION,
) -> str:
    """Digest the exact source projection used to assemble a completed day."""

    payload = {
        "assembler_version": assembler_version,
        "source_namespace": source_namespace,
        "active_date": active_date,
        "batches": [dict(row) for row in batch_rows],
        "event_statuses": [dict(row) for row in event_status_rows],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_day_source_digest(
    connection: sqlite3.Connection,
    source_namespace: str,
    active_date: str,
    *,
    assembler_version: str = ASSEMBLER_VERSION,
) -> tuple[str, list[sqlite3.Row], list[sqlite3.Row]]:
    """Load and digest current source lineage in one SQLite read transaction."""

    batch_rows, event_rows = load_day_lineage_rows(
        connection, source_namespace, active_date
    )
    digest = day_source_digest(
        source_namespace=source_namespace,
        active_date=active_date,
        batch_rows=batch_rows,
        event_status_rows=event_rows,
        assembler_version=assembler_version,
    )
    return digest, batch_rows, event_rows
