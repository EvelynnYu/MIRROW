"""Deterministic, budgeted orchestration for isolated Memory V2 backfill.

The command-line entrypoint is deliberately planning-only.  Provider calls
require a programmatic execution with an explicit isolated root, so inventory
or operator mistakes cannot write the production conversation authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .conversation_source import ConversationSource
from .day_backfill_runner import run_backfill_day
from .day_settlement import DAY_SETTLEMENT_VERSION, resolve_day_settlement_id
from .day_settlement_assembler import DaySettlementPlanAssembler
from .day_settlement_runner import DAY_SETTLEMENT_PROMPT_VERSION
from .encoding import ParsedEncoding, assess_encoding_quality
from .replay import ReplayDayPlan, ReplayPlanner
from .store import MemoryV2Store


DayRunner = Callable[..., Awaitable[dict[str, Any]]]


def _day_input_digest(day: ReplayDayPlan) -> str:
    payload = {
        "active_date": day.active_date,
        "roles": list(day.roles),
        "batches": [
            {
                "batch_id": plan.batch.batch_id,
                "source_digest": plan.batch.source_digest,
                "encoder_version": plan.batch.encoder_version,
                "prompt_version": plan.batch.prompt_version,
            }
            for plan in day.batches
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BackfillDayPlan:
    active_date: str
    completed_before_active_date: str
    message_count: int
    estimated_source_tokens: int
    batch_count: int
    settlement_count: int
    session_count: int
    input_digest: str

    @property
    def base_request_ceiling(self) -> int:
        return self.batch_count + self.settlement_count

    def safe_dict(self) -> dict[str, Any]:
        return {
            "active_date": self.active_date,
            "completed_before_active_date": self.completed_before_active_date,
            "message_count": self.message_count,
            "estimated_source_tokens": self.estimated_source_tokens,
            "batch_count": self.batch_count,
            "settlement_count": self.settlement_count,
            "session_count": self.session_count,
            "base_request_ceiling": self.base_request_ceiling,
            "input_digest": self.input_digest,
        }


@dataclass(frozen=True)
class BackfillWavePlan:
    wave_number: int
    days: tuple[BackfillDayPlan, ...]

    @property
    def base_request_ceiling(self) -> int:
        return sum(day.base_request_ceiling for day in self.days)

    @property
    def estimated_source_tokens(self) -> int:
        return sum(day.estimated_source_tokens for day in self.days)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "wave_number": self.wave_number,
            "date_from": self.days[0].active_date if self.days else "",
            "date_to": self.days[-1].active_date if self.days else "",
            "day_count": len(self.days),
            "message_count": sum(day.message_count for day in self.days),
            "estimated_source_tokens": self.estimated_source_tokens,
            "base_request_ceiling": self.base_request_ceiling,
            "days": [day.safe_dict() for day in self.days],
        }


@dataclass(frozen=True)
class BackfillPlan:
    days: tuple[BackfillDayPlan, ...]
    waves: tuple[BackfillWavePlan, ...]
    max_base_requests_per_wave: int
    max_source_tokens_per_wave: int

    def safe_dict(self) -> dict[str, Any]:
        return {
            "day_count": len(self.days),
            "message_count": sum(day.message_count for day in self.days),
            "estimated_source_tokens": sum(
                day.estimated_source_tokens for day in self.days
            ),
            "batch_count": sum(day.batch_count for day in self.days),
            "settlement_count": sum(day.settlement_count for day in self.days),
            "base_request_ceiling": sum(
                day.base_request_ceiling for day in self.days
            ),
            "wave_count": len(self.waves),
            "max_base_requests_per_wave": self.max_base_requests_per_wave,
            "max_source_tokens_per_wave": self.max_source_tokens_per_wave,
            "waves": [wave.safe_dict() for wave in self.waves],
        }


@dataclass(frozen=True)
class BackfillDayProgress:
    active_date: str
    status: str
    completed_batches: int
    batch_count: int
    completed_settlements: int
    settlement_count: int
    quality_clean: bool
    error_type: str = ""

    def safe_dict(self) -> dict[str, Any]:
        return {
            "active_date": self.active_date,
            "status": self.status,
            "completed_batches": self.completed_batches,
            "batch_count": self.batch_count,
            "completed_settlements": self.completed_settlements,
            "settlement_count": self.settlement_count,
            "quality_clean": self.quality_clean,
            "error_type": self.error_type,
        }


def _active_dates(source: ConversationSource) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                message.active_date
                for message in source.read_all()
                if message.active_date
            }
        )
    )


def plan_backfill(
    planner: ReplayPlanner,
    *,
    active_dates: Sequence[str] | None = None,
    completed_before_active_date: str | None = None,
    max_base_requests_per_wave: int = 60,
    max_source_tokens_per_wave: int = 60_000,
) -> BackfillPlan:
    """Partition whole days into deterministic, bounded execution waves."""

    if max_base_requests_per_wave <= 0 or max_source_tokens_per_wave <= 0:
        raise ValueError("backfill wave budgets must be positive")
    cutoff = completed_before_active_date or datetime.now(
        timezone(timedelta(hours=8))
    ).date().isoformat()
    try:
        cutoff_date = date.fromisoformat(cutoff)
    except ValueError as exc:
        raise ValueError("completed_before_active_date must be an ISO date") from exc
    requested_dates = tuple(
        sorted(dict.fromkeys(active_dates or _active_dates(planner.source)))
    )
    try:
        dates = tuple(
            active_date
            for active_date in requested_dates
            if date.fromisoformat(active_date) < cutoff_date
        )
    except ValueError as exc:
        raise ValueError("backfill active dates must use ISO YYYY-MM-DD") from exc
    days: list[BackfillDayPlan] = []
    for active_date in dates:
        replay_day = planner.plan_active_date(active_date)
        if not replay_day.batches:
            continue
        days.append(
            BackfillDayPlan(
                active_date=active_date,
                completed_before_active_date=cutoff,
                message_count=replay_day.message_count,
                estimated_source_tokens=replay_day.estimated_tokens,
                batch_count=len(replay_day.batches),
                settlement_count=1,
                session_count=replay_day.session_count,
                input_digest=_day_input_digest(replay_day),
            )
        )

    grouped: list[tuple[BackfillDayPlan, ...]] = []
    current: list[BackfillDayPlan] = []
    current_requests = 0
    current_tokens = 0
    for day in days:
        would_exceed = bool(current) and (
            current_requests + day.base_request_ceiling
            > max_base_requests_per_wave
            or current_tokens + day.estimated_source_tokens
            > max_source_tokens_per_wave
        )
        if would_exceed:
            grouped.append(tuple(current))
            current = []
            current_requests = 0
            current_tokens = 0
        current.append(day)
        current_requests += day.base_request_ceiling
        current_tokens += day.estimated_source_tokens
    if current:
        grouped.append(tuple(current))

    waves = tuple(
        BackfillWavePlan(wave_number=index, days=items)
        for index, items in enumerate(grouped, start=1)
    )
    return BackfillPlan(
        days=tuple(days),
        waves=waves,
        max_base_requests_per_wave=max_base_requests_per_wave,
        max_source_tokens_per_wave=max_source_tokens_per_wave,
    )


def inspect_backfill_progress(
    source: ConversationSource,
    planner: ReplayPlanner,
    plan: BackfillPlan,
    *,
    work_db_path: str | Path,
) -> tuple[BackfillDayProgress, ...]:
    """Rebuild completion from immutable batch plus day-settlement receipts."""

    path = Path(work_db_path)
    store = MemoryV2Store(path) if path.is_file() else None
    progress: list[BackfillDayProgress] = []
    for day_plan in plan.days:
        replay_day = planner.plan_active_date(day_plan.active_date)
        if _day_input_digest(replay_day) != day_plan.input_digest:
            progress.append(
                BackfillDayProgress(
                    active_date=day_plan.active_date,
                    status="source_changed",
                    completed_batches=0,
                    batch_count=day_plan.batch_count,
                    completed_settlements=0,
                    settlement_count=day_plan.settlement_count,
                    quality_clean=False,
                )
            )
            continue
        if store is None:
            progress.append(
                BackfillDayProgress(
                    active_date=day_plan.active_date,
                    status="pending",
                    completed_batches=0,
                    batch_count=day_plan.batch_count,
                    completed_settlements=0,
                    settlement_count=day_plan.settlement_count,
                    quality_clean=True,
                )
            )
            continue
        try:
            parsed: list[ParsedEncoding | None] = []
            quality_clean = True
            for batch in replay_day.batches:
                cached = store.load_completed_events(
                    batch.batch,
                    batch_sources=batch.batch_sources,
                    batch_source_validator=source.validate_batch_sources,
                )
                item = ParsedEncoding(events=cached) if cached is not None else None
                parsed.append(item)
                if item is not None and assess_encoding_quality(item, batch).review_required:
                    quality_clean = False
            completed_batches = sum(item is not None for item in parsed)
            completed_settlements = 0
            if completed_batches == day_plan.batch_count:
                assembly = DaySettlementPlanAssembler(path).assemble(
                    active_date=day_plan.active_date,
                    completed_before_active_date=day_plan.completed_before_active_date,
                )
                if not assembly.ready:
                    completed_settlements = int(
                        assembly.skip_reason == "no_active_events"
                    )
                else:
                    day_settlement = assembly.plan
                    assert day_settlement is not None
                    receipt = store.get_day_settlement_receipt_for_day(
                        source_namespace=day_settlement.source_namespace,
                        active_date=day_settlement.active_date,
                        settlement_version=DAY_SETTLEMENT_VERSION,
                        prompt_version=DAY_SETTLEMENT_PROMPT_VERSION,
                    )
                    if receipt is not None and (
                        receipt["id"] != resolve_day_settlement_id(day_settlement)
                        or receipt["source_digest"] != assembly.source_digest
                    ):
                        raise RuntimeError("day settlement receipt conflicts with source")
                    completed_settlements = int(receipt is not None)
            complete = (
                completed_batches == day_plan.batch_count
                and completed_settlements == day_plan.settlement_count
            )
            status = "complete" if complete else (
                "partial"
                if completed_batches or completed_settlements
                else "pending"
            )
            progress.append(
                BackfillDayProgress(
                    active_date=day_plan.active_date,
                    status=status,
                    completed_batches=completed_batches,
                    batch_count=day_plan.batch_count,
                    completed_settlements=completed_settlements,
                    settlement_count=day_plan.settlement_count,
                    quality_clean=quality_clean,
                )
            )
        except Exception as exc:
            progress.append(
                BackfillDayProgress(
                    active_date=day_plan.active_date,
                    status="conflict",
                    completed_batches=0,
                    batch_count=day_plan.batch_count,
                    completed_settlements=0,
                    settlement_count=day_plan.settlement_count,
                    quality_clean=False,
                    error_type=type(exc).__name__,
                )
            )
    return tuple(progress)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _failure_shape(report: dict[str, Any]) -> tuple[str, str, str]:
    """Return content-free failure routing without copying provider or contract text."""

    status = str(report.get("status") or "error")
    encoding_reports = report.get("encoding_reports")
    if status == "batch_encoding_error" and isinstance(encoding_reports, list):
        last = encoding_reports[-1] if encoding_reports else {}
        if isinstance(last, dict):
            return (
                "encoding",
                str(last.get("status") or status),
                str(last.get("provider_status") or ""),
            )
    settlement_report = report.get("settlement_report")
    if status.startswith("day_settlement_") and isinstance(
        settlement_report, dict
    ):
        return (
            "day_settlement",
            str(settlement_report.get("error_code") or status),
            str(settlement_report.get("status") or ""),
        )
    return ("day", status, "")


async def execute_backfill_wave(
    source: ConversationSource,
    planner: ReplayPlanner,
    wave: BackfillWavePlan,
    *,
    work_db_path: str | Path,
    isolated_root: str | Path,
    max_actual_requests: int,
    per_batch_max_requests: int = 1,
    repair_quality: bool = False,
    day_runner: DayRunner = run_backfill_day,
) -> dict[str, Any]:
    """Execute complete days without starting one that can exceed the request cap."""

    if max_actual_requests <= 0:
        raise ValueError("max_actual_requests must be positive")
    if per_batch_max_requests <= 0:
        raise ValueError("per_batch_max_requests must be positive")
    work_path = Path(work_db_path).resolve()
    safe_root = Path(isolated_root).resolve()
    authority_path = Path(source.db_path).resolve()
    if work_path == authority_path or not _inside(work_path, safe_root):
        raise ValueError("backfill work database must stay inside isolated_root")
    work_path.parent.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    actual_requests = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    status = "complete"
    unstarted_day = ""
    unstarted_day_request_ceiling = 0
    progress_plan = BackfillPlan(
        days=wave.days,
        waves=(wave,),
        max_base_requests_per_wave=max(
            1, sum(day.base_request_ceiling for day in wave.days)
        ),
        max_source_tokens_per_wave=max(
            1, sum(day.estimated_source_tokens for day in wave.days)
        ),
    )
    progress_by_date = {
        item.active_date: item
        for item in inspect_backfill_progress(
            source,
            planner,
            progress_plan,
            work_db_path=work_path,
        )
    }
    for day in wave.days:
        current = planner.plan_active_date(day.active_date)
        if _day_input_digest(current) != day.input_digest:
            status = "source_changed"
            reports.append(
                {
                    "active_date": day.active_date,
                    "status": "source_changed",
                    "request_count": 0,
                }
            )
            break
        day_progress = progress_by_date[day.active_date]
        missing_batches = max(
            0, day_progress.batch_count - day_progress.completed_batches
        )
        missing_settlements = max(
            0,
            day_progress.settlement_count
            - day_progress.completed_settlements,
        )
        quality_request_ceiling = 2 if repair_quality else 0
        day_request_ceiling = (
            missing_batches
            * (per_batch_max_requests + quality_request_ceiling)
            + missing_settlements
        )
        if actual_requests + day_request_ceiling > max_actual_requests:
            status = "request_budget_reached"
            unstarted_day = day.active_date
            unstarted_day_request_ceiling = day_request_ceiling
            break
        report = await day_runner(
            source,
            planner,
            day.active_date,
            max_requests=per_batch_max_requests,
            work_db_path=work_path,
            repair_quality=repair_quality,
            completed_before_active_date=day.completed_before_active_date,
        )
        request_count = int(report.get("request_count") or 0)
        actual_requests += request_count
        day_usage = report.get("usage") or {}
        for key in usage:
            usage[key] += int(day_usage.get(key) or 0)
        valid = bool(report.get("temporary_commit_valid"))
        failure_stage, failure_code, provider_status = _failure_shape(report)
        settlement_report = report.get("settlement_report")
        normalization_codes = (
            settlement_report.get("normalization_codes", [])
            if isinstance(settlement_report, dict)
            else []
        )
        reports.append(
            {
                "active_date": day.active_date,
                "status": str(report.get("status") or "error"),
                "commit_valid": valid,
                "request_count": request_count,
                "encoding_request_count": int(
                    report.get("encoding_request_count") or 0
                ),
                "settlement_request_count": int(
                    report.get("settlement_request_count") or 0
                ),
                "event_count": int(report.get("event_count") or 0),
                "quality_review_required": bool(
                    report.get("quality_review_required")
                ),
                "request_ceiling_before_run": day_request_ceiling,
                "failure_stage": "" if valid else failure_stage,
                "failure_code": "" if valid else failure_code,
                "provider_status": "" if valid else provider_status,
                "normalization_codes": [
                    str(code) for code in normalization_codes
                ],
                "error_type": (
                    str(report.get("status") or "error")
                    if report.get("status") != "ok"
                    else ""
                ),
            }
        )
        if report.get("status") != "ok" or not valid:
            status = "stopped_on_error"
            break
    completed_day_count = sum(
        item["status"] == "ok" and item["commit_valid"] for item in reports
    )
    shadow_refresh: dict[str, Any] = {
        "status": "not_requested",
        "injected": False,
    }
    if completed_day_count:
        try:
            from .context_shadow import refresh_context_shadow_for_source_commit

            shadow_refresh = refresh_context_shadow_for_source_commit((work_path,))
        except Exception as exc:
            # Shadow visibility must never turn a completed isolated backfill
            # transaction into a failure.  A later refresh can rebuild it.
            shadow_refresh = {
                "status": "error",
                "injected": False,
                "error_type": type(exc).__name__,
            }
    return {
        "status": status,
        "wave_number": wave.wave_number,
        "planned_day_count": len(wave.days),
        "completed_day_count": completed_day_count,
        "actual_request_count": actual_requests,
        "usage": usage,
        "day_reports": reports,
        "request_budget": max_actual_requests,
        "request_budget_remaining": max(0, max_actual_requests - actual_requests),
        "unstarted_day": unstarted_day,
        "unstarted_day_request_ceiling": unstarted_day_request_ceiling,
        "shadow_refresh": shadow_refresh,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Plan Memory V2 backfill without provider calls")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--max-base-requests", type=int, default=60)
    parser.add_argument("--max-source-tokens", type=int, default=60_000)
    args = parser.parse_args()
    source = ConversationSource(args.source_db)
    plan = plan_backfill(
        ReplayPlanner(source),
        max_base_requests_per_wave=args.max_base_requests,
        max_source_tokens_per_wave=args.max_source_tokens,
    )
    print(json.dumps(plan.safe_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "BackfillDayPlan",
    "BackfillDayProgress",
    "BackfillPlan",
    "BackfillWavePlan",
    "execute_backfill_wave",
    "inspect_backfill_progress",
    "plan_backfill",
]
