"""Isolated execution of a planned Memory V2 day repair.

The production store is opened read-only and copied with SQLite's online backup
API.  New encoding, old-event disposition, and evidence-preserving thread
projection happen only inside the staging copy.  Promotion, repair receipts,
and any needed compact or new-link settlement remain separate responsibilities.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .conversation_source import ConversationSource
from .day_repair import DayRepairPlan, DayRepairPlanner
from .day_repair_manifest import build_day_repair_stage_manifest
from .encoding import ParsedEncoding
from .recall import EventRecallIndex
from .replay import ReplayBatchPlan
from .store import MemoryV2Store


BatchEncoder = Callable[
    [ConversationSource, ReplayBatchPlan, int],
    Awaitable[tuple[dict[str, Any], ParsedEncoding | None]],
]


@dataclass(frozen=True)
class DayRepairEventReplacement:
    previous_event_id: str
    disposition: str
    replacement_event_id: str = ""


@dataclass(frozen=True)
class DayRepairThreadProjection:
    previous_thread_id: str
    disposition: str
    projected_event_ids: tuple[str, ...]
    replacement_thread_id: str = ""


@dataclass(frozen=True)
class DayRepairStagingResult:
    status: str
    repair_id: str
    active_date: str
    stage_db_path: str = ""
    stage_result_digest: str = ""
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    new_event_ids: tuple[str, ...] = ()
    event_replacements: tuple[DayRepairEventReplacement, ...] = ()
    thread_projections: tuple[DayRepairThreadProjection, ...] = ()
    error_code: str = ""

    @property
    def staged(self) -> bool:
        return self.status == "staged_core_ready"

    def safe_dict(self) -> dict[str, Any]:
        dispositions = Counter(item.disposition for item in self.event_replacements)
        thread_dispositions = Counter(
            item.disposition for item in self.thread_projections
        )
        return {
            "status": self.status,
            "repair_id": self.repair_id,
            "active_date": self.active_date,
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "stage_result_digest": self.stage_result_digest,
            "new_event_count": len(self.new_event_ids),
            "previous_event_count": len(self.event_replacements),
            "superseded_event_count": dispositions.get("superseded", 0),
            "retracted_event_count": dispositions.get("retracted", 0),
            "affected_thread_count": len(self.thread_projections),
            "replaceable_thread_count": thread_dispositions.get("replace", 0),
            "retracted_thread_count": thread_dispositions.get("retract", 0),
            "blocked_thread_count": thread_dispositions.get("blocked_order", 0),
            "error_code": self.error_code,
        }


async def _default_batch_encoder(
    source: ConversationSource,
    plan: ReplayBatchPlan,
    max_requests: int,
) -> tuple[dict[str, Any], ParsedEncoding | None]:
    from .experiment import _encode_plan, _resolve_encoding_quality

    report, parsed = await _encode_plan(
        source,
        plan,
        {
            "active_date": plan.batch.active_date,
            "batch_id": plan.batch.batch_id,
            "source_message_count": plan.batch.source_count,
            "estimated_source_tokens": plan.estimated_tokens,
        },
        max_requests=max_requests,
    )
    if parsed is None:
        return report, None
    return await _resolve_encoding_quality(
        source,
        plan,
        report,
        parsed,
        enabled=False,
    )


def _usage_value(report: dict[str, Any], key: str) -> int:
    usage = report.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _copy_sqlite(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(destination_path, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _event_rows(
    db_path: Path,
    event_ids: Sequence[str],
) -> dict[str, sqlite3.Row]:
    if not event_ids:
        return {}
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in event_ids)
        return {
            str(row["id"]): row
            for row in connection.execute(
                "SELECT id, subject_id, event_type, reported_at "
                f"FROM events WHERE id IN ({placeholders})",
                tuple(event_ids),
            ).fetchall()
        }
    finally:
        connection.close()


def _event_time_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_sets(
    db_path: Path,
    event_ids: Sequence[str],
) -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    if not event_ids:
        return {}
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        for offset in range(0, len(event_ids), 400):
            chunk = tuple(event_ids[offset : offset + 400])
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                "SELECT event_id, message_id FROM event_sources "
                f"WHERE event_id IN ({placeholders})",
                chunk,
            ).fetchall():
                result[str(row["event_id"])].add(str(row["message_id"]))
    finally:
        connection.close()
    return {event_id: frozenset(values) for event_id, values in result.items()}


def _map_events(
    db_path: Path,
    previous_event_ids: Sequence[str],
    new_event_ids: Sequence[str],
) -> tuple[DayRepairEventReplacement, ...]:
    previous_rows = _event_rows(db_path, previous_event_ids)
    new_rows = _event_rows(db_path, new_event_ids)
    sources = _source_sets(db_path, (*previous_event_ids, *new_event_ids))
    result: list[DayRepairEventReplacement] = []
    for previous_event_id in previous_event_ids:
        previous = previous_rows[previous_event_id]
        previous_sources = sources.get(previous_event_id, frozenset())
        candidates: list[tuple[tuple[float, float, int], str]] = []
        for new_event_id in new_event_ids:
            current = new_rows[new_event_id]
            if str(current["subject_id"]) != str(previous["subject_id"]):
                continue
            new_sources = sources.get(new_event_id, frozenset())
            overlap = len(previous_sources.intersection(new_sources))
            if not overlap:
                continue
            union = len(previous_sources.union(new_sources))
            score = (
                float(overlap),
                overlap / max(1, union),
                int(str(current["event_type"]) == str(previous["event_type"])),
            )
            candidates.append((score, new_event_id))
        if candidates:
            best_score = max(score for score, _event_id in candidates)
            best_ids = sorted(
                event_id for score, event_id in candidates if score == best_score
            )
        else:
            best_ids = []
        if len(best_ids) == 1:
            result.append(
                DayRepairEventReplacement(
                    previous_event_id=previous_event_id,
                    disposition="superseded",
                    replacement_event_id=best_ids[0],
                )
            )
        else:
            result.append(
                DayRepairEventReplacement(
                    previous_event_id=previous_event_id,
                    disposition="retracted",
                )
            )
    return tuple(result)


def _apply_event_replacements(
    store: MemoryV2Store,
    plan: DayRepairPlan,
    replacements: Sequence[DayRepairEventReplacement],
) -> None:
    for item in replacements:
        store.append_event_status(
            item.previous_event_id,
            item.disposition,
            reason_code=(
                "day_repair_unique_source_replacement"
                if item.disposition == "superseded"
                else "day_repair_no_unique_source_replacement"
            ),
            source_revision=plan.source_revision,
            replacement_event_id=item.replacement_event_id,
        )


def _thread_projections(
    db_path: Path,
    replacements: Sequence[DayRepairEventReplacement],
) -> tuple[DayRepairThreadProjection, ...]:
    if not replacements:
        return ()
    replacement_by_event = {
        item.previous_event_id: item.replacement_event_id for item in replacements
    }
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in replacement_by_event)
        thread_status_sql = (
            "COALESCE((SELECT status FROM event_thread_status_log status_log "
            "WHERE status_log.thread_id=thread.id "
            "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1),"
            "'active')"
        )
        thread_ids = tuple(
            str(row["thread_id"])
            for row in connection.execute(
                "SELECT DISTINCT membership.thread_id FROM thread_events membership "
                "JOIN event_threads thread ON thread.id=membership.thread_id "
                f"WHERE membership.event_id IN ({placeholders}) "
                f"AND {thread_status_sql}='active' ORDER BY membership.thread_id",
                tuple(replacement_by_event),
            ).fetchall()
        )
        result: list[DayRepairThreadProjection] = []
        for thread_id in thread_ids:
            rows = connection.execute(
                "SELECT membership.event_id, event.reported_at, "
                "COALESCE((SELECT status FROM event_status_log status_log "
                "WHERE status_log.event_id=event.id "
                "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1),"
                "'active') AS current_status "
                "FROM thread_events membership "
                "JOIN events event ON event.id=membership.event_id "
                "WHERE membership.thread_id=? ORDER BY membership.sequence_no",
                (thread_id,),
            ).fetchall()
            projected: list[str] = []
            for row in rows:
                event_id = str(row["event_id"])
                if event_id in replacement_by_event:
                    replacement = replacement_by_event[event_id]
                    if replacement and replacement not in projected:
                        projected.append(replacement)
                elif str(row["current_status"]) == "active" and event_id not in projected:
                    projected.append(event_id)
            if len(projected) < 2:
                disposition = "retract"
            else:
                projected_rows = _event_rows(db_path, projected)
                timestamps = [
                    _event_time_key(str(projected_rows[event_id]["reported_at"]))
                    for event_id in projected
                ]
                disposition = (
                    "replace"
                    if timestamps == sorted(timestamps)
                    else "blocked_order"
                )
            result.append(
                DayRepairThreadProjection(
                    previous_thread_id=thread_id,
                    disposition=disposition,
                    projected_event_ids=tuple(projected),
                )
            )
        return tuple(result)
    finally:
        connection.close()


def _apply_thread_projections(
    store: MemoryV2Store,
    plan: DayRepairPlan,
    projections: Sequence[DayRepairThreadProjection],
) -> tuple[DayRepairThreadProjection, ...]:
    applied: list[DayRepairThreadProjection] = []
    for projection in projections:
        if projection.disposition == "blocked_order":
            raise RuntimeError("thread projection order is ambiguous")
        if projection.disposition == "retract":
            store.append_event_thread_status(
                projection.previous_thread_id,
                "retracted",
                reason_code="day_repair_projection_became_too_short",
                source_key=plan.source_revision,
            )
            applied.append(projection)
            continue
        replacement_thread_id, _status_id = store.replace_event_thread_projection(
            projection.previous_thread_id,
            projection.projected_event_ids,
            reason_code="day_repair_projection_replaced",
            source_key=plan.source_revision,
        )
        applied.append(
            DayRepairThreadProjection(
                previous_thread_id=projection.previous_thread_id,
                disposition=projection.disposition,
                projected_event_ids=projection.projected_event_ids,
                replacement_thread_id=replacement_thread_id,
            )
        )
    return tuple(applied)


def _validate_stage_projection(
    stage_db_path: Path,
    previous_event_ids: Sequence[str],
    new_event_ids: Sequence[str],
) -> None:
    visible_ids = [
        event_id
        for document in EventRecallIndex(stage_db_path).load_documents()
        for event_id in document.event_ids
    ]
    counts = Counter(visible_ids)
    if set(previous_event_ids).intersection(visible_ids):
        raise RuntimeError("previous repair generation remains recall-visible")
    if any(counts[event_id] != 1 for event_id in new_event_ids):
        raise RuntimeError("new repair generation is missing or duplicated in recall")


def _same_plan(expected: DayRepairPlan, current: DayRepairPlan) -> bool:
    return (
        current.ready
        and current.repair_id == expected.repair_id
        and current.authority_digest == expected.authority_digest
        and current.baseline_digest == expected.baseline_digest
        and current.previous_event_ids == expected.previous_event_ids
        and current.invalid_event_ids == expected.invalid_event_ids
    )


async def execute_day_repair_staging(
    plan: DayRepairPlan,
    source: ConversationSource,
    *,
    memory_db_path: str | Path,
    stage_root: str | Path,
    max_requests_per_batch: int = 2,
    batch_encoder: BatchEncoder = _default_batch_encoder,
) -> DayRepairStagingResult:
    """Run the event-level repair only inside a new staging copy."""

    if max_requests_per_batch <= 0:
        raise ValueError("max_requests_per_batch must be positive")
    if not plan.ready:
        return DayRepairStagingResult(
            status=plan.status,
            repair_id=plan.repair_id,
            active_date=plan.active_date,
        )
    memory_path = Path(memory_db_path).resolve()
    root = Path(stage_root).resolve()
    if not memory_path.is_file():
        raise ValueError("Memory V2 production source is missing")
    current = DayRepairPlanner(
        source,
        memory_path,
        replay_config=plan.base_replay_config,
    ).plan(
        active_date=plan.active_date,
        completed_before_active_date=plan.completed_before_active_date,
    )
    if not _same_plan(plan, current):
        return DayRepairStagingResult(
            status="stale_plan",
            repair_id=plan.repair_id,
            active_date=plan.active_date,
        )

    root.mkdir(parents=True, exist_ok=True)
    stage_dir = root / plan.repair_id
    stage_db_path = stage_dir / "memory_v2.db"
    if stage_dir.exists():
        return DayRepairStagingResult(
            status="stage_conflict",
            repair_id=plan.repair_id,
            active_date=plan.active_date,
            error_code="stage_identity_already_exists",
        )
    stage_dir.mkdir()
    request_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    new_event_ids: list[str] = []
    try:
        _copy_sqlite(memory_path, stage_db_path)
        stage_store = MemoryV2Store(stage_db_path)
        for batch in plan.replay_day.batches:
            report, parsed = await batch_encoder(
                source, batch, max_requests_per_batch
            )
            request_count += int(report.get("request_count") or 0)
            prompt_tokens += _usage_value(report, "prompt_tokens")
            completion_tokens += _usage_value(report, "completion_tokens")
            if parsed is None or str(report.get("status")) != "ok":
                raise RuntimeError(
                    "batch_encoder_failed:"
                    + str(report.get("status") or "unknown")[:80]
                )
            new_event_ids.extend(
                stage_store.commit_encoding(
                    batch.batch,
                    parsed.events,
                    batch_sources=batch.batch_sources,
                    source_validator=source.validate_refs,
                    batch_source_validator=source.validate_batch_sources,
                )
            )

        current = DayRepairPlanner(
            source,
            memory_path,
            replay_config=plan.base_replay_config,
        ).plan(
            active_date=plan.active_date,
            completed_before_active_date=plan.completed_before_active_date,
        )
        if not _same_plan(plan, current):
            raise RuntimeError("repair plan changed during staging")

        replacements = _map_events(
            stage_db_path, plan.previous_event_ids, new_event_ids
        )
        _apply_event_replacements(stage_store, plan, replacements)
        projections = _apply_thread_projections(
            stage_store,
            plan,
            _thread_projections(stage_db_path, replacements),
        )
        _validate_stage_projection(
            stage_db_path, plan.previous_event_ids, new_event_ids
        )
        manifest = build_day_repair_stage_manifest(stage_db_path, plan)
        expected_event_replacements = tuple(
            sorted(
                (
                    item.previous_event_id,
                    item.disposition,
                    item.replacement_event_id,
                )
                for item in replacements
            )
        )
        if (
            set(manifest.event_ids) != set(new_event_ids)
            or manifest.event_replacements != expected_event_replacements
        ):
            raise RuntimeError("day repair stage manifest does not match its result")
        expected_thread_replacements = tuple(
            sorted(
                (
                    item.previous_thread_id,
                    (
                        "superseded"
                        if item.disposition == "replace"
                        else "retracted"
                    ),
                    item.replacement_thread_id,
                )
                for item in projections
            )
        )
        if manifest.thread_replacements != expected_thread_replacements:
            raise RuntimeError("day repair thread manifest does not match its result")
        return DayRepairStagingResult(
            status="staged_core_ready",
            repair_id=plan.repair_id,
            active_date=plan.active_date,
            stage_db_path=str(stage_db_path),
            stage_result_digest=manifest.result_digest,
            request_count=request_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            new_event_ids=tuple(new_event_ids),
            event_replacements=replacements,
            thread_projections=projections,
        )
    except Exception as exc:
        if stage_dir.is_dir() and stage_dir.parent == root:
            shutil.rmtree(stage_dir)
        return DayRepairStagingResult(
            status="staging_error",
            repair_id=plan.repair_id,
            active_date=plan.active_date,
            request_count=request_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=type(exc).__name__,
        )


__all__ = [
    "BatchEncoder",
    "DayRepairEventReplacement",
    "DayRepairStagingResult",
    "DayRepairThreadProjection",
    "execute_day_repair_staging",
]
