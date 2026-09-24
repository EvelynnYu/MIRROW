"""Read-only planning for versioned repair of a sealed Memory V2 active day.

Planning is deliberately separate from execution.  It binds one repair identity
to both the current immutable event projection and the latest authoritative raw
day, then derives collision-free encoding and settlement identities.  No model
call or database write occurs here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Sequence

from .conversation_source import ConversationSource
from .day_settlement import DAY_SETTLEMENT_VERSION
from .replay import ReplayConfig, ReplayDayPlan, ReplayPlanner
from .schema import SCHEMA_VERSION


DAY_REPAIR_CONTRACT_VERSION = "day-repair-v1"
_MINIMUM_SCHEMA_VERSION = 9


class DayRepairPlanningError(RuntimeError):
    """A sealed day cannot be represented by a trustworthy repair plan."""


@dataclass(frozen=True)
class DayRepairPlan:
    status: str
    active_date: str
    completed_before_active_date: str
    source_namespace: str
    repair_id: str
    source_revision: str
    authority_digest: str
    baseline_digest: str
    repair_encoder_version: str
    repair_settlement_version: str
    previous_event_ids: tuple[str, ...]
    invalid_event_ids: tuple[str, ...]
    replay_day: ReplayDayPlan
    base_replay_config: ReplayConfig
    incomplete_batch_count: int = 0

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "ready_empty"}

    @property
    def base_request_ceiling(self) -> int:
        if not self.ready:
            return 0
        return len(self.replay_day.batches)

    def safe_dict(self) -> dict[str, Any]:
        """Return a body-free operator receipt."""

        return {
            "status": self.status,
            "active_date": self.active_date,
            "completed_before_active_date": self.completed_before_active_date,
            "source_namespace": self.source_namespace,
            "repair_id": self.repair_id,
            "source_revision": self.source_revision,
            "authority_digest": self.authority_digest,
            "baseline_digest": self.baseline_digest,
            "repair_encoder_version": self.repair_encoder_version,
            "repair_settlement_version": self.repair_settlement_version,
            "previous_event_count": len(self.previous_event_ids),
            "invalid_event_count": len(self.invalid_event_ids),
            "message_count": self.replay_day.message_count,
            "batch_count": len(self.replay_day.batches),
            "estimated_source_tokens": self.replay_day.estimated_tokens,
            "base_request_ceiling": self.base_request_ceiling,
            "incomplete_batch_count": self.incomplete_batch_count,
        }


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def day_authority_digest(day: ReplayDayPlan) -> str:
    return _digest(
        {
            "active_date": day.active_date,
            "roles": list(day.roles),
            "batches": [
                {
                    "session_id": plan.batch.session_id,
                    "from_message_id": plan.batch.from_message_id,
                    "to_message_id": plan.batch.to_message_id,
                    "source_count": plan.batch.source_count,
                    "source_digest": plan.batch.source_digest,
                }
                for plan in day.batches
            ],
        }
    )


class DayRepairPlanner:
    """Construct stable repair identities from two read-only authorities."""

    def __init__(
        self,
        source: ConversationSource,
        memory_db_path: str | Path,
        *,
        replay_config: ReplayConfig | None = None,
    ):
        path = Path(memory_db_path).resolve()
        if not path.is_file():
            raise DayRepairPlanningError(f"Memory V2 database does not exist: {path}")
        self.source = source
        self.memory_db_path = path
        self.replay_config = replay_config or ReplayConfig()
        self.replay_config.validate()
        self._verify_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.memory_db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _verify_schema(self) -> None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT schema_version FROM memory_schema "
                    "WHERE schema_key='memory_v2'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise DayRepairPlanningError(
                f"cannot inspect Memory V2 repair source: {exc}"
            ) from exc
        if row is None:
            raise DayRepairPlanningError("Memory V2 repair schema is missing")
        version = int(row["schema_version"])
        if not _MINIMUM_SCHEMA_VERSION <= version <= SCHEMA_VERSION:
            raise DayRepairPlanningError(
                f"unsupported Memory V2 repair schema: {version}"
            )

    @staticmethod
    def _validate_completed_date(
        active_date: str,
        completed_before_active_date: str,
    ) -> tuple[str, str]:
        try:
            target = date.fromisoformat(str(active_date))
            cutoff = date.fromisoformat(str(completed_before_active_date))
        except ValueError as exc:
            raise DayRepairPlanningError(
                "repair dates must use ISO YYYY-MM-DD"
            ) from exc
        if target >= cutoff:
            raise DayRepairPlanningError(
                "target active day is not proven complete by the supplied cutoff"
            )
        return target.isoformat(), cutoff.isoformat()

    def _load_baseline(
        self,
        source_namespace: str,
        active_date: str,
    ) -> tuple[list[sqlite3.Row], int]:
        with self._connect() as connection:
            incomplete_batch_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM encoding_batches "
                    "WHERE source_namespace=? AND active_date=? "
                    "AND status<>'completed'",
                    (source_namespace, active_date),
                ).fetchone()[0]
            )
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
                "FROM events event "
                "JOIN encoding_batches batch ON batch.id=event.batch_id "
                "WHERE batch.source_namespace=? AND batch.active_date=? "
                "AND batch.status='completed') current_events "
                "WHERE current_status IN ('active', 'invalid_source') "
                "ORDER BY id",
                (source_namespace, active_date),
            ).fetchall()
        return list(rows), incomplete_batch_count

    @staticmethod
    def _baseline_digest(rows: Sequence[sqlite3.Row]) -> str:
        return _digest(
            [
                {
                    "event_id": str(row["id"]),
                    "content_digest": str(row["content_digest"]),
                    "current_status": str(row["current_status"]),
                    "source_revision": str(row["source_revision"]),
                }
                for row in rows
            ]
        )

    def plan(
        self,
        *,
        active_date: str,
        completed_before_active_date: str,
    ) -> DayRepairPlan:
        active_date, cutoff = self._validate_completed_date(
            active_date, completed_before_active_date
        )
        source_namespace = self.replay_config.source_namespace
        baseline_rows, incomplete_batch_count = self._load_baseline(
            source_namespace, active_date
        )
        previous_event_ids = tuple(str(row["id"]) for row in baseline_rows)
        invalid_event_ids = tuple(
            str(row["id"])
            for row in baseline_rows
            if str(row["current_status"]) == "invalid_source"
        )
        baseline_digest = self._baseline_digest(baseline_rows)

        base_day = ReplayPlanner(self.source, self.replay_config).plan_active_date(
            active_date
        )
        authority_digest = day_authority_digest(base_day)
        identity_digest = _digest(
            {
                "contract_version": DAY_REPAIR_CONTRACT_VERSION,
                "source_namespace": source_namespace,
                "active_date": active_date,
                "authority_digest": authority_digest,
                "baseline_digest": baseline_digest,
                "base_encoder_version": self.replay_config.encoder_version,
                "prompt_version": self.replay_config.prompt_version,
            }
        )
        repair_id = f"day_repair_{identity_digest}"
        source_revision = f"day_repair:{identity_digest}"
        suffix = identity_digest[:16]
        repair_encoder_version = (
            f"{self.replay_config.encoder_version}+{DAY_REPAIR_CONTRACT_VERSION}-{suffix}"
        )
        repair_settlement_version = (
            f"{DAY_SETTLEMENT_VERSION}+{DAY_REPAIR_CONTRACT_VERSION}-{suffix}"
        )
        repair_config = replace(
            self.replay_config,
            encoder_version=repair_encoder_version,
        )
        repair_day = ReplayPlanner(self.source, repair_config).plan_active_date(
            active_date
        )
        if day_authority_digest(repair_day) != authority_digest:
            raise DayRepairPlanningError("authority changed during repair planning")

        if not invalid_event_ids:
            status = "not_needed"
        elif incomplete_batch_count:
            status = "blocked_incomplete_batches"
        elif repair_day.batches:
            status = "ready"
        else:
            status = "ready_empty"
        return DayRepairPlan(
            status=status,
            active_date=active_date,
            completed_before_active_date=cutoff,
            source_namespace=source_namespace,
            repair_id=repair_id,
            source_revision=source_revision,
            authority_digest=authority_digest,
            baseline_digest=baseline_digest,
            repair_encoder_version=repair_encoder_version,
            repair_settlement_version=repair_settlement_version,
            previous_event_ids=previous_event_ids,
            invalid_event_ids=invalid_event_ids,
            replay_day=repair_day,
            base_replay_config=self.replay_config,
            incomplete_batch_count=incomplete_batch_count,
        )


__all__ = [
    "DAY_REPAIR_CONTRACT_VERSION",
    "DayRepairPlan",
    "DayRepairPlanner",
    "DayRepairPlanningError",
    "day_authority_digest",
]
