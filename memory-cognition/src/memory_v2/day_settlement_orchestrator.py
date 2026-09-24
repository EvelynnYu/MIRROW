"""Recoverable orchestration for one completed active-day settlement."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .day_settlement import day_settlement_input_digest, resolve_day_settlement_id
from .day_settlement_assembler import (
    DaySettlementAssemblyError,
    DaySettlementPlanAssembler,
)
from .day_settlement_persistence import prepare_day_settlement_draft
from .day_settlement_runner import (
    DEFAULT_DAY_SETTLEMENT_MAX_OUTPUT_TOKENS,
    DAY_SETTLEMENT_PROMPT_VERSION,
    DaySettlementModelCall,
    day_settlement_execution_digest,
    run_day_settlement,
)
from .store import MemoryV2Store


_SETTLEMENT_LOCKS: dict[tuple[str, str, str, int], asyncio.Lock] = {}


@dataclass(frozen=True)
class DaySettlementOrchestrationResult:
    status: str
    active_date: str
    source_namespace: str
    source_digest: str = ""
    settlement_id: str = ""
    execution_digest: str = ""
    model_call_count: int = 0
    receipt_persisted: bool = False
    compact_item_count: int = 0
    candidate_count: int = 0
    judgment_count: int = 0
    accepted_continuation_count: int = 0
    skip_reason: str = ""
    error_code: str = ""
    normalization_codes: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.status in {"committed", "cached", "skipped"}

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "active_date": self.active_date,
            "source_namespace": self.source_namespace,
            "source_digest": self.source_digest,
            "settlement_id": self.settlement_id,
            "execution_digest": self.execution_digest,
            "model_call_count": self.model_call_count,
            "receipt_persisted": self.receipt_persisted,
            "compact_item_count": self.compact_item_count,
            "candidate_count": self.candidate_count,
            "judgment_count": self.judgment_count,
            "unjudged_candidate_count": max(
                0, self.candidate_count - self.judgment_count
            ),
            "accepted_continuation_count": self.accepted_continuation_count,
            "skip_reason": self.skip_reason,
            "error_code": self.error_code,
            "normalization_codes": list(self.normalization_codes),
        }


def _lock_for(
    store: MemoryV2Store, source_namespace: str, active_date: str
) -> asyncio.Lock:
    key = (
        str(Path(store.db_path).resolve()),
        source_namespace,
        active_date,
        id(asyncio.get_running_loop()),
    )
    lock = _SETTLEMENT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SETTLEMENT_LOCKS[key] = lock
    return lock


def _receipt_matches(receipt: dict, *, plan: Any, assembly: Any) -> bool:
    return all(
        (
            receipt.get("id") == resolve_day_settlement_id(plan),
            receipt.get("source_namespace") == plan.source_namespace,
            receipt.get("active_date") == plan.active_date,
            receipt.get("settlement_version") == plan.settlement_version,
            receipt.get("assembler_version") == assembly.assembler_version,
            receipt.get("prompt_version") == DAY_SETTLEMENT_PROMPT_VERSION,
            receipt.get("source_digest") == plan.source_digest,
            receipt.get("input_digest") == day_settlement_input_digest(plan),
            receipt.get("execution_digest") == day_settlement_execution_digest(plan),
        )
    )


async def settle_completed_day(
    store: MemoryV2Store,
    *,
    active_date: str,
    completed_before_active_date: str,
    model_call: DaySettlementModelCall,
    source_namespace: str = "mainline",
    max_output_tokens: int = DEFAULT_DAY_SETTLEMENT_MAX_OUTPUT_TOKENS,
) -> DaySettlementOrchestrationResult:
    """Resume, run at most once, and atomically commit one completed active day."""

    async with _lock_for(store, source_namespace, active_date):
        try:
            assembly = DaySettlementPlanAssembler(store.db_path).assemble(
                active_date=active_date,
                completed_before_active_date=completed_before_active_date,
                source_namespace=source_namespace,
            )
        except DaySettlementAssemblyError as exc:
            return DaySettlementOrchestrationResult(
                status="assembly_error",
                active_date=active_date,
                source_namespace=source_namespace,
                error_code=type(exc).__name__,
            )
        if not assembly.ready:
            return DaySettlementOrchestrationResult(
                status="skipped",
                active_date=active_date,
                source_namespace=source_namespace,
                source_digest=assembly.source_digest,
                skip_reason=assembly.skip_reason,
            )

        plan = assembly.plan
        assert plan is not None
        settlement_id = resolve_day_settlement_id(plan)
        input_digest = day_settlement_input_digest(plan)
        execution_digest = day_settlement_execution_digest(plan)
        receipt = store.get_day_settlement_receipt_for_day(
            source_namespace=source_namespace,
            active_date=active_date,
            settlement_version=plan.settlement_version,
            prompt_version=DAY_SETTLEMENT_PROMPT_VERSION,
        )
        if receipt is not None:
            if not _receipt_matches(receipt, plan=plan, assembly=assembly):
                return DaySettlementOrchestrationResult(
                    status="receipt_conflict",
                    active_date=active_date,
                    source_namespace=source_namespace,
                    source_digest=plan.source_digest,
                    settlement_id=settlement_id,
                    execution_digest=execution_digest,
                    error_code="immutable_receipt_mismatch",
                )
            return DaySettlementOrchestrationResult(
                status="cached",
                active_date=active_date,
                source_namespace=source_namespace,
                source_digest=plan.source_digest,
                settlement_id=settlement_id,
                execution_digest=execution_digest,
                receipt_persisted=True,
                compact_item_count=int(receipt["compact_item_count"]),
                candidate_count=int(receipt["candidate_count"]),
                judgment_count=int(receipt["judgment_count"]),
                accepted_continuation_count=int(
                    receipt["accepted_continuation_count"]
                ),
            )

        run_result = await run_day_settlement(
            assembly,
            model_call=model_call,
            max_output_tokens=max_output_tokens,
        )
        if run_result.status != "completed" or run_result.parsed is None:
            return DaySettlementOrchestrationResult(
                status=run_result.status,
                active_date=active_date,
                source_namespace=source_namespace,
                source_digest=plan.source_digest,
                settlement_id=settlement_id,
                execution_digest=execution_digest,
                model_call_count=run_result.model_call_count,
                candidate_count=len(plan.continuation_candidates),
                error_code=run_result.error_code,
                normalization_codes=run_result.normalization_codes,
            )
        try:
            draft = prepare_day_settlement_draft(assembly, run_result)
            store.commit_day_settlement(draft)
        except Exception as exc:
            return DaySettlementOrchestrationResult(
                status="commit_error",
                active_date=active_date,
                source_namespace=source_namespace,
                source_digest=plan.source_digest,
                settlement_id=settlement_id,
                execution_digest=execution_digest,
                model_call_count=run_result.model_call_count,
                compact_item_count=len(run_result.parsed.day_compact),
                candidate_count=len(plan.continuation_candidates),
                judgment_count=len(run_result.parsed.thread_judgments),
                accepted_continuation_count=len(
                    run_result.parsed.accepted_continuations()
                ),
                error_code=type(exc).__name__,
                normalization_codes=run_result.normalization_codes,
            )
        return DaySettlementOrchestrationResult(
            status="committed",
            active_date=active_date,
            source_namespace=source_namespace,
            source_digest=plan.source_digest,
            settlement_id=settlement_id,
            execution_digest=execution_digest,
            model_call_count=run_result.model_call_count,
            receipt_persisted=True,
            compact_item_count=len(run_result.parsed.day_compact),
            candidate_count=len(plan.continuation_candidates),
            judgment_count=len(run_result.parsed.thread_judgments),
            accepted_continuation_count=len(
                run_result.parsed.accepted_continuations()
            ),
            normalization_codes=run_result.normalization_codes,
        )
