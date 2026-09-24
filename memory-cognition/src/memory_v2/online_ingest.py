"""Low-frequency production ingestion for completed Memory V2 active days.

The online boundary is deliberately day-level.  Current conversation rows stay
in raw context; only a date strictly older than the caller's conservative
cutoff may be encoded and settled.  This keeps model calls out of the per-turn
path and prevents a late message from changing an already frozen batch.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .conversation_source import ConversationSource
from .day_backfill_runner import run_backfill_day
from .day_repair_runner import run_day_repair
from .day_settlement import DAY_SETTLEMENT_VERSION
from .day_settlement_runner import DAY_SETTLEMENT_PROMPT_VERSION
from .replay import ReplayPlanner
from .store import MemoryV2Store


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_online_lock = asyncio.Lock()


class OnlineIngestError(RuntimeError):
    """A safe, body-free completed-day ingestion failure."""


@dataclass(frozen=True)
class OnlineIngestConfiguration:
    memory_db_path: Path
    authority_db_path: Path
    max_days_per_run: int = 3
    max_requests_per_batch: int = 2
    repair_stage_root: Path | None = None

    @property
    def resolved_repair_stage_root(self) -> Path:
        if self.repair_stage_root is not None:
            return self.repair_stage_root.resolve()
        return (self.memory_db_path.parent / ".memory_v2_day_repair_staging").resolve()

    def validate(self) -> None:
        if not self.memory_db_path.is_file():
            raise ValueError("memory_v2_db_missing")
        if not self.authority_db_path.is_file():
            raise ValueError("conversation_authority_missing")
        if self.max_days_per_run <= 0:
            raise ValueError("max_days_per_run_must_be_positive")
        if self.max_requests_per_batch <= 0:
            raise ValueError("max_requests_per_batch_must_be_positive")
        if self.resolved_repair_stage_root.is_file():
            raise ValueError("repair_stage_root_must_be_a_directory")


@dataclass(frozen=True)
class OnlineIngestResult:
    status: str
    trigger: str
    completed_before_active_date: str
    candidate_count: int = 0
    selected_count: int = 0
    processed_count: int = 0
    processed_active_dates: tuple[str, ...] = ()
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    event_count: int = 0
    repair_candidate_count: int = 0
    repaired_count: int = 0
    ingested_count: int = 0
    refresh_status: str = "not_requested"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trigger": self.trigger,
            "completed_before_active_date": self.completed_before_active_date,
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "processed_count": self.processed_count,
            "processed_active_dates": list(self.processed_active_dates),
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "event_count": self.event_count,
            "repair_candidate_count": self.repair_candidate_count,
            "repaired_count": self.repaired_count,
            "ingested_count": self.ingested_count,
            "refresh_status": self.refresh_status,
        }


DayRunner = Callable[..., Awaitable[dict[str, Any]]]
RepairRunner = Callable[..., Awaitable[Any]]
Refresher = Callable[[Sequence[str | Path]], dict[str, object]]


def online_ingest_enabled() -> bool:
    return (
        os.environ.get("MIRROW_MEMORY_V2_ONLINE_INGEST", "")
        .strip()
        .casefold()
        in _TRUE_VALUES
    )


def _positive_int_from_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name.lower()}_must_be_an_integer") from exc
    if value <= 0:
        raise ValueError(f"{name.lower()}_must_be_positive")
    return value


def configuration_from_environment() -> OnlineIngestConfiguration | None:
    if not online_ingest_enabled():
        return None
    backend_root = Path(__file__).resolve().parents[1]
    raw_paths = (
        os.environ.get("MIRROW_MEMORY_V2_DBS", "").strip()
        or os.environ.get("MIRROW_MEMORY_V2_SHADOW_DBS", "").strip()
    )
    paths = tuple(
        Path(value).resolve()
        for value in raw_paths.split(os.pathsep)
        if value.strip()
    )
    if len(paths) != 1:
        raise ValueError("online_ingest_requires_one_memory_v2_db")
    configuration = OnlineIngestConfiguration(
        memory_db_path=paths[0],
        authority_db_path=Path(
            os.environ.get(
                "MIRROW_MEMORY_V2_AUTHORITY_DB",
                str(backend_root / "events" / "event_chronicle.db"),
            )
        ).resolve(),
        max_days_per_run=_positive_int_from_environment(
            "MIRROW_MEMORY_V2_ONLINE_MAX_DAYS_PER_RUN", 3
        ),
        max_requests_per_batch=_positive_int_from_environment(
            "MIRROW_MEMORY_V2_ONLINE_MAX_REQUESTS_PER_BATCH", 2
        ),
        repair_stage_root=Path(
            os.environ.get(
                "MIRROW_MEMORY_V2_REPAIR_STAGE_ROOT",
                str(backend_root / ".tmp" / "memory_v2_day_repair"),
            )
        ).resolve(),
    )
    configuration.validate()
    return configuration


def conservative_completed_before_active_date(
    now: datetime,
    *,
    active_topic_dates: Sequence[str] = (),
    day_change_hour: int = 6,
) -> str:
    """Return the earliest date that is not proven complete.

    Before 06:00, the previous calendar day may still own a cross-midnight
    conversation.  Any currently active topic lowers the cutoff further.
    """

    if not 0 <= day_change_hour <= 23:
        raise ValueError("day_change_hour_out_of_range")
    clock_cutoff = now.date()
    if now.hour < day_change_hour:
        clock_cutoff -= timedelta(days=1)
    candidates = [clock_cutoff]
    for value in active_topic_dates:
        try:
            candidates.append(date.fromisoformat(str(value)))
        except ValueError as exc:
            raise ValueError("active_topic_date_must_be_iso") from exc
    return min(candidates).isoformat()


def _usage_value(report: dict[str, Any], key: str) -> int:
    usage = report.get("usage")
    raw_value = usage.get(key) if isinstance(usage, dict) else report.get(key)
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return 0


async def ingest_completed_days(
    configuration: OnlineIngestConfiguration,
    *,
    completed_before_active_date: str,
    trigger: str,
    day_runner: DayRunner = run_backfill_day,
    repair_runner: RepairRunner = run_day_repair,
    refresher: Refresher | None = None,
) -> OnlineIngestResult:
    """Encode missing completed days oldest-first, with one process lock."""

    configuration.validate()
    try:
        cutoff = date.fromisoformat(str(completed_before_active_date)).isoformat()
    except ValueError as exc:
        raise ValueError("completed_before_active_date_must_be_iso") from exc
    safe_trigger = str(trigger or "unspecified")[:80]

    async with _online_lock:
        source = ConversationSource(
            configuration.authority_db_path,
            canonical_sessions_only=True,
        )
        planner = ReplayPlanner(source)
        # The up-to-date path must be genuinely read-only: do not execute even
        # idempotent DDL on every startup/daily check.  The day runner performs
        # normal schema initialisation only after a missing completed day exists.
        store = MemoryV2Store(configuration.memory_db_path, initialise=False)
        repair_candidates = tuple(
            active_date
            for active_date in store.list_pending_source_rebuild_dates(
                source_namespace=planner.config.source_namespace
            )
            if active_date < cutoff
        )
        source_dates = source.list_active_dates(before_active_date=cutoff)
        settled_dates = set(
            store.list_settled_active_dates(
                source_namespace=planner.config.source_namespace,
                settlement_version=DAY_SETTLEMENT_VERSION,
                prompt_version=DAY_SETTLEMENT_PROMPT_VERSION,
                before_active_date=cutoff,
            )
        )
        repair_set = set(repair_candidates)
        ingest_candidates = tuple(
            day
            for day in source_dates
            if day not in settled_dates and day not in repair_set
        )
        candidates = tuple(
            sorted(
                (
                    *((active_date, "repair") for active_date in repair_candidates),
                    *((active_date, "ingest") for active_date in ingest_candidates),
                ),
                key=lambda item: (item[0], item[1]),
            )
        )
        selected = candidates[: configuration.max_days_per_run]
        if not selected:
            return OnlineIngestResult(
                status="up_to_date",
                trigger=safe_trigger,
                completed_before_active_date=cutoff,
                candidate_count=0,
                selected_count=0,
                repair_candidate_count=0,
            )

        processed: list[str] = []
        request_count = 0
        prompt_tokens = 0
        completion_tokens = 0
        event_count = 0
        repaired_count = 0
        ingested_count = 0
        for active_date, work_kind in selected:
            if work_kind == "repair":
                raw_report = await repair_runner(
                    source,
                    memory_db_path=configuration.memory_db_path,
                    stage_root=configuration.resolved_repair_stage_root,
                    active_date=active_date,
                    completed_before_active_date=cutoff,
                    max_requests_per_batch=configuration.max_requests_per_batch,
                )
                report = (
                    raw_report.safe_dict()
                    if hasattr(raw_report, "safe_dict")
                    else dict(raw_report)
                )
                success = report.get("status") == "ok"
            else:
                report = await day_runner(
                    source,
                    planner,
                    active_date,
                    max_requests=configuration.max_requests_per_batch,
                    work_db_path=configuration.memory_db_path,
                    repair_quality=False,
                    completed_before_active_date=cutoff,
                )
                success = report.get("status") == "ok" and bool(
                    report.get("temporary_commit_valid")
                )
            request_count += int(report.get("request_count") or 0)
            prompt_tokens += _usage_value(report, "prompt_tokens")
            completion_tokens += _usage_value(report, "completion_tokens")
            event_count += int(report.get("event_count") or 0)
            if not success:
                code = str(report.get("status") or "unknown")[:80]
                raise OnlineIngestError(
                    f"completed_day_{work_kind}_failed:{active_date}:{code}"
                )
            processed.append(active_date)
            if work_kind == "repair":
                repaired_count += 1
            else:
                ingested_count += 1

        refresh_status = "not_configured"
        if refresher is None:
            from .context_shadow import refresh_context_shadow_for_source_commit

            refresher = refresh_context_shadow_for_source_commit
        try:
            refresh = refresher(
                (configuration.memory_db_path, configuration.authority_db_path)
            )
            refresh_status = str(refresh.get("status") or "unknown")[:80]
        except Exception as exc:
            refresh_status = f"error:{type(exc).__name__}"

        return OnlineIngestResult(
            status="processed",
            trigger=safe_trigger,
            completed_before_active_date=cutoff,
            candidate_count=len(candidates),
            selected_count=len(selected),
            processed_count=len(processed),
            processed_active_dates=tuple(processed),
            request_count=request_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            event_count=event_count,
            repair_candidate_count=len(repair_candidates),
            repaired_count=repaired_count,
            ingested_count=ingested_count,
            refresh_status=refresh_status,
        )


async def ingest_completed_days_from_environment(
    *,
    completed_before_active_date: str,
    trigger: str,
) -> OnlineIngestResult:
    configuration = configuration_from_environment()
    if configuration is None:
        return OnlineIngestResult(
            status="disabled",
            trigger=str(trigger or "unspecified")[:80],
            completed_before_active_date=str(completed_before_active_date),
        )
    return await ingest_completed_days(
        configuration,
        completed_before_active_date=completed_before_active_date,
        trigger=trigger,
    )


__all__ = [
    "OnlineIngestConfiguration",
    "OnlineIngestError",
    "OnlineIngestResult",
    "configuration_from_environment",
    "conservative_completed_before_active_date",
    "ingest_completed_days",
    "ingest_completed_days_from_environment",
    "online_ingest_enabled",
]
