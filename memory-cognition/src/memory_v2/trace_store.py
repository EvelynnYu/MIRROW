"""Local, derived diagnostics for one Memory V2 recall turn.

The trace database is deliberately not a memory authority.  It keeps the
bounded recall result and the memory-only context snapshot needed to explain
what Agent could see for one persisted reply.  Feedback is append-only evaluation
data and never mutates events, cognition, Affect, or Open Loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TRACE_SCHEMA_VERSION = 3
TRACE_PIPELINE_VERSION = "memory-v2-bounded-query-v5"
TRACE_PROJECTION_VERSION = "memory-v2-k-second-person-v5"
TRACE_INDEX_VERSION = "memory-v2-recall-two-lane-v3"
TRACE_FEEDBACK_LABELS = frozenset(
    {"accurate", "missed", "wrong_or_crossed", "too_much"}
)

_MAX_JSON_CHARS = 512_000
_MAX_NOTE_CHARS = 1_000
_default_store_lock = threading.Lock()
_default_store: "MemoryRecallTraceStore | None" = None
_default_store_path: Path | None = None

MEMORY_SECTION_NAMES = frozenset(
    {
        "memories",
        "self_book",
        "other_core",
        "other_details",
        "cognitive_core",
        "day_before_diary",
        "yesterday",
        "gap_diaries",
        "referenced_diary",
        "important_events",
        "emotion_self",
        "emotion_resonance",
        "open_loops_v2",
        "memory_v2",
        "self_awareness",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS memory_recall_trace_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_recall_traces (
    trace_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finalized_at TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    user_message_id TEXT NOT NULL DEFAULT '',
    assistant_message_id TEXT NOT NULL DEFAULT '',
    reference_date TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'shadow',
    status TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    index_version TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    routing_json TEXT NOT NULL DEFAULT '{}',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    expected_projection_text TEXT NOT NULL DEFAULT '',
    actual_sections_json TEXT NOT NULL DEFAULT '[]',
    actual_injected_text TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_assistant
ON memory_recall_traces(assistant_message_id);

CREATE INDEX IF NOT EXISTS idx_memory_recall_traces_user
ON memory_recall_traces(user_message_id);

CREATE TABLE IF NOT EXISTS memory_recall_feedback (
    feedback_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    label TEXT NOT NULL CHECK (
        label IN ('accurate', 'missed', 'wrong_or_crossed', 'too_much')
    ),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(trace_id) REFERENCES memory_recall_traces(trace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_feedback_trace
ON memory_recall_feedback(trace_id, created_at);

CREATE TABLE IF NOT EXISTS memory_recall_message_bindings (
    trace_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    binding_kind TEXT NOT NULL DEFAULT 'intermediate',
    ordinal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(trace_id, message_id),
    FOREIGN KEY(trace_id) REFERENCES memory_recall_traces(trace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_message_bindings_message
ON memory_recall_message_bindings(message_id);

CREATE TABLE IF NOT EXISTS memory_recall_search_attempts (
    attempt_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    include_source_detail INTEGER NOT NULL DEFAULT 0,
    requested_limit INTEGER NOT NULL,
    reused INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    projection_text TEXT NOT NULL DEFAULT '',
    UNIQUE(trace_id, ordinal),
    FOREIGN KEY(trace_id) REFERENCES memory_recall_traces(trace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_search_attempts_trace
ON memory_recall_search_attempts(trace_id, ordinal);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def query_digest(query_text: str) -> str:
    return hashlib.sha256(str(query_text or "").encode("utf-8")).hexdigest()


def default_trace_db_path() -> Path:
    backend_root = Path(__file__).resolve().parents[1]
    return Path(
        os.environ.get(
            "MIRROW_MEMORY_V2_TRACE_DB",
            str(backend_root / "events" / "memory_v2_diagnostics.db"),
        )
    ).resolve()


def trace_enabled() -> bool:
    return os.environ.get("MIRROW_MEMORY_V2_TRACE", "1").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def get_default_trace_store() -> "MemoryRecallTraceStore":
    global _default_store, _default_store_path
    path = default_trace_db_path()
    with _default_store_lock:
        if _default_store is None or _default_store_path != path:
            _default_store = MemoryRecallTraceStore(path)
            _default_store_path = path
        return _default_store


def _redact(value: Any) -> Any:
    try:
        from context_inspection import redact

        return redact(value)
    except Exception:
        return value


def _json_text(value: Any) -> str:
    encoded = json.dumps(
        _redact(value), ensure_ascii=False, separators=(",", ":"), default=str
    )
    if len(encoded) > _MAX_JSON_CHARS:
        raise ValueError("memory recall diagnostic payload exceeds its local bound")
    return encoded


def _decode_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class MemoryRecallTraceStore:
    """Small connection-per-operation store for local recall diagnostics."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_DDL)
            row = connection.execute(
                "SELECT schema_version FROM memory_recall_trace_meta WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO memory_recall_trace_meta "
                    "(singleton, schema_version, created_at, updated_at) "
                    "VALUES (1, ?, ?, ?)",
                    (TRACE_SCHEMA_VERSION, now, now),
                )
            elif int(row["schema_version"]) in {1, 2}:
                columns = {
                    str(item["name"])
                    for item in connection.execute(
                        "PRAGMA table_info(memory_recall_traces)"
                    ).fetchall()
                }
                if "index_version" not in columns:
                    connection.execute(
                        "ALTER TABLE memory_recall_traces ADD COLUMN "
                        "index_version TEXT NOT NULL DEFAULT 'memory-v2-recall-fts-v1'"
                    )
                connection.execute(
                    "UPDATE memory_recall_trace_meta SET schema_version = ?, updated_at = ? "
                    "WHERE singleton = 1",
                    (TRACE_SCHEMA_VERSION, now),
                )
            elif int(row["schema_version"]) != TRACE_SCHEMA_VERSION:
                raise RuntimeError("unsupported memory recall trace schema version")

    def create_trace(
        self,
        *,
        query_text: str,
        status: str,
        reference_date: str = "",
        session_id: str = "",
        user_message_id: str = "",
        mode: str = "shadow",
        metrics: Mapping[str, Any] | None = None,
        routing: Mapping[str, Any] | None = None,
        candidates: Sequence[Mapping[str, Any]] = (),
        evidence: Mapping[str, Any] | None = None,
        expected_projection_text: str = "",
        pipeline_version: str = TRACE_PIPELINE_VERSION,
        index_version: str = TRACE_INDEX_VERSION,
        projection_version: str = TRACE_PROJECTION_VERSION,
    ) -> str:
        trace_id = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO memory_recall_traces ("
                "trace_id, created_at, session_id, user_message_id, "
                "reference_date, mode, status, query_digest, pipeline_version, "
                "index_version, projection_version, metrics_json, routing_json, candidates_json, "
                "evidence_json, expected_projection_text"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    _utc_now(),
                    str(session_id or ""),
                    str(user_message_id or ""),
                    str(reference_date or ""),
                    str(mode or "shadow"),
                    str(status or "error"),
                    query_digest(query_text),
                    str(pipeline_version or TRACE_PIPELINE_VERSION),
                    str(index_version or TRACE_INDEX_VERSION),
                    str(projection_version or TRACE_PROJECTION_VERSION),
                    _json_text(metrics or {}),
                    _json_text(routing or {}),
                    _json_text(list(candidates)),
                    _json_text(evidence or {}),
                    str(_redact(expected_projection_text or "")),
                ),
            )
        return trace_id

    def finalize_trace(
        self,
        trace_id: str,
        *,
        actual_sections: Sequence[Mapping[str, Any]],
        actual_injected_text: str,
    ) -> bool:
        if not trace_id:
            return False
        sections_json = _json_text(list(actual_sections))
        injected_text = str(_redact(actual_injected_text or ""))
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT finalized_at, actual_sections_json, actual_injected_text "
                "FROM memory_recall_traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if row is None:
                return False
            if row["finalized_at"]:
                return (
                    row["actual_sections_json"] == sections_json
                    and row["actual_injected_text"] == injected_text
                )
            connection.execute(
                "UPDATE memory_recall_traces SET finalized_at = ?, "
                "actual_sections_json = ?, actual_injected_text = ? "
                "WHERE trace_id = ? AND finalized_at = ''",
                (_utc_now(), sections_json, injected_text, trace_id),
            )
            return True

    def bind_assistant_message(self, trace_id: str, assistant_message_id: str) -> bool:
        clean_id = str(assistant_message_id or "").strip()
        if not trace_id or not clean_id:
            return False
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT assistant_message_id FROM memory_recall_traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if row is None:
                return False
            existing = str(row["assistant_message_id"] or "")
            if existing:
                if existing != clean_id:
                    return False
                connection.execute(
                    "INSERT OR IGNORE INTO memory_recall_message_bindings "
                    "(trace_id, message_id, binding_kind, ordinal, created_at) "
                    "VALUES (?, ?, 'final', 0, ?)",
                    (trace_id, clean_id, _utc_now()),
                )
                return True
            connection.execute(
                "UPDATE memory_recall_traces SET assistant_message_id = ? "
                "WHERE trace_id = ? AND assistant_message_id = ''",
                (clean_id, trace_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_recall_message_bindings "
                "(trace_id, message_id, binding_kind, ordinal, created_at) "
                "VALUES (?, ?, 'final', 0, ?)",
                (trace_id, clean_id, _utc_now()),
            )
            return True

    def bind_turn_messages_by_user(
        self,
        *,
        user_message_id: str,
        session_id: str = "",
        assistant_message_ids: Sequence[str] = (),
        final_assistant_message_id: str = "",
    ) -> bool:
        """Bind every persisted Agent bubble in one turn to the same trace."""

        clean_user = str(user_message_id or "").strip()
        clean_ids = tuple(
            dict.fromkeys(
                value
                for item in assistant_message_ids
                if (value := str(item or "").strip())
            )
        )
        clean_final = str(final_assistant_message_id or "").strip()
        if not clean_user or not clean_ids:
            return False
        with closing(self._connect()) as connection, connection:
            params: list[str] = [clean_user]
            where = "user_message_id = ?"
            if str(session_id or "").strip():
                where += " AND session_id = ?"
                params.append(str(session_id).strip())
            row = connection.execute(
                f"SELECT trace_id, assistant_message_id FROM memory_recall_traces "
                f"WHERE {where} ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return False
            trace_id = str(row["trace_id"])
            if clean_final:
                existing = str(row["assistant_message_id"] or "")
                if existing and existing != clean_final:
                    return False
                if not existing:
                    connection.execute(
                        "UPDATE memory_recall_traces SET assistant_message_id = ? "
                        "WHERE trace_id = ? AND assistant_message_id = ''",
                        (clean_final, trace_id),
                    )
            now = _utc_now()
            for ordinal, message_id in enumerate(clean_ids, start=1):
                connection.execute(
                    "INSERT OR IGNORE INTO memory_recall_message_bindings "
                    "(trace_id, message_id, binding_kind, ordinal, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        trace_id,
                        message_id,
                        "final" if message_id == clean_final else "intermediate",
                        ordinal,
                        now,
                    ),
                )
            return True

    def append_search_attempt_by_user_message(
        self,
        *,
        user_message_id: str,
        query_text: str,
        include_source_detail: bool,
        requested_limit: int,
        reused: bool,
        status: str,
        metrics: Mapping[str, Any] | None = None,
        projection_text: str = "",
        session_id: str = "",
    ) -> dict[str, Any] | None:
        clean_user = str(user_message_id or "").strip()
        if not clean_user:
            return None
        attempt_id = uuid.uuid4().hex
        created_at = _utc_now()
        bounded_projection = str(_redact(projection_text or ""))[:12_000]
        with closing(self._connect()) as connection, connection:
            params: list[str] = [clean_user]
            where = "user_message_id = ?"
            if str(session_id or "").strip():
                where += " AND session_id = ?"
                params.append(str(session_id).strip())
            trace = connection.execute(
                f"SELECT trace_id FROM memory_recall_traces WHERE {where} "
                "ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if trace is None:
                return None
            trace_id = str(trace["trace_id"])
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal "
                    "FROM memory_recall_search_attempts WHERE trace_id = ?",
                    (trace_id,),
                ).fetchone()["next_ordinal"]
            )
            connection.execute(
                "INSERT INTO memory_recall_search_attempts "
                "(attempt_id, trace_id, ordinal, created_at, query_digest, "
                "include_source_detail, requested_limit, reused, status, "
                "metrics_json, projection_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    trace_id,
                    ordinal,
                    created_at,
                    query_digest(query_text),
                    int(bool(include_source_detail)),
                    max(1, min(int(requested_limit), 10)),
                    int(bool(reused)),
                    str(status or "error"),
                    _json_text(metrics or {}),
                    bounded_projection,
                ),
            )
        return {
            "attempt_id": attempt_id,
            "trace_id": trace_id,
            "ordinal": ordinal,
            "created_at": created_at,
            "include_source_detail": bool(include_source_detail),
            "requested_limit": max(1, min(int(requested_limit), 10)),
            "reused": bool(reused),
            "status": str(status or "error"),
        }

    def append_feedback(self, trace_id: str, label: str, note: str = "") -> dict[str, Any] | None:
        clean_label = str(label or "").strip()
        if clean_label not in TRACE_FEEDBACK_LABELS:
            raise ValueError("unsupported memory recall feedback label")
        clean_note = str(_redact(str(note or "").strip()))
        if len(clean_note) > _MAX_NOTE_CHARS:
            raise ValueError("memory recall feedback note is too long")
        feedback_id = uuid.uuid4().hex
        created_at = _utc_now()
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM memory_recall_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if exists is None:
                return None
            connection.execute(
                "INSERT INTO memory_recall_feedback "
                "(feedback_id, trace_id, label, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (feedback_id, trace_id, clean_label, clean_note, created_at),
            )
        return {
            "feedback_id": feedback_id,
            "trace_id": trace_id,
            "label": clean_label,
            "note": clean_note,
            "created_at": created_at,
        }

    def get_by_assistant_message(self, assistant_message_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT DISTINCT t.* FROM memory_recall_traces AS t "
                "LEFT JOIN memory_recall_message_bindings AS b ON b.trace_id = t.trace_id "
                "WHERE t.assistant_message_id = ? OR b.message_id = ? "
                "ORDER BY t.created_at DESC LIMIT 1",
                (str(assistant_message_id or ""), str(assistant_message_id or "")),
            ).fetchone()
            if row is None:
                return None
            feedback_rows = connection.execute(
                "SELECT feedback_id, label, note, created_at "
                "FROM memory_recall_feedback WHERE trace_id = ? "
                "ORDER BY created_at ASC, feedback_id ASC",
                (row["trace_id"],),
            ).fetchall()
            search_rows = self._search_rows(connection, str(row["trace_id"]))
            binding_rows = self._binding_rows(connection, str(row["trace_id"]))
        return self._public_row(row, feedback_rows, search_rows, binding_rows)

    def get_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM memory_recall_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if row is None:
                return None
            feedback_rows = connection.execute(
                "SELECT feedback_id, label, note, created_at "
                "FROM memory_recall_feedback WHERE trace_id = ? "
                "ORDER BY created_at ASC, feedback_id ASC",
                (trace_id,),
            ).fetchall()
            search_rows = self._search_rows(connection, trace_id)
            binding_rows = self._binding_rows(connection, trace_id)
        return self._public_row(row, feedback_rows, search_rows, binding_rows)

    def delete_by_message_ids(self, message_ids: Sequence[str]) -> int:
        clean_ids = tuple(
            dict.fromkeys(
                clean
                for item in message_ids
                if (clean := str(item or "").strip())
            )
        )
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        with closing(self._connect()) as connection, connection:
            bound_rows = connection.execute(
                f"SELECT DISTINCT trace_id FROM memory_recall_message_bindings "
                f"WHERE message_id IN ({placeholders})",
                clean_ids,
            ).fetchall()
            bound_trace_ids = tuple(str(row["trace_id"]) for row in bound_rows)
            binding_clause = ""
            params: tuple[str, ...] = (*clean_ids, *clean_ids)
            if bound_trace_ids:
                trace_placeholders = ",".join("?" for _ in bound_trace_ids)
                binding_clause = f" OR trace_id IN ({trace_placeholders})"
                params = (*params, *bound_trace_ids)
            cursor = connection.execute(
                f"DELETE FROM memory_recall_traces WHERE "
                f"assistant_message_id IN ({placeholders}) OR "
                f"user_message_id IN ({placeholders}){binding_clause}",
                params,
            )
            return max(0, int(cursor.rowcount or 0))

    @staticmethod
    def _search_rows(connection: sqlite3.Connection, trace_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT attempt_id, ordinal, created_at, query_digest, "
            "include_source_detail, requested_limit, reused, status, "
            "metrics_json, projection_text FROM memory_recall_search_attempts "
            "WHERE trace_id = ? ORDER BY ordinal ASC",
            (trace_id,),
        ).fetchall()

    @staticmethod
    def _binding_rows(connection: sqlite3.Connection, trace_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT message_id, binding_kind, ordinal FROM memory_recall_message_bindings "
            "WHERE trace_id = ? ORDER BY ordinal ASC, message_id ASC",
            (trace_id,),
        ).fetchall()

    @staticmethod
    def _public_row(
        row: sqlite3.Row,
        feedback_rows: Sequence[sqlite3.Row],
        search_rows: Sequence[sqlite3.Row] = (),
        binding_rows: Sequence[sqlite3.Row] = (),
    ) -> dict[str, Any]:
        return {
            "trace_id": row["trace_id"],
            "created_at": row["created_at"],
            "finalized_at": row["finalized_at"],
            "session_id": row["session_id"],
            "user_message_id": row["user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "reference_date": row["reference_date"],
            "mode": row["mode"],
            "status": row["status"],
            "query_digest": row["query_digest"],
            "pipeline_version": row["pipeline_version"],
            "index_version": row["index_version"],
            "projection_version": row["projection_version"],
            "metrics": _decode_json(row["metrics_json"], {}),
            "routing": _decode_json(row["routing_json"], {}),
            "candidates": _decode_json(row["candidates_json"], []),
            "evidence": _decode_json(row["evidence_json"], {}),
            "expected_projection_text": row["expected_projection_text"],
            "actual_sections": _decode_json(row["actual_sections_json"], []),
            "actual_injected_text": row["actual_injected_text"],
            "feedback": [dict(item) for item in feedback_rows],
            "active_searches": [
                {
                    "attempt_id": item["attempt_id"],
                    "ordinal": item["ordinal"],
                    "created_at": item["created_at"],
                    "query_digest": item["query_digest"],
                    "include_source_detail": bool(item["include_source_detail"]),
                    "requested_limit": item["requested_limit"],
                    "reused": bool(item["reused"]),
                    "status": item["status"],
                    "metrics": _decode_json(item["metrics_json"], {}),
                    "projection_text": item["projection_text"],
                }
                for item in search_rows
            ],
            "message_bindings": [dict(item) for item in binding_rows],
        }


def collect_memory_sections(
    sections: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Extract exact memory-only Builder sections in their existing order."""

    collected: list[dict[str, Any]] = []
    text_blocks: list[str] = []
    skipped_statuses = {
        "disabled",
        "gated",
        "empty",
        "error",
        "shadow_observed",
        "shadow_preview",
        "not_observed",
        "not_configured",
        "warming",
    }
    for name, raw in sections.items():
        if name not in MEMORY_SECTION_NAMES or not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "")
        text = str(raw.get("text") or "")
        if status in skipped_statuses or not text.strip():
            continue
        item = {
            "name": name,
            "status": status or "ok",
            "text": text,
            "chars": len(text),
            "tokens": int(raw.get("tokens") or 0),
        }
        collected.append(item)
        text_blocks.append(f"[{name}]\n{text}")
    return collected, "\n\n".join(text_blocks)


__all__ = [
    "MEMORY_SECTION_NAMES",
    "MemoryRecallTraceStore",
    "TRACE_FEEDBACK_LABELS",
    "TRACE_INDEX_VERSION",
    "TRACE_PIPELINE_VERSION",
    "TRACE_PROJECTION_VERSION",
    "collect_memory_sections",
    "default_trace_db_path",
    "get_default_trace_store",
    "query_digest",
    "trace_enabled",
]
