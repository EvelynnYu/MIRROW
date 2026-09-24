"""Transactional storage boundary for source-backed Memory V2 events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .models import (
    BatchSourceRef,
    BoundaryLinkJudgmentDraft,
    DaySettlementCandidateDraft,
    DaySettlementDraft,
    DayThreadJudgmentDraft,
    EncodingBatch,
    EventDraft,
    EventLinkDraft,
    SourceRef,
    digest_batch_sources,
)
from .day_settlement import MAX_DAY_COMPACT_SUMMARY_CHARS
from .day_settlement_lineage import (
    current_day_source_digest,
    effective_event_thread_status_sql,
)
from .periods import (
    PeriodGenerationJobDraft,
    PeriodJobState,
    PeriodSummaryDraft,
    PeriodSummaryItemDraft,
    period_input_digest,
)
from .schema import DDL, SCHEMA_VERSION


class ImmutableConflictError(RuntimeError):
    """A stable identity was reused with different immutable content."""


class SourceBoundaryError(ValueError):
    """An event source is missing or falls outside its declared batch."""


class ThreadConflictError(RuntimeError):
    """A continuation would fork, prepend, or merge incompatible event threads."""


SourceValidator = Callable[[Sequence[SourceRef]], bool]
BatchSourceValidator = Callable[[Sequence[BatchSourceRef]], bool]
_SOURCE_KINDS = frozenset({"chat", "wander", "sentinel", "reminder"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_instant(value: str) -> datetime:
    """Parse authority timestamps for boundary ordering.

    Historical SQLite rows mix explicit UTC offsets, ``Z`` and older local
    timestamps.  Naive values use MIRROW's documented Asia/Shanghai authority
    convention; row IDs remain identities, never chronology.
    """

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SourceBoundaryError("boundary source timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    body = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"


class MemoryV2Store:
    """Small, synchronous SQLite store intended for background workers.

    The class does not know how to read ``conversation_messages``.  Production
    integration must verify the batch manifest and event citations against
    that authority before committing an encoding result.
    """

    def __init__(self, db_path: str | Path, *, initialise: bool = True):
        self.db_path = str(db_path)
        if initialise:
            self.initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialise(self) -> None:
        now = _now_iso()
        connection = self._connect()
        try:
            schema_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='memory_schema'"
            ).fetchone()
            row = None
            if schema_exists is not None:
                row = connection.execute(
                    "SELECT schema_version FROM memory_schema "
                    "WHERE schema_key='memory_v2'"
                ).fetchone()
                if row is not None and int(row["schema_version"]) not in {
                    4,
                    5,
                    6,
                    7,
                    8,
                    9,
                    SCHEMA_VERSION,
                }:
                    raise RuntimeError(
                        f"unsupported Memory V2 schema: {row['schema_version']} "
                        f"(expected 4, 5, 6, 7, 8, 9, or {SCHEMA_VERSION})"
                    )
            connection.executescript("BEGIN IMMEDIATE;\n" + DDL)
            if row is None:
                connection.execute(
                    "INSERT INTO memory_schema "
                    "(schema_key, schema_version, created_at, upgraded_at) "
                    "VALUES ('memory_v2', ?, ?, ?)",
                    (SCHEMA_VERSION, now, now),
                )
            elif int(row["schema_version"]) < SCHEMA_VERSION:
                connection.execute(
                    "UPDATE memory_schema SET schema_version=?, upgraded_at=? "
                    "WHERE schema_key='memory_v2'",
                    (SCHEMA_VERSION, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def resolve_batch_id(batch: EncodingBatch) -> str:
        return batch.batch_id or _stable_id(
            "batch",
            batch.source_namespace,
            batch.session_id,
            batch.from_message_row_id,
            batch.to_message_row_id,
            batch.encoder_version,
            batch.prompt_version,
        )

    @staticmethod
    def resolve_event_id(batch_id: str, event: EventDraft) -> str:
        return event.event_id or _stable_id("event", batch_id, event.ordinal)

    def commit_encoding(
        self,
        batch: EncodingBatch,
        events: Iterable[EventDraft],
        *,
        batch_sources: Sequence[BatchSourceRef],
        source_validator: SourceValidator | None = None,
        batch_source_validator: BatchSourceValidator | None = None,
    ) -> list[str]:
        """Atomically persist one batch and its immutable events.

        Repeating the exact commit is a no-op.  Reusing a batch/event identity
        with different content raises instead of silently rewriting memory.
        """

        event_list = list(events)
        self._validate_batch(batch)
        self._validate_batch_sources(batch, batch_sources, batch_source_validator)
        self._validate_events(batch, batch_sources, event_list, source_validator)
        batch_id = self.resolve_batch_id(batch)
        now = _now_iso()
        event_ids: list[str] = []

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch_completed = self._insert_or_verify_batch(
                connection, batch_id, batch, len(event_list), now
            )
            self._insert_or_verify_batch_sources(
                connection,
                batch_id,
                batch_sources,
                allow_insert=not batch_completed,
            )
            for event in event_list:
                event_id = self.resolve_event_id(batch_id, event)
                self._insert_or_verify_event(
                    connection,
                    batch_id,
                    event_id,
                    batch.encoder_version,
                    event,
                    now,
                    allow_insert=not batch_completed,
                )
                self._insert_or_verify_sources(
                    connection,
                    event_id,
                    event.sources,
                    allow_insert=not batch_completed,
                )
                self._insert_or_verify_participants(
                    connection,
                    event_id,
                    event.subject_id,
                    event.participant_ids,
                    allow_insert=not batch_completed,
                )
                event_ids.append(event_id)
            if not batch_completed:
                connection.execute(
                    "UPDATE encoding_batches SET status='completed', error_code='', "
                    "event_count=?, completed_at=? WHERE id=?",
                    (len(event_list), now, batch_id),
                )
            connection.commit()
            return event_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_event(self, event_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["attributes"] = json.loads(result.pop("attributes_json"))
            result["sources"] = [
                dict(source)
                for source in connection.execute(
                    "SELECT * FROM event_sources WHERE event_id=? ORDER BY source_order",
                    (event_id,),
                ).fetchall()
            ]
            result["participant_ids"] = [
                participant["participant_id"]
                for participant in connection.execute(
                    "SELECT participant_id FROM event_participants "
                    "WHERE event_id=? ORDER BY participant_order",
                    (event_id,),
                ).fetchall()
            ]
            return result
        finally:
            connection.close()

    def append_event_status(
        self,
        event_id: str,
        status: str,
        *,
        reason_code: str,
        source_revision: str,
        replacement_event_id: str = "",
    ) -> str:
        """Append one idempotent event correction without rewriting history."""

        event_id = str(event_id or "").strip()
        status = str(status or "").strip()
        reason_code = str(reason_code or "").strip()
        source_revision = str(source_revision or "").strip()
        replacement_event_id = str(replacement_event_id or "").strip()
        self._validate_event_status_values(
            event_id,
            status,
            reason_code=reason_code,
            source_revision=source_revision,
            replacement_event_id=replacement_event_id,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            status_id = self._append_event_status_in_transaction(
                connection,
                event_id,
                status,
                reason_code=reason_code,
                source_revision=source_revision,
                replacement_event_id=replacement_event_id,
            )
            connection.commit()
            return status_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def invalidate_events_by_source_message_ids(
        self,
        message_ids: Sequence[str],
        *,
        reason_code: str,
        source_revision: str,
        surviving_message_ids: Sequence[str] = (),
    ) -> dict[str, object]:
        """Invalidate every currently-active event citing mutated messages.

        The event rows and their citations remain immutable.  The latest
        append-only status becomes the effective truth boundary used by recall
        and later settlement inputs.  Replaying the same mutation is a no-op.

        A historical duplicate persistence bug can leave one event citing two
        source rows that are identical apart from row and message identities.
        For deletion-like mutations, when the authority confirms that an exact
        cited twin still exists, the event remains valid: removing one redundant
        anchor did not remove any evidence.  Edits still require rebuilding so
        the changed fact can be learned.  Similar text at another time is
        deliberately not treated as a replacement source.
        """

        normalized_ids = tuple(
            dict.fromkeys(
                str(message_id).strip()
                for message_id in message_ids
                if str(message_id).strip()
            )
        )
        if not normalized_ids:
            return {
                "event_ids": (),
                "active_dates": (),
                "status_ids": (),
                "preserved_event_ids": (),
            }
        if len(normalized_ids) > 20_000:
            raise ValueError("source invalidation message set is too large")
        surviving_ids = tuple(
            dict.fromkeys(
                str(message_id).strip()
                for message_id in surviving_message_ids
                if str(message_id).strip()
                and str(message_id).strip() not in normalized_ids
            )
        )
        if len(surviving_ids) > 100_000:
            raise ValueError("surviving source message set is too large")
        reason_code = str(reason_code or "").strip()
        source_revision = str(source_revision or "").strip()
        # Validate shared fields before taking the write lock.  The event ID is
        # checked per selected row below.
        self._validate_event_status_values(
            "event-placeholder",
            "invalid_source",
            reason_code=reason_code,
            source_revision=source_revision,
            replacement_event_id="",
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TEMP TABLE memory_v2_mutated_sources ("
                "message_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO memory_v2_mutated_sources(message_id) VALUES (?)",
                ((message_id,) for message_id in normalized_ids),
            )
            affected_rows = connection.execute(
                "SELECT DISTINCT e.id, e.active_date FROM events e "
                "JOIN event_sources source ON source.event_id=e.id "
                "JOIN memory_v2_mutated_sources mutated "
                "ON mutated.message_id=source.message_id "
                "WHERE COALESCE((SELECT status FROM event_status_log status_log "
                "WHERE status_log.event_id=e.id "
                "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1), "
                "'active')='active' ORDER BY e.active_date, e.id"
            ).fetchall()
            preserved_event_ids: set[str] = set()
            if (
                affected_rows
                and surviving_ids
                and reason_code
                in {
                    "source_message_deleted",
                    "source_message_revoked",
                    "source_session_cleared",
                }
            ):
                connection.execute(
                    "CREATE TEMP TABLE memory_v2_affected_events ("
                    "event_id TEXT PRIMARY KEY) WITHOUT ROWID"
                )
                connection.executemany(
                    "INSERT INTO memory_v2_affected_events(event_id) VALUES (?)",
                    ((str(row["id"]),) for row in affected_rows),
                )
                source_rows = connection.execute(
                    "SELECT source.event_id, source.message_id, "
                    "source.span_start, source.span_end, source.span_digest, "
                    "manifest.session_id, manifest.active_date, "
                    "manifest.calendar_date, manifest.source_ts, "
                    "manifest.source_role, manifest.source_kind, "
                    "manifest.source_event_type, manifest.content_digest "
                    "FROM event_sources source "
                    "JOIN memory_v2_affected_events affected "
                    "ON affected.event_id=source.event_id "
                    "JOIN events event ON event.id=source.event_id "
                    "JOIN encoding_batch_sources manifest "
                    "ON manifest.batch_id=event.batch_id "
                    "AND manifest.message_id=source.message_id "
                    "ORDER BY source.event_id, source.source_order"
                ).fetchall()
                sources_by_event: dict[str, list[sqlite3.Row]] = {}
                for source_row in source_rows:
                    sources_by_event.setdefault(
                        str(source_row["event_id"]), []
                    ).append(source_row)

                mutated_set = set(normalized_ids)
                surviving_set = set(surviving_ids)

                def source_fingerprint(row: sqlite3.Row) -> tuple[object, ...]:
                    return (
                        row["session_id"],
                        row["active_date"],
                        row["calendar_date"],
                        row["source_ts"],
                        row["source_role"],
                        row["source_kind"],
                        row["source_event_type"],
                        row["content_digest"],
                        row["span_start"],
                        row["span_end"],
                        row["span_digest"],
                    )

                for event_id, event_sources in sources_by_event.items():
                    mutated_sources = [
                        row
                        for row in event_sources
                        if str(row["message_id"]) in mutated_set
                    ]
                    live_sources = [
                        row
                        for row in event_sources
                        if str(row["message_id"]) in surviving_set
                    ]
                    if mutated_sources and all(
                        any(
                            source_fingerprint(candidate)
                            == source_fingerprint(mutated_source)
                            for candidate in live_sources
                        )
                        for mutated_source in mutated_sources
                    ):
                        preserved_event_ids.add(event_id)

            rows = [
                row
                for row in affected_rows
                if str(row["id"]) not in preserved_event_ids
            ]
            status_ids = []
            for row in rows:
                status_ids.append(
                    self._append_event_status_in_transaction(
                        connection,
                        str(row["id"]),
                        "invalid_source",
                        reason_code=reason_code,
                        source_revision=source_revision,
                        replacement_event_id="",
                    )
                )
            connection.commit()
            return {
                "event_ids": tuple(str(row["id"]) for row in rows),
                "active_dates": tuple(
                    dict.fromkeys(
                        str(row["active_date"])
                        for row in rows
                        if str(row["active_date"] or "").strip()
                    )
                ),
                "status_ids": tuple(status_ids),
                "preserved_event_ids": tuple(
                    str(row["id"])
                    for row in affected_rows
                    if str(row["id"]) in preserved_event_ids
                ),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_pending_source_rebuild_dates(
        self,
        *,
        source_namespace: str | None = None,
    ) -> tuple[str, ...]:
        """Return days whose latest event correction still needs rebuilding."""

        params: tuple[str, ...] = ()
        namespace_sql = ""
        if source_namespace is not None:
            namespace_sql = "AND batch.source_namespace=? "
            params = (str(source_namespace),)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT DISTINCT event.active_date FROM events event "
                "JOIN encoding_batches batch ON batch.id=event.batch_id "
                "WHERE COALESCE((SELECT status FROM event_status_log status_log "
                "WHERE status_log.event_id=event.id "
                "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1), "
                "'active')='invalid_source' "
                + namespace_sql
                + "ORDER BY event.active_date",
                params,
            ).fetchall()
            return tuple(str(row["active_date"]) for row in rows)
        finally:
            connection.close()

    @staticmethod
    def _validate_event_status_values(
        event_id: str,
        status: str,
        *,
        reason_code: str,
        source_revision: str,
        replacement_event_id: str,
    ) -> None:
        if not event_id or status not in {
            "active",
            "superseded",
            "invalid_source",
            "retracted",
        }:
            raise ValueError("event status is invalid")
        if not reason_code or len(reason_code) > 120:
            raise ValueError("event status reason_code is invalid")
        if not source_revision or len(source_revision) > 240:
            raise ValueError("event status source_revision is invalid")
        if status == "superseded" and not replacement_event_id:
            raise ValueError("superseded event needs a replacement")
        if status != "superseded" and replacement_event_id:
            raise ValueError("replacement is only valid for superseded events")
        if replacement_event_id == event_id:
            raise ValueError("event cannot replace itself")

    def _append_event_status_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        status: str,
        *,
        reason_code: str,
        source_revision: str,
        replacement_event_id: str,
    ) -> str:
        self._validate_event_status_values(
            event_id,
            status,
            reason_code=reason_code,
            source_revision=source_revision,
            replacement_event_id=replacement_event_id,
        )
        if connection.execute(
            "SELECT 1 FROM events WHERE id=?", (event_id,)
        ).fetchone() is None:
            raise SourceBoundaryError("event status target is missing")
        if replacement_event_id and connection.execute(
            "SELECT 1 FROM events WHERE id=?", (replacement_event_id,)
        ).fetchone() is None:
            raise SourceBoundaryError("replacement event is missing")

        status_id = _stable_id("event_status", event_id, source_revision)
        existing = connection.execute(
            "SELECT * FROM event_status_log WHERE id=?", (status_id,)
        ).fetchone()
        expected = (
            event_id,
            status,
            reason_code,
            replacement_event_id,
            source_revision,
        )
        if existing is None:
            connection.execute(
                "INSERT INTO event_status_log "
                "(id, event_id, status, reason_code, replacement_event_id, "
                "source_revision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    status_id,
                    event_id,
                    status,
                    reason_code,
                    replacement_event_id or None,
                    source_revision,
                    _now_iso(),
                ),
            )
        elif (
            str(existing["event_id"]),
            str(existing["status"]),
            str(existing["reason_code"]),
            str(existing["replacement_event_id"] or ""),
            str(existing["source_revision"]),
        ) != expected:
            raise ImmutableConflictError(
                "event status source revision already has different content"
            )
        return status_id

    def count_events(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            connection.close()

    @staticmethod
    def resolve_period_generation_job_id(draft: PeriodGenerationJobDraft) -> str:
        return draft.job_id or _stable_id(
            "period_job",
            draft.namespace,
            draft.period_kind,
            draft.date_basis,
            draft.reference_date,
            draft.generator_version,
            draft.prompt_version,
            draft.plan_digest,
        )

    def commit_period_generation_job(self, draft: PeriodGenerationJobDraft) -> str:
        """Persist an immutable safe plan receipt before any provider call."""

        job_id = self.resolve_period_generation_job_id(draft)
        now = _now_iso()
        estimated_calls = sum(
            int(candidate.model_call_required) for candidate in draft.candidates
        )
        expected = {
            "id": job_id,
            "namespace": draft.namespace,
            "period_kind": draft.period_kind,
            "date_basis": draft.date_basis,
            "reference_date": draft.reference_date,
            "generator_version": draft.generator_version,
            "prompt_version": draft.prompt_version,
            "plan_digest": draft.plan_digest,
            "candidate_count": len(draft.candidates),
            "estimated_model_calls": estimated_calls,
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM period_generation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise ImmutableConflictError(
                        "period generation job identity was reused with different content"
                    )
                stored_candidates = connection.execute(
                    "SELECT * FROM period_generation_job_candidates WHERE job_id=? "
                    "ORDER BY candidate_order",
                    (job_id,),
                ).fetchall()
                if len(stored_candidates) != len(draft.candidates) or any(
                    (
                        row["candidate_order"],
                        row["period_key"],
                        row["revision"],
                        row["input_digest"],
                        row["input_count"],
                        bool(row["model_call_required"]),
                    )
                    != (
                        candidate.candidate_order,
                        candidate.period_key,
                        candidate.revision,
                        candidate.input_digest,
                        candidate.input_count,
                        candidate.model_call_required,
                    )
                    for row, candidate in zip(stored_candidates, draft.candidates)
                ):
                    raise ImmutableConflictError(
                        "period generation candidate receipt conflict"
                    )
                connection.commit()
                return job_id

            connection.execute(
                "INSERT INTO period_generation_jobs "
                "(id, namespace, period_kind, date_basis, reference_date, "
                "generator_version, prompt_version, plan_digest, candidate_count, "
                "estimated_model_calls, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*expected.values(), now),
            )
            for candidate in draft.candidates:
                connection.execute(
                    "INSERT INTO period_generation_job_candidates "
                    "(job_id, candidate_order, period_key, revision, input_digest, "
                    "input_count, model_call_required) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        candidate.candidate_order,
                        candidate.period_key,
                        candidate.revision,
                        candidate.input_digest,
                        candidate.input_count,
                        int(candidate.model_call_required),
                    ),
                )
            transition_id = _stable_id("period_job_transition", job_id, 0, "planned")
            connection.execute(
                "INSERT INTO period_generation_job_transitions "
                "(id, job_id, transition_order, state, completed_candidate_count, "
                "error_code, created_at) VALUES (?, ?, 0, 'planned', 0, '', ?)",
                (transition_id, job_id, now),
            )
            connection.commit()
            return job_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_period_generation_transition(
        self,
        job_id: str,
        state: PeriodJobState,
        *,
        error_code: str = "",
    ) -> str:
        """Append operational state; never rewrite the plan or prior attempts."""

        if state not in {"planned", "running", "completed", "error"}:
            raise ValueError("invalid period generation job state")
        clean_error = str(error_code or "").strip()
        if state == "error" and not clean_error:
            raise ValueError("error transition requires error_code")
        if state != "error" and clean_error:
            raise ValueError("error_code is only valid for error transitions")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT candidate_count FROM period_generation_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise SourceBoundaryError("period generation job is missing")
            completed_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM period_generation_job_outputs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
            )
            if state == "completed" and completed_count != int(job["candidate_count"]):
                raise SourceBoundaryError(
                    "period generation job cannot complete with missing outputs"
                )
            ordinal = int(
                connection.execute(
                    "SELECT COUNT(*) FROM period_generation_job_transitions WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
            )
            transition_id = _stable_id(
                "period_job_transition", job_id, ordinal, state, clean_error
            )
            connection.execute(
                "INSERT INTO period_generation_job_transitions "
                "(id, job_id, transition_order, state, completed_candidate_count, "
                "error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    transition_id,
                    job_id,
                    ordinal,
                    state,
                    completed_count,
                    clean_error,
                    _now_iso(),
                ),
            )
            connection.commit()
            return transition_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_period_generation_output(
        self, job_id: str, candidate_order: int, period_summary_id: str
    ) -> None:
        """Link one atomic period summary to its restartable job candidate."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                "SELECT * FROM period_generation_job_candidates "
                "WHERE job_id=? AND candidate_order=?",
                (job_id, candidate_order),
            ).fetchone()
            summary = connection.execute(
                "SELECT * FROM period_summary_versions WHERE id=?",
                (period_summary_id,),
            ).fetchone()
            if candidate is None or summary is None:
                raise SourceBoundaryError(
                    "period job candidate or summary output is missing"
                )
            job = connection.execute(
                "SELECT * FROM period_generation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                summary["namespace"] != job["namespace"]
                or summary["period_kind"] != job["period_kind"]
                or summary["date_basis"] != job["date_basis"]
                or summary["period_key"] != candidate["period_key"]
                or summary["revision"] != candidate["revision"]
                or summary["input_digest"] != candidate["input_digest"]
            ):
                raise SourceBoundaryError(
                    "period summary output does not match its planned candidate"
                )
            existing = connection.execute(
                "SELECT period_summary_id FROM period_generation_job_outputs "
                "WHERE job_id=? AND candidate_order=?",
                (job_id, candidate_order),
            ).fetchone()
            if existing is not None:
                if existing["period_summary_id"] != period_summary_id:
                    raise ImmutableConflictError(
                        "period generation output identity conflict"
                    )
                connection.commit()
                return
            connection.execute(
                "INSERT INTO period_generation_job_outputs "
                "(job_id, candidate_order, period_summary_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, candidate_order, period_summary_id, _now_iso()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_period_generation_candidate(
        self,
        job_id: str,
        candidate_order: int,
        draft: PeriodSummaryDraft,
    ) -> str:
        """Atomically append a period summary and attach it to its job candidate."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM period_generation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM period_generation_job_candidates "
                "WHERE job_id=? AND candidate_order=?",
                (job_id, candidate_order),
            ).fetchone()
            if job is None or candidate is None:
                raise SourceBoundaryError("period job candidate is missing")
            input_digest = period_input_digest(
                event_ids=draft.input_event_ids,
                parent_summary_ids=draft.input_parent_summary_ids,
            )
            input_count = len(draft.input_event_ids) + len(
                draft.input_parent_summary_ids
            )
            if (
                draft.namespace != job["namespace"]
                or draft.period_kind != job["period_kind"]
                or draft.date_basis != job["date_basis"]
                or draft.generator_version != job["generator_version"]
                or draft.prompt_version != job["prompt_version"]
                or draft.period_key != candidate["period_key"]
                or draft.revision != candidate["revision"]
                or input_digest != candidate["input_digest"]
                or input_count != candidate["input_count"]
            ):
                raise SourceBoundaryError(
                    "period summary draft does not match its planned candidate"
                )
            expected_summary_id = self.resolve_period_summary_id(draft)
            existing = connection.execute(
                "SELECT period_summary_id FROM period_generation_job_outputs "
                "WHERE job_id=? AND candidate_order=?",
                (job_id, candidate_order),
            ).fetchone()
            if existing is not None:
                if existing["period_summary_id"] != expected_summary_id:
                    raise ImmutableConflictError(
                        "period generation output identity conflict"
                    )
                connection.commit()
                return expected_summary_id
            summary_id = self.commit_period_summary(draft, _connection=connection)
            connection.execute(
                "INSERT INTO period_generation_job_outputs "
                "(job_id, candidate_order, period_summary_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, candidate_order, summary_id, _now_iso()),
            )
            connection.commit()
            return summary_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_period_generation_job(self, job_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM period_generation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["candidates"] = [
                dict(candidate)
                for candidate in connection.execute(
                    "SELECT * FROM period_generation_job_candidates WHERE job_id=? "
                    "ORDER BY candidate_order",
                    (job_id,),
                ).fetchall()
            ]
            result["transitions"] = [
                dict(transition)
                for transition in connection.execute(
                    "SELECT * FROM period_generation_job_transitions WHERE job_id=? "
                    "ORDER BY transition_order",
                    (job_id,),
                ).fetchall()
            ]
            result["outputs"] = [
                dict(output)
                for output in connection.execute(
                    "SELECT * FROM period_generation_job_outputs WHERE job_id=? "
                    "ORDER BY candidate_order",
                    (job_id,),
                ).fetchall()
            ]
            return result
        finally:
            connection.close()

    @staticmethod
    def resolve_period_summary_id(draft: PeriodSummaryDraft) -> str:
        return draft.summary_id or _stable_id(
            "period",
            draft.namespace,
            draft.period_kind,
            draft.date_basis,
            draft.period_key,
            draft.revision,
        )

    @staticmethod
    def resolve_period_item_id(summary_id: str, item: PeriodSummaryItemDraft) -> str:
        return _stable_id("period_item", summary_id, item.ordinal)

    def commit_period_summary(
        self,
        draft: PeriodSummaryDraft,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        """Atomically append one rebuildable period view and its exact lineage."""

        summary_id = self.resolve_period_summary_id(draft)
        now = _now_iso()
        item_payloads = [
            {
                "ordinal": item.ordinal,
                "item_kind": item.item_kind,
                "summary": item.summary,
                "importance": float(item.importance),
                "confidence": float(item.confidence),
                "source_event_ids": list(item.source_event_ids),
                "source_item_ids": list(item.source_item_ids),
                "attributes": dict(item.attributes),
            }
            for item in draft.items
        ]
        input_digest = period_input_digest(
            event_ids=draft.input_event_ids,
            parent_summary_ids=draft.input_parent_summary_ids,
        )
        output_digest = _digest(item_payloads)
        expected = {
            "id": summary_id,
            "namespace": draft.namespace,
            "period_kind": draft.period_kind,
            "date_basis": draft.date_basis,
            "period_key": draft.period_key,
            "revision": draft.revision,
            "date_from": draft.date_from,
            "date_to": draft.date_to,
            "generator_version": draft.generator_version,
            "prompt_version": draft.prompt_version,
            "input_digest": input_digest,
            "output_digest": output_digest,
            "item_count": len(draft.items),
        }

        owns_connection = _connection is None
        connection = _connection or self._connect()
        try:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM period_summary_versions WHERE id=?",
                (summary_id,),
            ).fetchone()
            keyed = connection.execute(
                "SELECT * FROM period_summary_versions "
                "WHERE namespace=? AND period_kind=? AND date_basis=? "
                "AND period_key=? AND revision=?",
                (
                    draft.namespace,
                    draft.period_kind,
                    draft.date_basis,
                    draft.period_key,
                    draft.revision,
                ),
            ).fetchone()
            if existing is not None or keyed is not None:
                row = existing or keyed
                if any(row[key] != value for key, value in expected.items()):
                    raise ImmutableConflictError(
                        "period summary identity was reused with different content"
                    )
                if owns_connection:
                    connection.commit()
                return str(row["id"])

            latest = connection.execute(
                "SELECT MAX(revision) AS revision FROM period_summary_versions "
                "WHERE namespace=? AND period_kind=? AND date_basis=? AND period_key=?",
                (draft.namespace, draft.period_kind, draft.date_basis, draft.period_key),
            ).fetchone()
            expected_revision = int(latest["revision"] or 0) + 1
            if draft.revision != expected_revision:
                raise ImmutableConflictError(
                    f"period summary revision must append {expected_revision}"
                )
            self._validate_period_inputs(connection, draft)

            connection.execute(
                "INSERT INTO period_summary_versions "
                "(id, namespace, period_kind, date_basis, period_key, revision, date_from, date_to, "
                "generator_version, prompt_version, input_digest, output_digest, "
                "item_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id,
                    draft.namespace,
                    draft.period_kind,
                    draft.date_basis,
                    draft.period_key,
                    draft.revision,
                    draft.date_from,
                    draft.date_to,
                    draft.generator_version,
                    draft.prompt_version,
                    input_digest,
                    output_digest,
                    len(draft.items),
                    now,
                ),
            )
            for order, event_id in enumerate(draft.input_event_ids):
                connection.execute(
                    "INSERT INTO period_summary_event_inputs "
                    "(period_summary_id, event_id, input_order) VALUES (?, ?, ?)",
                    (summary_id, event_id, order),
                )
            for order, parent_id in enumerate(draft.input_parent_summary_ids):
                connection.execute(
                    "INSERT INTO period_summary_parent_inputs "
                    "(period_summary_id, parent_summary_id, input_order) VALUES (?, ?, ?)",
                    (summary_id, parent_id, order),
                )
            for item in draft.items:
                item_id = self.resolve_period_item_id(summary_id, item)
                attributes_json = _canonical_json(dict(item.attributes))
                content_digest = _digest(
                    {
                        "item_kind": item.item_kind,
                        "summary": item.summary,
                        "importance": float(item.importance),
                        "confidence": float(item.confidence),
                        "attributes": dict(item.attributes),
                    }
                )
                connection.execute(
                    "INSERT INTO period_summary_items "
                    "(id, period_summary_id, item_order, item_kind, summary, importance, "
                    "confidence, attributes_json, content_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        summary_id,
                        item.ordinal,
                        item.item_kind,
                        item.summary,
                        float(item.importance),
                        float(item.confidence),
                        attributes_json,
                        content_digest,
                        now,
                    ),
                )
                for order, event_id in enumerate(item.source_event_ids):
                    connection.execute(
                        "INSERT INTO period_item_event_sources "
                        "(period_item_id, event_id, source_order) VALUES (?, ?, ?)",
                        (item_id, event_id, order),
                    )
                for order, parent_item_id in enumerate(item.source_item_ids):
                    connection.execute(
                        "INSERT INTO period_item_parent_sources "
                        "(period_item_id, parent_item_id, source_order) VALUES (?, ?, ?)",
                        (item_id, parent_item_id, order),
                    )
            if owns_connection:
                connection.commit()
            return summary_id
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()

    def get_period_summary(self, summary_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM period_summary_versions WHERE id=?", (summary_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["input_event_ids"] = [
                item["event_id"]
                for item in connection.execute(
                    "SELECT event_id FROM period_summary_event_inputs "
                    "WHERE period_summary_id=? ORDER BY input_order",
                    (summary_id,),
                ).fetchall()
            ]
            result["input_parent_summary_ids"] = [
                item["parent_summary_id"]
                for item in connection.execute(
                    "SELECT parent_summary_id FROM period_summary_parent_inputs "
                    "WHERE period_summary_id=? ORDER BY input_order",
                    (summary_id,),
                ).fetchall()
            ]
            result["items"] = []
            for item in connection.execute(
                "SELECT * FROM period_summary_items WHERE period_summary_id=? "
                "ORDER BY item_order",
                (summary_id,),
            ).fetchall():
                payload = dict(item)
                payload["attributes"] = json.loads(payload.pop("attributes_json"))
                payload["source_event_ids"] = [
                    source["event_id"]
                    for source in connection.execute(
                        "SELECT event_id FROM period_item_event_sources "
                        "WHERE period_item_id=? ORDER BY source_order",
                        (item["id"],),
                    ).fetchall()
                ]
                payload["source_item_ids"] = [
                    source["parent_item_id"]
                    for source in connection.execute(
                        "SELECT parent_item_id FROM period_item_parent_sources "
                        "WHERE period_item_id=? ORDER BY source_order",
                        (item["id"],),
                    ).fetchall()
                ]
                result["items"].append(payload)
            return result
        finally:
            connection.close()

    def resolve_period_source_message_ids(
        self, summary_id: str, *, max_nodes: int = 10_000
    ) -> list[str]:
        """Follow a period view back to original event source messages."""

        if max_nodes <= 0:
            raise ValueError("max_nodes must be positive")
        pending = [str(summary_id)]
        visited: set[str] = set()
        event_ids: list[str] = []
        event_seen: set[str] = set()
        connection = self._connect()
        try:
            root = connection.execute(
                "SELECT 1 FROM period_summary_versions WHERE id=?",
                (str(summary_id),),
            ).fetchone()
            if root is None:
                raise SourceBoundaryError("period summary is missing")
            while pending and len(visited) < max_nodes:
                current = pending.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                for row in connection.execute(
                    "SELECT event_id FROM period_summary_event_inputs "
                    "WHERE period_summary_id=? ORDER BY input_order",
                    (current,),
                ).fetchall():
                    event_id = str(row["event_id"])
                    if event_id not in event_seen:
                        event_seen.add(event_id)
                        event_ids.append(event_id)
                pending.extend(
                    str(row["parent_summary_id"])
                    for row in connection.execute(
                        "SELECT parent_summary_id FROM period_summary_parent_inputs "
                        "WHERE period_summary_id=? ORDER BY input_order",
                        (current,),
                    ).fetchall()
                )
            if pending:
                raise RuntimeError("period lineage exceeds max_nodes")
            message_ids: list[str] = []
            seen_messages: set[str] = set()
            for event_id in event_ids:
                for row in connection.execute(
                    "SELECT message_id FROM event_sources WHERE event_id=? "
                    "ORDER BY source_order",
                    (event_id,),
                ).fetchall():
                    message_id = str(row["message_id"])
                    if message_id not in seen_messages:
                        seen_messages.add(message_id)
                        message_ids.append(message_id)
            return message_ids
        finally:
            connection.close()

    def resolve_period_item_source_message_ids(
        self, item_id: str, *, max_nodes: int = 10_000
    ) -> list[str]:
        """Resolve one compact period item through parent items to raw messages."""

        if max_nodes <= 0:
            raise ValueError("max_nodes must be positive")
        pending = [str(item_id)]
        visited: set[str] = set()
        event_ids: list[str] = []
        event_seen: set[str] = set()
        connection = self._connect()
        try:
            root = connection.execute(
                "SELECT 1 FROM period_summary_items WHERE id=?", (str(item_id),)
            ).fetchone()
            if root is None:
                raise SourceBoundaryError("period summary item is missing")
            while pending and len(visited) < max_nodes:
                current = pending.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                for row in connection.execute(
                    "SELECT event_id FROM period_item_event_sources "
                    "WHERE period_item_id=? ORDER BY source_order",
                    (current,),
                ).fetchall():
                    event_id = str(row["event_id"])
                    if event_id not in event_seen:
                        event_seen.add(event_id)
                        event_ids.append(event_id)
                pending.extend(
                    str(row["parent_item_id"])
                    for row in connection.execute(
                        "SELECT parent_item_id FROM period_item_parent_sources "
                        "WHERE period_item_id=? ORDER BY source_order",
                        (current,),
                    ).fetchall()
                )
            if pending:
                raise RuntimeError("period item lineage exceeds max_nodes")
            message_ids: list[str] = []
            seen_messages: set[str] = set()
            for event_id in event_ids:
                for row in connection.execute(
                    "SELECT message_id FROM event_sources WHERE event_id=? "
                    "ORDER BY source_order",
                    (event_id,),
                ).fetchall():
                    message_id = str(row["message_id"])
                    if message_id not in seen_messages:
                        seen_messages.add(message_id)
                        message_ids.append(message_id)
            return message_ids
        finally:
            connection.close()

    @staticmethod
    def _validate_period_inputs(
        connection: sqlite3.Connection,
        draft: PeriodSummaryDraft,
    ) -> None:
        if draft.period_kind == "day":
            placeholders = ",".join("?" for _ in draft.input_event_ids)
            rows = connection.execute(
                "SELECT e.id, e.active_date, e.calendar_date, b.source_namespace, "
                "COALESCE((SELECT status FROM event_status_log l WHERE l.event_id=e.id "
                "ORDER BY l.created_at DESC, l.id DESC LIMIT 1), 'active') AS status "
                "FROM events e JOIN encoding_batches b ON b.id=e.batch_id "
                f"WHERE e.id IN ({placeholders})",
                draft.input_event_ids,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if len(by_id) != len(draft.input_event_ids):
                raise SourceBoundaryError("period summary event input is missing")
            for event_id in draft.input_event_ids:
                row = by_id[event_id]
                event_date = row[draft.date_basis]
                if (
                    row["source_namespace"] != draft.namespace
                    or event_date < draft.date_from
                    or event_date > draft.date_to
                    or row["status"] != "active"
                ):
                    raise SourceBoundaryError(
                        "period summary event input is outside the active period scope"
                    )
            allowed_events = set(draft.input_event_ids)
            if any(
                not set(item.source_event_ids).issubset(allowed_events)
                for item in draft.items
            ):
                raise SourceBoundaryError(
                    "period item event source is absent from period inputs"
                )
            return

        placeholders = ",".join("?" for _ in draft.input_parent_summary_ids)
        rows = connection.execute(
            "SELECT * FROM period_summary_versions "
            f"WHERE id IN ({placeholders})",
            draft.input_parent_summary_ids,
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(draft.input_parent_summary_ids):
            raise SourceBoundaryError("period parent summary input is missing")
        expected_kinds = (
            {"day"} if draft.period_kind == "week" else {"day", "week"}
        )
        parent_ranges: list[tuple[str, str]] = []
        for parent_id in draft.input_parent_summary_ids:
            row = by_id[parent_id]
            latest = connection.execute(
                "SELECT MAX(revision) AS revision FROM period_summary_versions "
                "WHERE namespace=? AND period_kind=? AND date_basis=? AND period_key=?",
                (
                    row["namespace"],
                    row["period_kind"],
                    row["date_basis"],
                    row["period_key"],
                ),
            ).fetchone()
            if (
                row["namespace"] != draft.namespace
                or row["period_kind"] not in expected_kinds
                or row["date_basis"] != draft.date_basis
                or row["date_from"] < draft.date_from
                or row["date_to"] > draft.date_to
                or int(row["revision"]) != int(latest["revision"])
            ):
                raise SourceBoundaryError(
                    "period parent is stale or outside the required lineage scope"
                )
            parent_ranges.append((str(row["date_from"]), str(row["date_to"])))
        ordered_ranges = sorted(parent_ranges)
        if any(
            current[0] <= previous[1]
            for previous, current in zip(ordered_ranges, ordered_ranges[1:])
        ):
            raise SourceBoundaryError("period parent input ranges must not overlap")
        allowed_parents = set(draft.input_parent_summary_ids)
        source_item_ids = tuple(
            source_id for item in draft.items for source_id in item.source_item_ids
        )
        if source_item_ids:
            item_placeholders = ",".join("?" for _ in source_item_ids)
            source_rows = connection.execute(
                "SELECT id, period_summary_id FROM period_summary_items "
                f"WHERE id IN ({item_placeholders})",
                source_item_ids,
            ).fetchall()
            if len({str(row["id"]) for row in source_rows}) != len(set(source_item_ids)):
                raise SourceBoundaryError("period item parent source is missing")
            if any(
                str(row["period_summary_id"]) not in allowed_parents
                for row in source_rows
            ):
                raise SourceBoundaryError(
                    "period item parent source is absent from period inputs"
                )

    def load_completed_events(
        self,
        batch: EncodingBatch,
        *,
        batch_sources: Sequence[BatchSourceRef],
        batch_source_validator: BatchSourceValidator | None = None,
    ) -> tuple[EventDraft, ...] | None:
        """Load an exact completed batch for retry/resume without another model call."""

        self._validate_batch(batch)
        self._validate_batch_sources(batch, batch_sources, batch_source_validator)
        batch_id = self.resolve_batch_id(batch)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM encoding_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] != "completed":
                return None
            expected_batch = {
                "source_namespace": batch.source_namespace,
                "session_id": batch.session_id,
                "active_date": batch.active_date,
                "from_message_row_id": batch.from_message_row_id,
                "to_message_row_id": batch.to_message_row_id,
                "from_message_id": batch.from_message_id,
                "to_message_id": batch.to_message_id,
                "source_count": batch.source_count,
                "source_digest": batch.source_digest,
                "encoder_version": batch.encoder_version,
                "prompt_version": batch.prompt_version,
            }
            if {key: row[key] for key in expected_batch} != expected_batch:
                raise ImmutableConflictError(f"encoding batch conflict: {batch_id}")

            stored_sources = connection.execute(
                "SELECT * FROM encoding_batch_sources WHERE batch_id=? "
                "ORDER BY source_order",
                (batch_id,),
            ).fetchall()
            expected_sources = [
                {
                    "message_row_id": source.message_row_id,
                    "message_id": source.message_id,
                    "session_id": source.session_id,
                    "active_date": source.active_date,
                    "calendar_date": source.calendar_date,
                    "source_ts": source.source_ts,
                    "source_role": source.source_role,
                    "source_kind": source.source_kind,
                    "source_event_type": source.source_event_type,
                    "content_digest": source.content_digest,
                    "source_order": index,
                }
                for index, source in enumerate(batch_sources)
            ]
            if len(stored_sources) != len(expected_sources) or any(
                {key: stored[key] for key in expected} != expected
                for stored, expected in zip(stored_sources, expected_sources)
            ):
                raise ImmutableConflictError(
                    f"encoding batch source conflict: {batch_id}"
                )

            event_rows = connection.execute(
                "SELECT * FROM events WHERE batch_id=? ORDER BY batch_ordinal",
                (batch_id,),
            ).fetchall()
            if len(event_rows) != int(row["event_count"]):
                raise ImmutableConflictError(
                    f"completed encoding batch is missing events: {batch_id}"
                )
            events: list[EventDraft] = []
            for event_row in event_rows:
                source_rows = connection.execute(
                    "SELECT * FROM event_sources WHERE event_id=? ORDER BY source_order",
                    (event_row["id"],),
                ).fetchall()
                participant_rows = connection.execute(
                    "SELECT participant_id FROM event_participants "
                    "WHERE event_id=? ORDER BY participant_order",
                    (event_row["id"],),
                ).fetchall()
                events.append(
                    EventDraft(
                        ordinal=int(event_row["batch_ordinal"]),
                        subject_id=str(event_row["subject_id"]),
                        event_type=str(event_row["event_type"]),
                        summary=str(event_row["summary"]),
                        occurred_at=str(event_row["occurred_at"]),
                        reported_at=str(event_row["reported_at"]),
                        active_date=str(event_row["active_date"]),
                        calendar_date=str(event_row["calendar_date"]),
                        importance=float(event_row["importance"]),
                        emotional_weight=float(event_row["emotional_weight"]),
                        confidence=float(event_row["confidence"]),
                        epistemic_status=str(event_row["epistemic_status"]),
                        attributes=json.loads(event_row["attributes_json"]),
                        sources=tuple(
                            SourceRef(
                                message_row_id=int(source_row["message_row_id"]),
                                message_id=str(source_row["message_id"]),
                                session_id=str(source_row["session_id"]),
                                source_ts=str(source_row["source_ts"]),
                                source_role=str(source_row["source_role"]),
                                source_kind=str(source_row["source_kind"]),
                                source_event_type=str(source_row["source_event_type"]),
                                scene_id=str(source_row["scene_id"]),
                                span_start=source_row["span_start"],
                                span_end=source_row["span_end"],
                                span_digest=str(source_row["span_digest"]),
                            )
                            for source_row in source_rows
                        ),
                        participant_ids=tuple(
                            str(participant_row["participant_id"])
                            for participant_row in participant_rows
                        ),
                        event_id=str(event_row["id"]),
                    )
                )
            return tuple(events)
        finally:
            connection.close()

    def append_continuation(self, link: EventLinkDraft) -> tuple[str, str]:
        """Append one chronological cross-batch continuation and its thread membership.

        The older event points to the newer event.  A new thread is anchored to the
        older event when necessary; later calls may only append to its tail.  This
        deliberately refuses thread merging or prepending because either operation
        would require mutable canonical membership.
        """

        self._validate_continuation(link)
        now = _now_iso()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            thread_id, link_id = self._append_continuation(connection, link, now)
            connection.commit()
            return thread_id, link_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_continuation(
        self,
        connection: sqlite3.Connection,
        link: EventLinkDraft,
        now: str,
    ) -> tuple[str, str]:
        """Apply a validated continuation inside the caller's transaction."""

        evidence_json = _canonical_json(dict(link.evidence))
        if len(evidence_json) > 4_096:
            raise ValueError("event link evidence is too large")
        link_id = link.link_id or _stable_id(
            "link", link.from_event_id, link.to_event_id, link.link_type
        )
        from_event = self._event_scope(connection, link.from_event_id)
        to_event = self._event_scope(connection, link.to_event_id)
        if from_event is None or to_event is None:
            raise SourceBoundaryError("event link endpoints must already exist")
        if from_event["namespace"] != to_event["namespace"]:
            raise SourceBoundaryError("event link endpoints must share a namespace")
        if from_event["batch_id"] == to_event["batch_id"]:
            raise SourceBoundaryError("continuations must cross encoding batches")
        if self._event_time_key(from_event["reported_at"]) > self._event_time_key(
            to_event["reported_at"]
        ):
            raise SourceBoundaryError("continuations must point from older to newer events")

        namespace = str(from_event["namespace"])
        thread_type = "scene_continuation"
        from_membership = self._continuity_membership(
            connection, link.from_event_id, namespace, thread_type
        )
        to_membership = self._continuity_membership(
            connection, link.to_event_id, namespace, thread_type
        )
        if len(from_membership) > 1 or len(to_membership) > 1:
            raise ThreadConflictError("an event belongs to multiple continuity threads")

        if not from_membership and not to_membership:
            thread_id = _stable_id(
                "thread", namespace, thread_type, link.from_event_id
            )
            connection.execute(
                "INSERT INTO event_threads "
                "(id, namespace, subject_id, thread_type, label, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    namespace,
                    from_event["subject_id"],
                    thread_type,
                    "event continuation",
                    now,
                ),
            )
            self._insert_thread_event(
                connection, thread_id, link.from_event_id, 0, now
            )
            self._insert_thread_event(connection, thread_id, link.to_event_id, 1, now)
        elif from_membership and not to_membership:
            thread_id = str(from_membership[0]["thread_id"])
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence_no), -1) + 1 "
                    "FROM thread_events WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()[0]
            )
            if int(from_membership[0]["sequence_no"]) != next_sequence - 1:
                raise ThreadConflictError(
                    "continuations may only append from the current thread tail"
                )
            self._insert_thread_event(
                connection, thread_id, link.to_event_id, next_sequence, now
            )
        elif not from_membership and to_membership:
            raise ThreadConflictError("continuations cannot prepend an older event")
        else:
            from_thread = str(from_membership[0]["thread_id"])
            to_thread = str(to_membership[0]["thread_id"])
            if from_thread != to_thread:
                raise ThreadConflictError("continuations cannot merge existing threads")
            if int(from_membership[0]["sequence_no"]) >= int(
                to_membership[0]["sequence_no"]
            ):
                raise ThreadConflictError("continuation order conflicts with thread order")
            thread_id = from_thread

        expected = {
            "from_event_id": link.from_event_id,
            "to_event_id": link.to_event_id,
            "link_type": link.link_type,
            "confidence": float(link.confidence),
            "evidence_json": evidence_json,
        }
        existing = connection.execute(
            "SELECT * FROM event_links WHERE id=?", (link_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO event_links "
                "(id, from_event_id, to_event_id, link_type, confidence, "
                "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link_id, *expected.values(), now),
            )
        elif {key: existing[key] for key in expected} != expected:
            raise ImmutableConflictError(f"event link conflict: {link_id}")
        return thread_id, link_id

    def commit_boundary_judgment(
        self,
        judgment: BoundaryLinkJudgmentDraft,
        accepted_link: EventLinkDraft | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Atomically persist one completed boundary receipt and any accepted link."""

        self._validate_boundary_judgment(judgment)
        if judgment.outcome == "accepted":
            if accepted_link is None:
                raise ValueError("an accepted boundary judgment requires an event link")
            self._validate_continuation(accepted_link)
            if (
                accepted_link.from_event_id != judgment.candidate_from_event_id
                or accepted_link.to_event_id != judgment.candidate_to_event_id
                or float(accepted_link.confidence) != float(judgment.confidence)
            ):
                raise ValueError("accepted link does not match its boundary judgment")
            evidence = dict(accepted_link.evidence)
            required_evidence = {
                "boundary_id": judgment.boundary_id,
                "previous_batch_id": judgment.previous_batch_id,
                "next_batch_id": judgment.next_batch_id,
                "linker_version": judgment.linker_version,
                "input_digest": judgment.input_digest,
                "reason_codes": list(judgment.reason_codes),
            }
            if any(evidence.get(key) != value for key, value in required_evidence.items()):
                raise ValueError("accepted link evidence does not match its judgment")
        elif accepted_link is not None:
            raise ValueError("only an accepted boundary judgment may persist an event link")

        judgment_id = judgment.judgment_id or _stable_id(
            "judgment", judgment.boundary_id
        )
        now = _now_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous_batch = self._completed_batch_scope(
                connection, judgment.previous_batch_id
            )
            next_batch = self._completed_batch_scope(connection, judgment.next_batch_id)
            if previous_batch is None or next_batch is None:
                raise SourceBoundaryError(
                    "boundary judgment batches must already be completed"
                )
            if (
                previous_batch["source_namespace"] != judgment.source_namespace
                or next_batch["source_namespace"] != judgment.source_namespace
            ):
                raise SourceBoundaryError(
                    "boundary judgment batches must share its source namespace"
                )
            overlap = connection.execute(
                "SELECT 1 FROM encoding_batch_sources previous "
                "JOIN encoding_batch_sources following "
                "ON (previous.message_row_id=following.message_row_id "
                "OR previous.message_id=following.message_id) "
                "WHERE previous.batch_id=? AND following.batch_id=? LIMIT 1",
                (judgment.previous_batch_id, judgment.next_batch_id),
            ).fetchone()
            previous_edge = self._batch_edge_source(
                connection, judgment.previous_batch_id, tail=True
            )
            next_edge = self._batch_edge_source(
                connection, judgment.next_batch_id, tail=False
            )
            if overlap is not None or previous_edge is None or next_edge is None:
                raise SourceBoundaryError(
                    "boundary judgment batches must be chronological and disjoint"
                )
            previous_key = (
                _source_instant(str(previous_edge["source_ts"])),
                int(previous_edge["message_row_id"]),
            )
            next_key = (
                _source_instant(str(next_edge["source_ts"])),
                int(next_edge["message_row_id"]),
            )
            if previous_key >= next_key:
                raise SourceBoundaryError(
                    "boundary judgment batches must be chronological and disjoint"
                )

            expected_boundary_id = _stable_id(
                "boundary",
                judgment.previous_batch_id,
                judgment.next_batch_id,
                judgment.linker_version,
            )
            if judgment.boundary_id != expected_boundary_id:
                raise SourceBoundaryError("boundary identity does not match its batches")

            if judgment.candidate_from_event_id:
                previous_event = self._event_scope(
                    connection, judgment.candidate_from_event_id
                )
                next_event = self._event_scope(connection, judgment.candidate_to_event_id)
                if (
                    previous_event is None
                    or next_event is None
                    or previous_event["batch_id"] != judgment.previous_batch_id
                    or next_event["batch_id"] != judgment.next_batch_id
                ):
                    raise SourceBoundaryError(
                        "boundary candidate events must belong to their declared sides"
                    )

            thread_id: str | None = None
            link_id: str | None = None
            if accepted_link is not None:
                thread_id, link_id = self._append_continuation(
                    connection, accepted_link, now
                )

            expected = {
                "id": judgment_id,
                "boundary_id": judgment.boundary_id,
                "previous_batch_id": judgment.previous_batch_id,
                "next_batch_id": judgment.next_batch_id,
                "source_namespace": judgment.source_namespace,
                "linker_version": judgment.linker_version,
                "input_digest": judgment.input_digest,
                "outcome": judgment.outcome,
                "minimum_confidence": float(judgment.minimum_confidence),
                "candidate_from_event_id": judgment.candidate_from_event_id or None,
                "candidate_to_event_id": judgment.candidate_to_event_id or None,
                "confidence": (
                    float(judgment.confidence)
                    if judgment.confidence is not None
                    else None
                ),
                "reason_codes_json": _canonical_json(list(judgment.reason_codes)),
                "accepted_link_id": link_id,
            }
            existing = connection.execute(
                "SELECT * FROM boundary_link_judgments WHERE boundary_id=?",
                (judgment.boundary_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO boundary_link_judgments "
                    "(id, boundary_id, previous_batch_id, next_batch_id, "
                    "source_namespace, linker_version, input_digest, outcome, "
                    "minimum_confidence, candidate_from_event_id, "
                    "candidate_to_event_id, confidence, reason_codes_json, "
                    "accepted_link_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*expected.values(), now),
                )
            elif {key: existing[key] for key in expected} != expected:
                raise ImmutableConflictError(
                    f"boundary judgment conflict: {judgment.boundary_id}"
                )
            connection.commit()
            return judgment_id, thread_id, link_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_day_settlement(
        self,
        settlement: DaySettlementDraft,
    ) -> tuple[str, tuple[str, ...]]:
        """Atomically persist one completed-day receipt, compact, and accepted links."""

        self._validate_day_settlement(settlement)
        now = _now_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_digest, batch_rows, event_rows = current_day_source_digest(
                connection,
                settlement.source_namespace,
                settlement.active_date,
                assembler_version=settlement.assembler_version,
            )
            if any(str(row["status"]) != "completed" for row in batch_rows):
                raise SourceBoundaryError(
                    "completed day gained an incomplete encoding batch"
                )
            if current_digest != settlement.source_digest:
                raise SourceBoundaryError(
                    "completed-day source changed after settlement assembly"
                )
            current_day_event_ids = tuple(
                str(row["id"])
                for row in event_rows
                if str(row["current_status"]) == "active"
            )
            if current_day_event_ids != tuple(settlement.day_event_ids):
                raise SourceBoundaryError(
                    "completed-day active event set changed after settlement assembly"
                )

            expected_receipt = self._day_settlement_receipt_values(settlement)
            existing = connection.execute(
                "SELECT * FROM day_settlements WHERE source_namespace=? "
                "AND active_date=? AND settlement_version=? AND prompt_version=?",
                (
                    settlement.source_namespace,
                    settlement.active_date,
                    settlement.settlement_version,
                    settlement.prompt_version,
                ),
            ).fetchone()
            if existing is not None:
                if {key: existing[key] for key in expected_receipt} != expected_receipt:
                    raise ImmutableConflictError(
                        "completed-day settlement identity already has a different result"
                    )
                link_ids = tuple(
                    str(row["accepted_link_id"])
                    for row in connection.execute(
                        "SELECT accepted_link_id FROM day_thread_judgments "
                        "WHERE settlement_id=? AND accepted_link_id IS NOT NULL "
                        "ORDER BY candidate_ref",
                        (settlement.settlement_id,),
                    ).fetchall()
                )
                connection.commit()
                return settlement.settlement_id, link_ids

            candidate_scopes = self._validate_day_candidates(
                connection, settlement
            )
            accepted_judgments = [
                judgment
                for judgment in settlement.judgments
                if judgment.accepted_link is not None
            ]
            accepted_judgments.sort(
                key=lambda judgment: (
                    self._event_time_key(
                        str(candidate_scopes[judgment.candidate_ref][0]["reported_at"])
                    ),
                    self._event_time_key(
                        str(candidate_scopes[judgment.candidate_ref][2]["reported_at"])
                    ),
                    judgment.candidate_ref,
                )
            )
            accepted_link_ids: dict[str, str] = {}
            for judgment in accepted_judgments:
                assert judgment.accepted_link is not None
                _, link_id = self._append_or_reuse_continuation(
                    connection, judgment.accepted_link, now
                )
                accepted_link_ids[judgment.candidate_ref] = link_id

            connection.execute(
                "INSERT INTO day_settlements "
                "(id, source_namespace, active_date, settlement_version, "
                "assembler_version, prompt_version, source_digest, input_digest, "
                "execution_digest, result_digest, minimum_continuation_confidence, "
                "day_event_count, candidate_count, compact_item_count, "
                "judgment_count, accepted_continuation_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*expected_receipt.values(), now),
            )
            for item in settlement.compact_items:
                connection.execute(
                    "INSERT INTO day_compact_items "
                    "(id, settlement_id, item_order, summary, content_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.item_id,
                        settlement.settlement_id,
                        item.ordinal,
                        item.summary,
                        item.content_digest,
                        now,
                    ),
                )
                for event_order, event_id in enumerate(item.event_ids):
                    connection.execute(
                        "INSERT INTO day_compact_item_events "
                        "(settlement_id, item_order, event_id, event_order, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            settlement.settlement_id,
                            item.ordinal,
                            event_id,
                            event_order,
                            now,
                        ),
                    )
            for judgment in settlement.judgments:
                connection.execute(
                    "INSERT INTO day_thread_judgments "
                    "(id, settlement_id, candidate_ref, outcome, confidence, "
                    "reason_codes_json, from_event_id, match_event_id, to_event_id, "
                    "prior_thread_id, accepted_link_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        judgment.judgment_id,
                        settlement.settlement_id,
                        judgment.candidate_ref,
                        judgment.outcome,
                        float(judgment.confidence),
                        _canonical_json(list(judgment.reason_codes)),
                        judgment.from_event_id,
                        judgment.match_event_id,
                        judgment.to_event_id,
                        judgment.prior_thread_id or None,
                        accepted_link_ids.get(judgment.candidate_ref),
                        now,
                    ),
                )
            connection.commit()
            return settlement.settlement_id, tuple(
                accepted_link_ids[key] for key in sorted(accepted_link_ids)
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_or_reuse_continuation(
        self,
        connection: sqlite3.Connection,
        link: EventLinkDraft,
        now: str,
    ) -> tuple[str, str]:
        """Reuse a compatible legacy edge, otherwise append a new one."""

        existing = connection.execute(
            "SELECT id FROM event_links WHERE from_event_id=? AND to_event_id=? "
            "AND link_type='continues'",
            (link.from_event_id, link.to_event_id),
        ).fetchone()
        if existing is None:
            return self._append_continuation(connection, link, now)
        from_scope = self._event_scope(connection, link.from_event_id)
        to_scope = self._event_scope(connection, link.to_event_id)
        if from_scope is None or to_scope is None:
            raise SourceBoundaryError("existing continuation endpoints are unavailable")
        namespace = str(from_scope["namespace"])
        from_membership = self._continuity_membership(
            connection, link.from_event_id, namespace, "scene_continuation"
        )
        to_membership = self._continuity_membership(
            connection, link.to_event_id, namespace, "scene_continuation"
        )
        if (
            len(from_membership) != 1
            or len(to_membership) != 1
            or from_membership[0]["thread_id"] != to_membership[0]["thread_id"]
            or int(from_membership[0]["sequence_no"])
            >= int(to_membership[0]["sequence_no"])
        ):
            raise ThreadConflictError(
                "existing continuation is inconsistent with append-only membership"
            )
        return str(from_membership[0]["thread_id"]), str(existing["id"])

    def get_day_settlement(self, settlement_id: str) -> dict | None:
        """Read one settlement receipt with its bounded compact and judgment audit."""

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM day_settlements WHERE id=?", (settlement_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            compact_rows = connection.execute(
                "SELECT * FROM day_compact_items WHERE settlement_id=? "
                "ORDER BY item_order",
                (settlement_id,),
            ).fetchall()
            result["compact_items"] = []
            for item_row in compact_rows:
                item = dict(item_row)
                item["event_ids"] = [
                    str(event_row["event_id"])
                    for event_row in connection.execute(
                        "SELECT event_id FROM day_compact_item_events "
                        "WHERE settlement_id=? AND item_order=? ORDER BY event_order",
                        (settlement_id, item_row["item_order"]),
                    ).fetchall()
                ]
                result["compact_items"].append(item)
            result["thread_judgments"] = []
            for judgment_row in connection.execute(
                "SELECT * FROM day_thread_judgments WHERE settlement_id=? "
                "ORDER BY candidate_ref",
                (settlement_id,),
            ).fetchall():
                judgment = dict(judgment_row)
                judgment["reason_codes"] = json.loads(
                    judgment.pop("reason_codes_json")
                )
                result["thread_judgments"].append(judgment)
            return result
        finally:
            connection.close()

    def get_day_settlement_receipt_for_day(
        self,
        *,
        source_namespace: str,
        active_date: str,
        settlement_version: str,
        prompt_version: str,
    ) -> dict | None:
        """Read only the content-free immutable receipt for one day/version."""

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM day_settlements WHERE source_namespace=? "
                "AND active_date=? AND settlement_version=? AND prompt_version=?",
                (
                    source_namespace,
                    active_date,
                    settlement_version,
                    prompt_version,
                ),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def list_settled_active_dates(
        self,
        *,
        source_namespace: str,
        settlement_version: str,
        prompt_version: str,
        before_active_date: str | None = None,
    ) -> tuple[str, ...]:
        """Return content-free completed-day watermarks for one contract."""

        where = [
            "source_namespace=?",
            "settlement_version=?",
            "prompt_version=?",
        ]
        params: list[str] = [
            str(source_namespace),
            str(settlement_version),
            str(prompt_version),
        ]
        if before_active_date is not None:
            where.append("active_date < ?")
            params.append(str(before_active_date))
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT active_date FROM day_settlements WHERE "
                + " AND ".join(where)
                + " ORDER BY active_date",
                params,
            ).fetchall()
            return tuple(str(row["active_date"]) for row in rows)
        finally:
            connection.close()

    def get_boundary_judgment(self, boundary_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM boundary_link_judgments WHERE boundary_id=?",
                (boundary_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["reason_codes"] = json.loads(result.pop("reason_codes_json"))
            return result
        finally:
            connection.close()

    def get_event_thread(self, thread_id: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM event_threads WHERE id=?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["event_ids"] = [
                item["event_id"]
                for item in connection.execute(
                    "SELECT event_id FROM thread_events WHERE thread_id=? "
                    "ORDER BY sequence_no",
                    (thread_id,),
                ).fetchall()
            ]
            result["links"] = [
                {
                    **dict(item),
                    "evidence": json.loads(item["evidence_json"]),
                }
                for item in connection.execute(
                    "SELECT l.* FROM event_links l "
                    "JOIN thread_events a ON a.event_id=l.from_event_id "
                    "JOIN thread_events b ON b.event_id=l.to_event_id "
                    "WHERE a.thread_id=? AND b.thread_id=? "
                    "ORDER BY a.sequence_no, b.sequence_no",
                    (thread_id, thread_id),
                ).fetchall()
            ]
            for item in result["links"]:
                item.pop("evidence_json", None)
            return result
        finally:
            connection.close()

    def append_event_thread_status(
        self,
        thread_id: str,
        status: str,
        *,
        reason_code: str,
        source_key: str,
        replacement_thread_id: str = "",
    ) -> str:
        """Append an idempotent correction without rewriting thread history."""

        thread_id = str(thread_id or "").strip()
        status = str(status or "").strip()
        reason_code = str(reason_code or "").strip()
        source_key = str(source_key or "").strip()
        replacement_thread_id = str(replacement_thread_id or "").strip()
        if not thread_id or status not in {"active", "retracted", "superseded"}:
            raise ValueError("event thread status is invalid")
        if not reason_code or len(reason_code) > 120:
            raise ValueError("event thread status reason_code is invalid")
        if not source_key or len(source_key) > 240:
            raise ValueError("event thread status source_key is invalid")
        if status == "superseded" and not replacement_thread_id:
            raise ValueError("superseded event thread needs a replacement")
        if status != "superseded" and replacement_thread_id:
            raise ValueError("replacement is only valid for superseded event threads")
        if replacement_thread_id == thread_id:
            raise ValueError("event thread cannot replace itself")

        status_id = _stable_id("thread_status", thread_id, source_key)
        now = _now_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM event_threads WHERE id=?", (thread_id,)
            ).fetchone() is None:
                raise SourceBoundaryError("event thread status target is missing")
            if replacement_thread_id and connection.execute(
                "SELECT 1 FROM event_threads WHERE id=?", (replacement_thread_id,)
            ).fetchone() is None:
                raise SourceBoundaryError("replacement event thread is missing")
            existing = connection.execute(
                "SELECT * FROM event_thread_status_log WHERE id=?", (status_id,)
            ).fetchone()
            values = (
                status_id,
                thread_id,
                status,
                reason_code,
                replacement_thread_id or None,
                source_key,
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO event_thread_status_log "
                    "(id, thread_id, status, reason_code, replacement_thread_id, "
                    "source_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*values, now),
                )
            elif (
                str(existing["thread_id"]),
                str(existing["status"]),
                str(existing["reason_code"]),
                str(existing["replacement_thread_id"] or ""),
                str(existing["source_key"]),
            ) != (
                thread_id,
                status,
                reason_code,
                replacement_thread_id,
                source_key,
            ):
                raise ImmutableConflictError(
                    "event thread status source key already has different content"
                )
            connection.commit()
            return status_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_event_thread_projection(
        self,
        thread_id: str,
        event_ids: Sequence[str],
        *,
        reason_code: str,
        source_key: str,
    ) -> tuple[str, str]:
        """Atomically supersede one active thread with a chronological projection.

        The replacement membership is a projection of already established thread
        lineage after event correction.  It does not invent fresh continuation
        edges; later settlement may append evidence-backed links where needed.
        """

        thread_id = str(thread_id or "").strip()
        projected_ids = tuple(
            dict.fromkeys(
                str(event_id).strip()
                for event_id in event_ids
                if str(event_id).strip()
            )
        )
        reason_code = str(reason_code or "").strip()
        source_key = str(source_key or "").strip()
        if not thread_id:
            raise ValueError("event thread projection target is required")
        if len(projected_ids) < 2:
            raise ValueError("replacement event thread requires at least two events")
        if not reason_code or len(reason_code) > 120:
            raise ValueError("event thread projection reason_code is invalid")
        if not source_key or len(source_key) > 240:
            raise ValueError("event thread projection source_key is invalid")

        replacement_thread_id = _stable_id(
            "thread_projection", thread_id, source_key
        )
        status_id = _stable_id("thread_status", thread_id, source_key)
        now = _now_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            thread_status_sql = effective_event_thread_status_sql("thread")
            previous = connection.execute(
                "SELECT thread.* FROM event_threads thread WHERE thread.id=? "
                f"AND {thread_status_sql}='active'",
                (thread_id,),
            ).fetchone()
            if previous is None:
                existing_status = connection.execute(
                    "SELECT * FROM event_thread_status_log WHERE id=?",
                    (status_id,),
                ).fetchone()
                existing_thread = connection.execute(
                    "SELECT * FROM event_threads WHERE id=?",
                    (replacement_thread_id,),
                ).fetchone()
                if (
                    existing_status is None
                    or existing_thread is None
                    or str(existing_status["thread_id"]) != thread_id
                    or str(existing_status["status"]) != "superseded"
                    or str(existing_status["reason_code"]) != reason_code
                    or str(existing_status["replacement_thread_id"] or "")
                    != replacement_thread_id
                    or str(existing_status["source_key"]) != source_key
                ):
                    raise SourceBoundaryError(
                        "event thread projection target is not active"
                    )
                actual_ids = tuple(
                    str(row["event_id"])
                    for row in connection.execute(
                        "SELECT event_id FROM thread_events WHERE thread_id=? "
                        "ORDER BY sequence_no",
                        (replacement_thread_id,),
                    ).fetchall()
                )
                if actual_ids != projected_ids:
                    raise ImmutableConflictError(
                        "replacement event thread projection conflicts"
                    )
                connection.commit()
                return replacement_thread_id, status_id

            placeholders = ",".join("?" for _ in projected_ids)
            event_status_sql = (
                "COALESCE((SELECT status FROM event_status_log status_log "
                "WHERE status_log.event_id=event.id "
                "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1),"
                "'active')"
            )
            rows = connection.execute(
                "SELECT event.id, event.subject_id, event.reported_at, "
                "batch.source_namespace FROM events event "
                "JOIN encoding_batches batch ON batch.id=event.batch_id "
                f"WHERE event.id IN ({placeholders}) "
                f"AND {event_status_sql}='active'",
                projected_ids,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if set(by_id) != set(projected_ids):
                raise SourceBoundaryError(
                    "replacement event thread contains a missing or inactive event"
                )
            ordered = [by_id[event_id] for event_id in projected_ids]
            if any(
                str(row["source_namespace"]) != str(previous["namespace"])
                for row in ordered
            ):
                raise SourceBoundaryError(
                    "replacement event thread crosses source namespaces"
                )
            if any(
                str(row["subject_id"]) != str(previous["subject_id"])
                for row in ordered
            ):
                raise ThreadConflictError(
                    "replacement event thread changes its subject"
                )
            time_keys = [
                self._event_time_key(str(row["reported_at"])) for row in ordered
            ]
            if time_keys != sorted(time_keys):
                raise ThreadConflictError(
                    "replacement event thread is not chronological"
                )

            active_membership_status_sql = effective_event_thread_status_sql(
                "member_thread"
            )
            conflicts = connection.execute(
                "SELECT membership.event_id, membership.thread_id "
                "FROM thread_events membership "
                "JOIN event_threads member_thread "
                "ON member_thread.id=membership.thread_id "
                f"WHERE membership.event_id IN ({placeholders}) "
                "AND membership.thread_id<>? "
                f"AND {active_membership_status_sql}='active'",
                (*projected_ids, thread_id),
            ).fetchall()
            if conflicts:
                raise ThreadConflictError(
                    "replacement projection event belongs to another active thread"
                )

            existing_thread = connection.execute(
                "SELECT * FROM event_threads WHERE id=?",
                (replacement_thread_id,),
            ).fetchone()
            expected_thread = (
                replacement_thread_id,
                str(previous["namespace"]),
                str(previous["subject_id"]),
                str(previous["thread_type"]),
                "event continuation repair projection",
            )
            if existing_thread is None:
                connection.execute(
                    "INSERT INTO event_threads "
                    "(id, namespace, subject_id, thread_type, label, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (*expected_thread, now),
                )
            elif (
                str(existing_thread["id"]),
                str(existing_thread["namespace"]),
                str(existing_thread["subject_id"]),
                str(existing_thread["thread_type"]),
                str(existing_thread["label"]),
            ) != expected_thread:
                raise ImmutableConflictError(
                    "replacement event thread identity conflicts"
                )

            membership_rows = connection.execute(
                "SELECT event_id, sequence_no FROM thread_events WHERE thread_id=? "
                "ORDER BY sequence_no",
                (replacement_thread_id,),
            ).fetchall()
            if membership_rows:
                actual_ids = tuple(str(row["event_id"]) for row in membership_rows)
                if actual_ids != projected_ids:
                    raise ImmutableConflictError(
                        "replacement event thread membership conflicts"
                    )
            else:
                for sequence_no, event_id in enumerate(projected_ids):
                    self._insert_thread_event(
                        connection,
                        replacement_thread_id,
                        event_id,
                        sequence_no,
                        now,
                    )

            existing_status = connection.execute(
                "SELECT * FROM event_thread_status_log WHERE id=?", (status_id,)
            ).fetchone()
            values = (
                status_id,
                thread_id,
                "superseded",
                reason_code,
                replacement_thread_id,
                source_key,
            )
            if existing_status is None:
                connection.execute(
                    "INSERT INTO event_thread_status_log "
                    "(id, thread_id, status, reason_code, replacement_thread_id, "
                    "source_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*values, now),
                )
            elif (
                str(existing_status["id"]),
                str(existing_status["thread_id"]),
                str(existing_status["status"]),
                str(existing_status["reason_code"]),
                str(existing_status["replacement_thread_id"] or ""),
                str(existing_status["source_key"]),
            ) != values:
                raise ImmutableConflictError(
                    "event thread projection status conflicts"
                )
            connection.commit()
            return replacement_thread_id, status_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _day_settlement_receipt_values(
        settlement: DaySettlementDraft,
    ) -> dict[str, object]:
        return {
            "id": settlement.settlement_id,
            "source_namespace": settlement.source_namespace,
            "active_date": settlement.active_date,
            "settlement_version": settlement.settlement_version,
            "assembler_version": settlement.assembler_version,
            "prompt_version": settlement.prompt_version,
            "source_digest": settlement.source_digest,
            "input_digest": settlement.input_digest,
            "execution_digest": settlement.execution_digest,
            "result_digest": settlement.result_digest,
            "minimum_continuation_confidence": float(
                settlement.minimum_continuation_confidence
            ),
            "day_event_count": len(settlement.day_event_ids),
            "candidate_count": len(settlement.candidates),
            "compact_item_count": len(settlement.compact_items),
            "judgment_count": len(settlement.judgments),
            "accepted_continuation_count": sum(
                judgment.accepted_link is not None
                for judgment in settlement.judgments
            ),
        }

    @staticmethod
    def _day_settlement_result_digest(settlement: DaySettlementDraft) -> str:
        return _digest(
            {
                "day_compact": [
                    {
                        "ordinal": item.ordinal,
                        "summary": item.summary,
                        "event_ids": list(item.event_ids),
                    }
                    for item in settlement.compact_items
                ],
                "thread_judgments": [
                    {
                        "candidate_ref": judgment.candidate_ref,
                        "outcome": judgment.outcome,
                        "confidence": judgment.confidence,
                        "reason_codes": list(judgment.reason_codes),
                        "from_event_id": judgment.from_event_id,
                        "match_event_id": judgment.match_event_id,
                        "to_event_id": judgment.to_event_id,
                        "prior_thread_id": judgment.prior_thread_id,
                        "accepted": judgment.accepted_link is not None,
                    }
                    for judgment in settlement.judgments
                ],
            }
        )

    @classmethod
    def _validate_day_settlement(cls, settlement: DaySettlementDraft) -> None:
        required = (
            settlement.settlement_id,
            settlement.source_namespace,
            settlement.active_date,
            settlement.settlement_version,
            settlement.assembler_version,
            settlement.prompt_version,
            settlement.source_digest,
            settlement.input_digest,
            settlement.execution_digest,
            settlement.result_digest,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("day settlement identity fields must not be empty")
        try:
            date.fromisoformat(settlement.active_date)
        except ValueError as exc:
            raise ValueError("day settlement active_date must be an ISO date") from exc
        for label, digest in (
            ("source", settlement.source_digest),
            ("input", settlement.input_digest),
            ("execution", settlement.execution_digest),
            ("result", settlement.result_digest),
        ):
            if len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise ValueError(f"day settlement {label} digest must be lowercase sha256")
        expected_id = _stable_id(
            "day_settlement",
            settlement.source_namespace,
            settlement.active_date,
            settlement.settlement_version,
            settlement.input_digest,
        )
        if settlement.settlement_id != expected_id:
            raise ValueError("day settlement ID does not match its immutable input")
        expected_execution = hashlib.sha256(
            f"{settlement.prompt_version}\0{settlement.input_digest}".encode("utf-8")
        ).hexdigest()
        if settlement.execution_digest != expected_execution:
            raise ValueError("day settlement execution digest does not match its prompt")
        minimum = float(settlement.minimum_continuation_confidence)
        if not 0 <= minimum <= 1:
            raise ValueError("day settlement confidence threshold is invalid")

        day_event_ids = tuple(str(item) for item in settlement.day_event_ids)
        if not day_event_ids or len(day_event_ids) != len(set(day_event_ids)) or any(
            not item for item in day_event_ids
        ):
            raise ValueError("day settlement event IDs must be unique and non-empty")
        candidates = tuple(settlement.candidates)
        candidate_by_ref: dict[str, DaySettlementCandidateDraft] = {}
        for candidate in candidates:
            if (
                not candidate.candidate_ref.strip()
                or candidate.candidate_ref in candidate_by_ref
                or not candidate.from_event_id.strip()
                or not candidate.match_event_id.strip()
                or not candidate.to_event_id.strip()
                or candidate.from_event_id == candidate.to_event_id
            ):
                raise ValueError("day settlement candidate identity is invalid")
            candidate_by_ref[candidate.candidate_ref] = candidate

        compact_items = tuple(settlement.compact_items)
        if not 1 <= len(compact_items) <= 16 or [
            item.ordinal for item in compact_items
        ] != list(range(len(compact_items))):
            raise ValueError("day compact item order must be consecutive and bounded")
        compact_event_ids: list[str] = []
        for item in compact_items:
            if (
                not item.summary.strip()
                or len(item.summary) > MAX_DAY_COMPACT_SUMMARY_CHARS
                or not item.event_ids
            ):
                raise ValueError("day compact item content is invalid")
            expected_item_id = _stable_id(
                "day_item", settlement.settlement_id, item.ordinal
            )
            expected_content_digest = _digest(
                {
                    "ordinal": item.ordinal,
                    "summary": item.summary,
                    "event_ids": list(item.event_ids),
                }
            )
            if (
                item.item_id != expected_item_id
                or item.content_digest != expected_content_digest
            ):
                raise ValueError("day compact item identity or digest is invalid")
            compact_event_ids.extend(str(event_id) for event_id in item.event_ids)
        if len(compact_event_ids) != len(set(compact_event_ids)) or not set(
            compact_event_ids
        ).issubset(day_event_ids):
            raise ValueError("day compact event assignments must be unique day events")

        seen_judgments: set[str] = set()
        for judgment in settlement.judgments:
            candidate = candidate_by_ref.get(judgment.candidate_ref)
            if candidate is None or judgment.candidate_ref in seen_judgments:
                raise ValueError("day judgment names an unknown or duplicate candidate")
            if (
                judgment.from_event_id != candidate.from_event_id
                or judgment.match_event_id != candidate.match_event_id
                or judgment.to_event_id != candidate.to_event_id
                or judgment.prior_thread_id != candidate.prior_thread_id
            ):
                raise ValueError("day judgment endpoints do not match its candidate")
            if judgment.outcome not in {"continue", "separate", "unknown"}:
                raise ValueError("day judgment outcome is invalid")
            confidence = float(judgment.confidence)
            reasons = tuple(str(reason).strip() for reason in judgment.reason_codes)
            if (
                not 0 <= confidence <= 1
                or not 1 <= len(reasons) <= 3
                or len(reasons) != len(set(reasons))
                or any(not reason for reason in reasons)
            ):
                raise ValueError("day judgment confidence or reasons are invalid")
            should_accept = judgment.outcome == "continue" and confidence >= minimum
            if should_accept != (judgment.accepted_link is not None):
                raise ValueError("day judgment accepted link does not match its threshold")
            expected_judgment_id = _stable_id(
                "day_judgment", settlement.settlement_id, judgment.candidate_ref
            )
            if judgment.judgment_id != expected_judgment_id:
                raise ValueError("day judgment ID does not match its candidate")
            if judgment.accepted_link is not None:
                cls._validate_continuation(judgment.accepted_link)
                link = judgment.accepted_link
                expected_evidence = {
                    "day_settlement_id": settlement.settlement_id,
                    "settlement_version": settlement.settlement_version,
                    "prompt_version": settlement.prompt_version,
                    "input_digest": settlement.input_digest,
                    "execution_digest": settlement.execution_digest,
                    "candidate_ref": judgment.candidate_ref,
                    "match_event_id": judgment.match_event_id,
                    "reason_codes": list(judgment.reason_codes),
                }
                if (
                    link.from_event_id != judgment.from_event_id
                    or link.to_event_id != judgment.to_event_id
                    or float(link.confidence) != confidence
                    or dict(link.evidence) != expected_evidence
                ):
                    raise ValueError("accepted day continuation does not match its judgment")
            seen_judgments.add(judgment.candidate_ref)
        if settlement.result_digest != cls._day_settlement_result_digest(settlement):
            raise ValueError("day settlement result digest does not match its content")

    @classmethod
    def _validate_day_candidates(
        cls,
        connection: sqlite3.Connection,
        settlement: DaySettlementDraft,
    ) -> dict[str, tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]]:
        day_event_ids = set(settlement.day_event_ids)
        result: dict[str, tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]] = {}
        for candidate in settlement.candidates:
            from_scope = cls._event_scope(connection, candidate.from_event_id)
            match_scope = cls._event_scope(connection, candidate.match_event_id)
            to_scope = cls._event_scope(connection, candidate.to_event_id)
            if from_scope is None or match_scope is None or to_scope is None:
                raise SourceBoundaryError(
                    "day settlement candidate endpoints must already be completed"
                )
            if (
                from_scope["namespace"] != settlement.source_namespace
                or match_scope["namespace"] != settlement.source_namespace
                or to_scope["namespace"] != settlement.source_namespace
                or candidate.to_event_id not in day_event_ids
                or from_scope["batch_id"] == to_scope["batch_id"]
                or cls._event_time_key(str(from_scope["reported_at"]))
                > cls._event_time_key(str(to_scope["reported_at"]))
            ):
                raise SourceBoundaryError("day settlement candidate scope changed")
            if candidate.prior_thread_id:
                thread = connection.execute(
                    "SELECT id FROM event_threads WHERE id=? AND namespace=? "
                    "AND thread_type='scene_continuation'",
                    (candidate.prior_thread_id, settlement.source_namespace),
                ).fetchone()
                memberships = connection.execute(
                    "SELECT event_id, sequence_no FROM thread_events "
                    "WHERE thread_id=? AND event_id IN (?, ?) ORDER BY sequence_no",
                    (
                        candidate.prior_thread_id,
                        candidate.match_event_id,
                        candidate.from_event_id,
                    ),
                ).fetchall()
                tail = connection.execute(
                    "SELECT event_id FROM thread_events WHERE thread_id=? "
                    "ORDER BY sequence_no DESC LIMIT 1",
                    (candidate.prior_thread_id,),
                ).fetchone()
                membership_ids = {str(row["event_id"]) for row in memberships}
                if (
                    thread is None
                    or membership_ids
                    != {candidate.match_event_id, candidate.from_event_id}
                    or tail is None
                    or str(tail["event_id"]) != candidate.from_event_id
                ):
                    raise ThreadConflictError(
                        "prior day-settlement thread changed after assembly"
                    )
            elif (
                candidate.match_event_id != candidate.from_event_id
                or candidate.from_event_id not in day_event_ids
            ):
                raise SourceBoundaryError("intra-day candidate scope changed")
            result[candidate.candidate_ref] = (from_scope, match_scope, to_scope)
        return result

    @staticmethod
    def _validate_continuation(link: EventLinkDraft) -> None:
        if link.link_type != "continues":
            raise ValueError("append_continuation only accepts continues links")
        if not link.from_event_id.strip() or not link.to_event_id.strip():
            raise ValueError("event link endpoints must not be empty")
        if link.from_event_id == link.to_event_id:
            raise ValueError("an event cannot continue itself")
        if not 0 <= float(link.confidence) <= 1:
            raise ValueError("event link confidence must be between 0 and 1")
        if not isinstance(link.evidence, Mapping):
            raise ValueError("event link evidence must be an object")

    @staticmethod
    def _validate_boundary_judgment(judgment: BoundaryLinkJudgmentDraft) -> None:
        required = (
            judgment.boundary_id,
            judgment.previous_batch_id,
            judgment.next_batch_id,
            judgment.source_namespace,
            judgment.linker_version,
            judgment.input_digest,
            judgment.outcome,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("boundary judgment identity fields must not be empty")
        if judgment.previous_batch_id == judgment.next_batch_id:
            raise ValueError("a boundary judgment requires two distinct batches")
        if len(judgment.input_digest) != 64 or any(
            char not in "0123456789abcdef" for char in judgment.input_digest
        ):
            raise ValueError("boundary input digest must be lowercase sha256")
        if judgment.outcome not in {
            "explicit_no_link",
            "below_threshold",
            "accepted",
            "skipped_empty_side",
        }:
            raise ValueError("unknown boundary judgment outcome")
        if not 0 <= float(judgment.minimum_confidence) <= 1:
            raise ValueError("boundary minimum confidence must be between 0 and 1")
        reason_codes = tuple(str(item).strip() for item in judgment.reason_codes)
        if (
            len(reason_codes) != len(set(reason_codes))
            or len(reason_codes) > 3
            or any(not item for item in reason_codes)
        ):
            raise ValueError("boundary reason codes must be unique non-empty values")

        has_candidate = bool(
            judgment.candidate_from_event_id or judgment.candidate_to_event_id
        )
        if judgment.outcome in {"explicit_no_link", "skipped_empty_side"}:
            if has_candidate or judgment.confidence is not None:
                raise ValueError("no-link boundary outcomes cannot contain a candidate")
            if judgment.outcome == "skipped_empty_side" and reason_codes:
                raise ValueError("an empty boundary cannot contain screening reasons")
            return
        if not (
            judgment.candidate_from_event_id
            and judgment.candidate_to_event_id
            and judgment.confidence is not None
            and reason_codes
        ):
            raise ValueError("candidate boundary outcomes require complete evidence")
        confidence = float(judgment.confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("boundary candidate confidence must be between 0 and 1")
        if judgment.outcome == "accepted" and confidence < float(
            judgment.minimum_confidence
        ):
            raise ValueError("accepted boundary confidence is below threshold")
        if judgment.outcome == "below_threshold" and confidence >= float(
            judgment.minimum_confidence
        ):
            raise ValueError("below-threshold boundary confidence reached threshold")

    @staticmethod
    def _event_time_key(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _event_scope(
        connection: sqlite3.Connection, event_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT e.id, e.batch_id, e.subject_id, e.reported_at, "
            "b.source_namespace AS namespace "
            "FROM events e JOIN encoding_batches b ON b.id=e.batch_id "
            "WHERE e.id=? AND b.status='completed'",
            (event_id,),
        ).fetchone()

    @staticmethod
    def _completed_batch_scope(
        connection: sqlite3.Connection, batch_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT id, source_namespace, from_message_row_id, to_message_row_id "
            "FROM encoding_batches WHERE id=? AND status='completed'",
            (batch_id,),
        ).fetchone()

    @staticmethod
    def _batch_edge_source(
        connection: sqlite3.Connection,
        batch_id: str,
        *,
        tail: bool,
    ) -> sqlite3.Row | None:
        direction = "DESC" if tail else "ASC"
        return connection.execute(
            "SELECT message_row_id, message_id, source_ts "
            "FROM encoding_batch_sources WHERE batch_id=? "
            f"ORDER BY source_order {direction} LIMIT 1",
            (batch_id,),
        ).fetchone()

    @staticmethod
    def _continuity_membership(
        connection: sqlite3.Connection,
        event_id: str,
        namespace: str,
        thread_type: str,
    ) -> list[sqlite3.Row]:
        thread_status_sql = effective_event_thread_status_sql("t")
        return connection.execute(
            "SELECT te.thread_id, te.sequence_no FROM thread_events te "
            "JOIN event_threads t ON t.id=te.thread_id "
            "WHERE te.event_id=? AND t.namespace=? AND t.thread_type=? "
            f"AND {thread_status_sql}='active'",
            (event_id, namespace, thread_type),
        ).fetchall()

    @staticmethod
    def _insert_thread_event(
        connection: sqlite3.Connection,
        thread_id: str,
        event_id: str,
        sequence_no: int,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO thread_events "
            "(thread_id, event_id, sequence_no, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, event_id, sequence_no, now),
        )

    @staticmethod
    def _validate_batch(batch: EncodingBatch) -> None:
        required = (
            batch.source_namespace,
            batch.session_id,
            batch.active_date,
            batch.from_message_id,
            batch.to_message_id,
            batch.source_digest,
            batch.encoder_version,
            batch.prompt_version,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("encoding batch fields must not be empty")
        if batch.source_count <= 0:
            raise ValueError("encoding batch source_count must be positive")
        if batch.from_message_row_id <= 0 or batch.to_message_row_id <= 0:
            raise SourceBoundaryError("encoding batch row identities must be positive")

    @staticmethod
    def _validate_batch_sources(
        batch: EncodingBatch,
        sources: Sequence[BatchSourceRef],
        source_validator: BatchSourceValidator | None,
    ) -> None:
        if len(sources) != batch.source_count:
            raise SourceBoundaryError("batch source manifest count does not match batch")
        if not sources:
            raise SourceBoundaryError("encoding batch requires an exact source manifest")
        if (
            sources[0].message_row_id != batch.from_message_row_id
            or sources[-1].message_row_id != batch.to_message_row_id
            or sources[0].message_id != batch.from_message_id
            or sources[-1].message_id != batch.to_message_id
        ):
            raise SourceBoundaryError("batch endpoints do not match its source manifest")
        seen_rows: set[int] = set()
        seen_ids: set[str] = set()
        for source in sources:
            if source.message_row_id <= 0:
                raise SourceBoundaryError("batch source row identity must be positive")
            required = (
                source.message_id,
                source.session_id,
                source.active_date,
                source.source_ts,
                source.source_role,
                source.source_kind,
                source.content_digest,
            )
            if any(not str(value).strip() for value in required):
                raise SourceBoundaryError("batch source identity must not be empty")
            if source.source_kind not in _SOURCE_KINDS:
                raise SourceBoundaryError("batch source kind is invalid")
            if source.source_kind != "chat" and source.source_role != "assistant":
                raise SourceBoundaryError("only assistant batch sources may be proactive")
            if (
                source.session_id != batch.session_id
                or source.active_date != batch.active_date
            ):
                raise SourceBoundaryError("batch source authority scope does not match batch")
            if source.message_row_id in seen_rows or source.message_id in seen_ids:
                raise SourceBoundaryError("batch source manifest contains duplicates")
            seen_rows.add(source.message_row_id)
            seen_ids.add(source.message_id)
        if digest_batch_sources(sources) != batch.source_digest:
            raise SourceBoundaryError("batch source manifest digest does not match batch")
        if source_validator is not None and not source_validator(sources):
            raise SourceBoundaryError("authoritative batch source validation failed")

    @staticmethod
    def _validate_events(
        batch: EncodingBatch,
        batch_sources: Sequence[BatchSourceRef],
        events: Sequence[EventDraft],
        source_validator: SourceValidator | None,
    ) -> None:
        manifest = {
            (source.message_row_id, source.message_id): source
            for source in batch_sources
        }
        ordinals: set[int] = set()
        for event in events:
            if event.ordinal < 0 or event.ordinal in ordinals:
                raise ValueError("event ordinals must be unique non-negative integers")
            ordinals.add(event.ordinal)
            if not event.subject_id.strip() or not event.event_type.strip() or not event.summary.strip():
                raise ValueError("event subject, type, and summary must not be empty")
            participant_ids = [str(value).strip() for value in event.participant_ids]
            if (
                not participant_ids
                or any(not value for value in participant_ids)
                or len(set(participant_ids)) != len(participant_ids)
                or event.subject_id not in participant_ids
            ):
                raise ValueError(
                    "event participants must be unique, non-empty, and include the primary subject"
                )
            if not event.reported_at or not event.active_date or not event.calendar_date:
                raise ValueError("event time axes must be explicit")
            if not 0 <= event.importance <= 1:
                raise ValueError("event importance must be between 0 and 1")
            if not -1 <= event.emotional_weight <= 1:
                raise ValueError("event emotional_weight must be between -1 and 1")
            if not 0 <= event.confidence <= 1:
                raise ValueError("event confidence must be between 0 and 1")
            if not event.sources:
                raise SourceBoundaryError("every event requires at least one source")
            seen_message_ids: set[str] = set()
            for source in event.sources:
                if (
                    not source.message_id.strip()
                    or not source.session_id.strip()
                    or not source.source_ts.strip()
                    or not source.source_role.strip()
                    or not source.source_kind.strip()
                ):
                    raise SourceBoundaryError("source identity must not be empty")
                if source.source_kind not in _SOURCE_KINDS:
                    raise SourceBoundaryError("event source kind is invalid")
                if source.source_kind != "chat" and source.source_role != "assistant":
                    raise SourceBoundaryError("only assistant event sources may be proactive")
                if source.session_id != batch.session_id:
                    raise SourceBoundaryError("source session does not match encoding batch")
                batch_source = manifest.get((source.message_row_id, source.message_id))
                if batch_source is None:
                    raise SourceBoundaryError("event source is absent from batch manifest")
                if (
                    source.session_id != batch_source.session_id
                    or source.source_ts != batch_source.source_ts
                    or source.source_role != batch_source.source_role
                    or source.source_kind != batch_source.source_kind
                    or source.source_event_type != batch_source.source_event_type
                ):
                    raise SourceBoundaryError("event source differs from batch manifest")
                if source.message_id in seen_message_ids:
                    raise SourceBoundaryError("event source message IDs must be unique")
                seen_message_ids.add(source.message_id)
                if (source.span_start is None) != (source.span_end is None):
                    raise SourceBoundaryError("source span requires both start and end")
                if source.span_start is not None and source.span_start >= source.span_end:
                    raise SourceBoundaryError("source span is empty or reversed")
                if source.span_start is not None and not source.span_digest.strip():
                    raise SourceBoundaryError("source span requires a digest")
                if source.span_start is None and source.span_digest:
                    raise SourceBoundaryError("source digest requires a span")
            if source_validator is not None and not source_validator(event.sources):
                raise SourceBoundaryError("authoritative source validation failed")

    @staticmethod
    def _batch_payload(batch: EncodingBatch) -> dict:
        payload = asdict(batch)
        payload.pop("batch_id", None)
        return payload

    def _insert_or_verify_batch(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        batch: EncodingBatch,
        event_count: int,
        now: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT * FROM encoding_batches WHERE id=?", (batch_id,)
        ).fetchone()
        expected = self._batch_payload(batch)
        if existing is not None:
            actual = {key: existing[key] for key in expected}
            if actual != expected:
                raise ImmutableConflictError(f"encoding batch conflict: {batch_id}")
            if existing["status"] == "completed":
                if int(existing["event_count"]) != event_count:
                    raise ImmutableConflictError(
                        f"completed encoding batch output conflict: {batch_id}"
                    )
                return True
            return False
        connection.execute(
            "INSERT INTO encoding_batches ("
            "id, source_namespace, session_id, active_date, "
            "from_message_row_id, to_message_row_id, from_message_id, to_message_id, "
            "source_count, source_digest, encoder_version, prompt_version, "
            "status, started_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                batch_id,
                batch.source_namespace,
                batch.session_id,
                batch.active_date,
                batch.from_message_row_id,
                batch.to_message_row_id,
                batch.from_message_id,
                batch.to_message_id,
                batch.source_count,
                batch.source_digest,
                batch.encoder_version,
                batch.prompt_version,
                now,
            ),
        )
        return False

    @staticmethod
    def _batch_source_payload(source: BatchSourceRef, source_order: int) -> dict:
        return {
            "message_row_id": source.message_row_id,
            "message_id": source.message_id,
            "session_id": source.session_id,
            "active_date": source.active_date,
            "calendar_date": source.calendar_date,
            "source_ts": source.source_ts,
            "source_role": source.source_role,
            "source_kind": source.source_kind,
            "source_event_type": source.source_event_type,
            "content_digest": source.content_digest,
            "source_order": source_order,
        }

    def _insert_or_verify_batch_sources(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        sources: Sequence[BatchSourceRef],
        *,
        allow_insert: bool,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM encoding_batch_sources WHERE batch_id=? ORDER BY source_order",
            (batch_id,),
        ).fetchall()
        expected = [
            self._batch_source_payload(source, index)
            for index, source in enumerate(sources)
        ]
        if rows:
            actual = [
                {key: row[key] for key in expected[index]}
                for index, row in enumerate(rows)
                if index < len(expected)
            ]
            if len(rows) != len(expected) or actual != expected:
                raise ImmutableConflictError(f"batch source conflict: {batch_id}")
            return
        if not allow_insert:
            raise ImmutableConflictError(
                f"completed encoding batch is missing source manifest: {batch_id}"
            )
        for payload in expected:
            columns = ["batch_id", *payload.keys()]
            values = [batch_id, *payload.values()]
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO encoding_batch_sources ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )

    @staticmethod
    def _event_payload(event: EventDraft, encoder_version: str) -> dict:
        attributes_json = _canonical_json(dict(event.attributes))
        immutable = {
            "batch_ordinal": event.ordinal,
            "subject_id": event.subject_id,
            "event_type": event.event_type,
            "summary": event.summary,
            "occurred_at": event.occurred_at,
            "reported_at": event.reported_at,
            "active_date": event.active_date,
            "calendar_date": event.calendar_date,
            "importance": float(event.importance),
            "emotional_weight": float(event.emotional_weight),
            "confidence": float(event.confidence),
            "epistemic_status": event.epistemic_status,
            "attributes_json": attributes_json,
            "encoder_version": encoder_version,
        }
        immutable["content_digest"] = _digest(
            {**immutable, "participant_ids": list(event.participant_ids)}
        )
        return immutable

    def _insert_or_verify_event(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        event_id: str,
        encoder_version: str,
        event: EventDraft,
        now: str,
        *,
        allow_insert: bool,
    ) -> None:
        expected = self._event_payload(event, encoder_version)
        existing = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if existing is not None:
            actual = {key: existing[key] for key in expected}
            if actual != expected or existing["batch_id"] != batch_id:
                raise ImmutableConflictError(f"event conflict: {event_id}")
            return
        if not allow_insert:
            raise ImmutableConflictError(
                f"completed encoding batch is missing event: {event_id}"
            )
        columns = ["id", "batch_id", *expected.keys(), "created_at"]
        values = [event_id, batch_id, *expected.values(), now]
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO events ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )

    @staticmethod
    def _source_payload(source: SourceRef, source_order: int) -> dict:
        return {
            "message_row_id": source.message_row_id,
            "message_id": source.message_id,
            "session_id": source.session_id,
            "source_ts": source.source_ts,
            "source_role": source.source_role,
            "source_kind": source.source_kind,
            "source_event_type": source.source_event_type,
            "scene_id": source.scene_id,
            "source_order": source_order,
            "span_start": source.span_start,
            "span_end": source.span_end,
            "span_digest": source.span_digest,
        }

    def _insert_or_verify_sources(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        sources: Sequence[SourceRef],
        *,
        allow_insert: bool,
    ) -> None:
        existing_rows = connection.execute(
            "SELECT * FROM event_sources WHERE event_id=? ORDER BY source_order", (event_id,)
        ).fetchall()
        expected = [self._source_payload(source, index) for index, source in enumerate(sources)]
        if existing_rows:
            actual = [
                {key: row[key] for key in expected[index]}
                for index, row in enumerate(existing_rows)
                if index < len(expected)
            ]
            if len(existing_rows) != len(expected) or actual != expected:
                raise ImmutableConflictError(f"event source conflict: {event_id}")
            return
        if not allow_insert:
            raise ImmutableConflictError(
                f"completed encoding batch is missing sources: {event_id}"
            )
        for payload in expected:
            columns = ["event_id", *payload.keys()]
            values = [event_id, *payload.values()]
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO event_sources ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    @staticmethod
    def _insert_or_verify_participants(
        connection: sqlite3.Connection,
        event_id: str,
        primary_subject_id: str,
        participant_ids: Sequence[str],
        *,
        allow_insert: bool,
    ) -> None:
        existing_rows = connection.execute(
            "SELECT participant_id, participant_order, is_primary "
            "FROM event_participants WHERE event_id=? ORDER BY participant_order",
            (event_id,),
        ).fetchall()
        expected = [
            {
                "participant_id": participant_id,
                "participant_order": index,
                "is_primary": int(participant_id == primary_subject_id),
            }
            for index, participant_id in enumerate(participant_ids)
        ]
        if existing_rows:
            actual = [dict(row) for row in existing_rows]
            if actual != expected:
                raise ImmutableConflictError(
                    f"event participant conflict: {event_id}"
                )
            return
        if not allow_insert:
            raise ImmutableConflictError(
                f"completed encoding batch is missing participants: {event_id}"
            )
        for payload in expected:
            connection.execute(
                "INSERT INTO event_participants "
                "(event_id, participant_id, participant_order, is_primary) "
                "VALUES (?, ?, ?, ?)",
                (
                    event_id,
                    payload["participant_id"],
                    payload["participant_order"],
                    payload["is_primary"],
                ),
            )
