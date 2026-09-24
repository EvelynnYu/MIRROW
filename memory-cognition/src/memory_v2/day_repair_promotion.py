"""Atomic promotion of one validated Memory V2 day-repair staging delta."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .conversation_source import ConversationSource
from .day_repair import (
    DAY_REPAIR_CONTRACT_VERSION,
    DayRepairPlan,
    DayRepairPlanner,
    day_authority_digest,
)
from .day_repair_manifest import (
    DayRepairManifestError,
    DayRepairStageManifest,
    build_day_repair_stage_manifest,
)
from .day_repair_staging import DayRepairStagingResult
from .replay import ReplayPlanner
from .schema import SCHEMA_VERSION


_COPY_TABLES = (
    ("encoding_batches", "id", "batch_ids"),
    ("encoding_batch_sources", "batch_id", "batch_ids"),
    ("events", "id", "event_ids"),
    ("event_sources", "event_id", "event_ids"),
    ("event_participants", "event_id", "event_ids"),
    ("event_threads", "id", "replacement_thread_ids"),
    ("thread_events", "thread_id", "replacement_thread_ids"),
    ("event_status_log", "id", "event_status_ids"),
    ("event_thread_status_log", "id", "thread_status_ids"),
)


class DayRepairPromotionError(RuntimeError):
    """A staged repair cannot be atomically promoted."""


@dataclass(frozen=True)
class DayRepairPromotionResult:
    status: str
    repair_id: str
    active_date: str
    stage_result_digest: str = ""
    batch_count: int = 0
    new_event_count: int = 0
    superseded_event_count: int = 0
    retracted_event_count: int = 0
    replacement_thread_count: int = 0
    retracted_thread_count: int = 0
    error_code: str = ""

    @property
    def promoted(self) -> bool:
        return self.status in {"promoted", "already_promoted"}

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repair_id": self.repair_id,
            "active_date": self.active_date,
            "stage_result_digest": self.stage_result_digest,
            "batch_count": self.batch_count,
            "new_event_count": self.new_event_count,
            "superseded_event_count": self.superseded_event_count,
            "retracted_event_count": self.retracted_event_count,
            "replacement_thread_count": self.replacement_thread_count,
            "retracted_thread_count": self.retracted_thread_count,
            "error_code": self.error_code,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _placeholders(values: Sequence[object]) -> str:
    if not values:
        raise ValueError("cannot build placeholders for an empty sequence")
    return ",".join("?" for _value in values)


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT schema_version FROM memory_schema WHERE schema_key='memory_v2'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _receipt_values(
    plan: DayRepairPlan,
    staging: DayRepairStagingResult,
) -> dict[str, object]:
    event_dispositions = Counter(
        item.disposition for item in staging.event_replacements
    )
    thread_dispositions = Counter(
        item.disposition for item in staging.thread_projections
    )
    return {
        "id": plan.repair_id,
        "source_namespace": plan.source_namespace,
        "active_date": plan.active_date,
        "repair_contract_version": DAY_REPAIR_CONTRACT_VERSION,
        "source_revision": plan.source_revision,
        "authority_digest": plan.authority_digest,
        "baseline_digest": plan.baseline_digest,
        "stage_result_digest": staging.stage_result_digest,
        "repair_encoder_version": plan.repair_encoder_version,
        "repair_settlement_version": plan.repair_settlement_version,
        "batch_count": len(plan.replay_day.batches),
        "new_event_count": len(staging.new_event_ids),
        "superseded_event_count": event_dispositions.get("superseded", 0),
        "retracted_event_count": event_dispositions.get("retracted", 0),
        "replacement_thread_count": thread_dispositions.get("replace", 0),
        "retracted_thread_count": thread_dispositions.get("retract", 0),
        "request_count": staging.request_count,
        "prompt_tokens": staging.prompt_tokens,
        "completion_tokens": staging.completion_tokens,
    }


def _result(
    status: str,
    plan: DayRepairPlan,
    staging: DayRepairStagingResult,
    *,
    error_code: str = "",
) -> DayRepairPromotionResult:
    values = _receipt_values(plan, staging)
    return DayRepairPromotionResult(
        status=status,
        repair_id=plan.repair_id,
        active_date=plan.active_date,
        stage_result_digest=staging.stage_result_digest,
        batch_count=int(values["batch_count"]),
        new_event_count=int(values["new_event_count"]),
        superseded_event_count=int(values["superseded_event_count"]),
        retracted_event_count=int(values["retracted_event_count"]),
        replacement_thread_count=int(values["replacement_thread_count"]),
        retracted_thread_count=int(values["retracted_thread_count"]),
        error_code=error_code,
    )


def _same_plan(expected: DayRepairPlan, current: DayRepairPlan) -> bool:
    return (
        current.ready
        and current.repair_id == expected.repair_id
        and current.authority_digest == expected.authority_digest
        and current.baseline_digest == expected.baseline_digest
        and current.previous_event_ids == expected.previous_event_ids
        and current.invalid_event_ids == expected.invalid_event_ids
    )


def _baseline_digest(
    connection: sqlite3.Connection,
    plan: DayRepairPlan,
) -> str:
    rows = connection.execute(
        "SELECT * FROM ("
        "SELECT event.id, event.content_digest, "
        "COALESCE((SELECT status FROM event_status_log status_log "
        "WHERE status_log.event_id=event.id "
        "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1), "
        "'active') AS current_status, "
        "COALESCE((SELECT source_revision FROM event_status_log status_log "
        "WHERE status_log.event_id=event.id "
        "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1), "
        "'') AS source_revision "
        "FROM events event JOIN encoding_batches batch ON batch.id=event.batch_id "
        "WHERE batch.source_namespace=? AND batch.active_date=? "
        "AND batch.status='completed') current_events "
        "WHERE current_status IN ('active', 'invalid_source') ORDER BY id",
        (plan.source_namespace, plan.active_date),
    ).fetchall()
    return DayRepairPlanner._baseline_digest(rows)


def _authority_digest(source: ConversationSource, plan: DayRepairPlan) -> str:
    day = ReplayPlanner(source, plan.base_replay_config).plan_active_date(
        plan.active_date
    )
    return day_authority_digest(day)


def _existing_receipt(
    connection: sqlite3.Connection,
    expected: dict[str, object],
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM day_repair_receipts WHERE id=?",
        (expected["id"],),
    ).fetchone()
    if row is None:
        return None
    for key, value in expected.items():
        if row[key] != value:
            raise DayRepairPromotionError(
                "day repair receipt identity already has different content"
            )
    return row


def _validate_staging_result(
    plan: DayRepairPlan,
    staging: DayRepairStagingResult,
    manifest: DayRepairStageManifest,
) -> None:
    if (
        not staging.staged
        or staging.repair_id != plan.repair_id
        or staging.active_date != plan.active_date
        or not staging.stage_result_digest
        or staging.stage_result_digest != manifest.result_digest
        or set(staging.new_event_ids) != set(manifest.event_ids)
    ):
        raise DayRepairPromotionError("day repair staging result identity is invalid")
    expected_events = tuple(
        sorted(
            (
                item.previous_event_id,
                item.disposition,
                item.replacement_event_id,
            )
            for item in staging.event_replacements
        )
    )
    if expected_events != manifest.event_replacements:
        raise DayRepairPromotionError("day repair event mapping changed after staging")
    expected_threads = tuple(
        sorted(
            (
                item.previous_thread_id,
                "superseded" if item.disposition == "replace" else "retracted",
                item.replacement_thread_id,
            )
            for item in staging.thread_projections
        )
    )
    if expected_threads != manifest.thread_replacements:
        raise DayRepairPromotionError("day repair thread mapping changed after staging")


def _table_columns(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in connection.execute(
            f'PRAGMA "{schema}".table_info("{table}")'
        ).fetchall()
    )


def _copy_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    values: Sequence[str],
) -> None:
    if not values:
        return
    if _table_columns(connection, "main", table) != _table_columns(
        connection, "stage", table
    ):
        raise DayRepairPromotionError(f"repair table schema differs: {table}")
    placeholders = _placeholders(values)
    conflict = connection.execute(
        f'SELECT 1 FROM main."{table}" WHERE "{key_column}" IN ({placeholders}) '
        "LIMIT 1",
        tuple(values),
    ).fetchone()
    if conflict is not None:
        raise DayRepairPromotionError(f"repair delta already exists without receipt: {table}")
    connection.execute(
        f'INSERT INTO main."{table}" SELECT * FROM stage."{table}" '
        f'WHERE "{key_column}" IN ({placeholders})',
        tuple(values),
    )


def _validate_thread_baselines(
    connection: sqlite3.Connection,
    plan: DayRepairPlan,
    manifest: DayRepairStageManifest,
) -> None:
    new_event_ids = set(manifest.event_ids)
    for previous_thread_id, _status, _replacement_id in manifest.thread_replacements:
        main_thread = connection.execute(
            "SELECT * FROM main.event_threads WHERE id=?",
            (previous_thread_id,),
        ).fetchone()
        stage_thread = connection.execute(
            "SELECT * FROM stage.event_threads WHERE id=?",
            (previous_thread_id,),
        ).fetchone()
        if main_thread is None or stage_thread is None or dict(main_thread) != dict(stage_thread):
            raise DayRepairPromotionError("affected thread identity changed after staging")
        main_membership = connection.execute(
            "SELECT event_id, sequence_no, created_at FROM main.thread_events "
            "WHERE thread_id=? ORDER BY sequence_no",
            (previous_thread_id,),
        ).fetchall()
        stage_membership = connection.execute(
            "SELECT event_id, sequence_no, created_at FROM stage.thread_events "
            "WHERE thread_id=? ORDER BY sequence_no",
            (previous_thread_id,),
        ).fetchall()
        if [tuple(row) for row in main_membership] != [
            tuple(row) for row in stage_membership
        ]:
            raise DayRepairPromotionError("affected thread membership changed after staging")
        main_statuses = connection.execute(
            "SELECT * FROM main.event_thread_status_log WHERE thread_id=? ORDER BY id",
            (previous_thread_id,),
        ).fetchall()
        stage_statuses = connection.execute(
            "SELECT * FROM stage.event_thread_status_log "
            "WHERE thread_id=? AND source_key<>? ORDER BY id",
            (previous_thread_id, plan.source_revision),
        ).fetchall()
        if [tuple(row) for row in main_statuses] != [tuple(row) for row in stage_statuses]:
            raise DayRepairPromotionError("affected thread status changed after staging")

    existing_projected_ids = sorted(
        {
            event_id
            for _thread_id, event_ids in manifest.thread_memberships
            for event_id in event_ids
            if event_id not in new_event_ids
        }
    )
    if not existing_projected_ids:
        return
    placeholders = _placeholders(existing_projected_ids)
    rows = connection.execute(
        "SELECT event.id, COALESCE((SELECT status FROM main.event_status_log status_log "
        "WHERE status_log.event_id=event.id "
        "ORDER BY status_log.created_at DESC, status_log.id DESC LIMIT 1), "
        "'active') AS current_status FROM main.events event "
        f"WHERE event.id IN ({placeholders})",
        tuple(existing_projected_ids),
    ).fetchall()
    if len(rows) != len(existing_projected_ids) or any(
        str(row["current_status"]) != "active" for row in rows
    ):
        raise DayRepairPromotionError("projected existing event is no longer active")


def _insert_receipt(
    connection: sqlite3.Connection,
    values: dict[str, object],
) -> None:
    columns = tuple(values)
    connection.execute(
        "INSERT INTO day_repair_receipts ("
        + ", ".join(columns)
        + ", created_at) VALUES ("
        + ", ".join("?" for _column in columns)
        + ", ?)",
        (*tuple(values[column] for column in columns), _now_iso()),
    )


def promote_day_repair(
    plan: DayRepairPlan,
    staging: DayRepairStagingResult,
    source: ConversationSource,
    *,
    memory_db_path: str | Path,
) -> DayRepairPromotionResult:
    """Promote an exact stage delta and its receipt in one SQLite transaction."""

    memory_path = Path(memory_db_path).resolve()
    if not memory_path.is_file():
        raise ValueError("Memory V2 production database is missing")
    connection = sqlite3.connect(memory_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    expected_receipt = _receipt_values(plan, staging)
    try:
        if _schema_version(connection) != SCHEMA_VERSION:
            return _result(
                "schema_not_ready",
                plan,
                staging,
                error_code="day_repair_receipt_schema_missing",
            )
        if _existing_receipt(connection, expected_receipt) is not None:
            return _result("already_promoted", plan, staging)
    except (sqlite3.Error, DayRepairPromotionError) as exc:
        return _result(
            "promotion_error", plan, staging, error_code=type(exc).__name__
        )
    finally:
        connection.close()

    if not _same_plan(
        plan,
        DayRepairPlanner(
            source,
            memory_path,
            replay_config=plan.base_replay_config,
        ).plan(
            active_date=plan.active_date,
            completed_before_active_date=plan.completed_before_active_date,
        ),
    ):
        return _result("stale_plan", plan, staging)

    stage_path = Path(staging.stage_db_path).resolve()
    if (
        not stage_path.is_file()
        or stage_path.name != "memory_v2.db"
        or stage_path.parent.name != plan.repair_id
    ):
        return _result(
            "invalid_stage", plan, staging, error_code="stage_path_invalid"
        )
    try:
        manifest = build_day_repair_stage_manifest(stage_path, plan)
        _validate_staging_result(plan, staging, manifest)
    except (DayRepairManifestError, DayRepairPromotionError) as exc:
        return _result(
            "invalid_stage", plan, staging, error_code=type(exc).__name__
        )

    connection = sqlite3.connect(memory_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    attached = False
    try:
        connection.execute("ATTACH DATABASE ? AS stage", (str(stage_path),))
        attached = True
        connection.execute("BEGIN IMMEDIATE")
        if _baseline_digest(connection, plan) != plan.baseline_digest:
            raise DayRepairPromotionError("production baseline changed before promotion")
        if _authority_digest(source, plan) != plan.authority_digest:
            raise DayRepairPromotionError("conversation authority changed before promotion")
        _validate_thread_baselines(connection, plan, manifest)
        for table, key_column, manifest_attribute in _COPY_TABLES:
            _copy_rows(
                connection,
                table=table,
                key_column=key_column,
                values=getattr(manifest, manifest_attribute),
            )
        if _authority_digest(source, plan) != plan.authority_digest:
            raise DayRepairPromotionError("conversation authority changed during promotion")
        _insert_receipt(connection, expected_receipt)
        connection.commit()
        return _result("promoted", plan, staging)
    except Exception as exc:
        connection.rollback()
        return _result(
            "promotion_error", plan, staging, error_code=type(exc).__name__
        )
    finally:
        if attached:
            try:
                connection.execute("DETACH DATABASE stage")
            except sqlite3.Error:
                pass
        connection.close()


__all__ = [
    "DayRepairPromotionError",
    "DayRepairPromotionResult",
    "promote_day_repair",
]
