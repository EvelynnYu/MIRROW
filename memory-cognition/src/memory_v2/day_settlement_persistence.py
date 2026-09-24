"""Prepare a validated, content-bounded atomic day-settlement write."""

from __future__ import annotations

import hashlib
import json

from .day_settlement import (
    MIN_CONTINUATION_CONFIDENCE,
    DaySettlementPlan,
    day_settlement_input_digest,
    resolve_day_settlement_id,
    validate_day_settlement_plan,
)
from .day_settlement_assembler import DaySettlementAssembly
from .day_settlement_runner import (
    DAY_SETTLEMENT_PROMPT_VERSION,
    DaySettlementRunResult,
    day_settlement_execution_digest,
)
from .models import (
    DayCompactItemDraft,
    DaySettlementCandidateDraft,
    DaySettlementDraft,
    DayThreadJudgmentDraft,
    EventLinkDraft,
)


class DaySettlementPersistenceError(ValueError):
    """A run cannot be converted into an authoritative settlement write."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    body = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"


def _candidate_drafts(plan: DaySettlementPlan) -> tuple[DaySettlementCandidateDraft, ...]:
    day_by_ref = {event.ref: event for event in plan.day_events}
    thread_by_ref = {thread.ref: thread for thread in plan.prior_threads}
    result: list[DaySettlementCandidateDraft] = []
    for candidate in plan.continuation_candidates:
        if candidate.prior_thread_ref:
            thread = thread_by_ref[candidate.prior_thread_ref]
            match = next(
                event
                for event in thread.representative_events
                if event.ref == candidate.match_event_ref
            )
            from_event = thread.representative_events[-1]
            prior_thread_id = thread.thread_id
        else:
            match = day_by_ref[candidate.match_event_ref]
            from_event = match
            prior_thread_id = ""
        result.append(
            DaySettlementCandidateDraft(
                candidate_ref=candidate.ref,
                from_event_id=from_event.event_id,
                match_event_id=match.event_id,
                to_event_id=day_by_ref[candidate.to_day_event_ref].event_id,
                prior_thread_id=prior_thread_id,
            )
        )
    return tuple(result)


def prepare_day_settlement_draft(
    assembly: DaySettlementAssembly,
    run_result: DaySettlementRunResult,
    *,
    minimum_continuation_confidence: float = MIN_CONTINUATION_CONFIDENCE,
) -> DaySettlementDraft:
    """Freeze one successful run before the SQLite transaction begins."""

    if not 0 <= float(minimum_continuation_confidence) <= 1:
        raise DaySettlementPersistenceError(
            "minimum continuation confidence must be between 0 and 1"
        )
    if not assembly.ready or assembly.plan is None:
        raise DaySettlementPersistenceError("a skipped assembly cannot be persisted")
    if assembly.candidate_selection is None:
        raise DaySettlementPersistenceError("a ready assembly requires frozen candidates")
    if run_result.status != "completed" or run_result.parsed is None:
        raise DaySettlementPersistenceError("only a completed parsed run can be persisted")
    plan = assembly.plan
    validate_day_settlement_plan(plan)
    if (
        plan.active_date != assembly.active_date
        or plan.source_namespace != assembly.source_namespace
        or plan.source_digest != assembly.source_digest
        or tuple(plan.continuation_candidates)
        != tuple(assembly.candidate_selection.candidates)
    ):
        raise DaySettlementPersistenceError(
            "assembly identity does not match its frozen day-settlement plan"
        )
    input_digest = day_settlement_input_digest(plan)
    expected_execution_digest = day_settlement_execution_digest(plan)
    if (
        run_result.active_date != assembly.active_date
        or run_result.active_date != plan.active_date
        or run_result.source_namespace != assembly.source_namespace
        or run_result.source_namespace != plan.source_namespace
        or run_result.source_digest != assembly.source_digest
        or run_result.source_digest != plan.source_digest
        or run_result.execution_digest != expected_execution_digest
        or run_result.prompt_version != DAY_SETTLEMENT_PROMPT_VERSION
        or run_result.model_call_count != 1
    ):
        raise DaySettlementPersistenceError(
            "run identity does not match its frozen day-settlement assembly"
        )

    settlement_id = resolve_day_settlement_id(plan)
    candidates = _candidate_drafts(plan)
    candidate_by_ref = {item.candidate_ref: item for item in candidates}
    compact_items = tuple(
        DayCompactItemDraft(
            ordinal=item.ordinal,
            summary=item.summary,
            event_ids=item.event_ids,
            content_digest=_digest(
                {
                    "ordinal": item.ordinal,
                    "summary": item.summary,
                    "event_ids": list(item.event_ids),
                }
            ),
            item_id=_stable_id("day_item", settlement_id, item.ordinal),
        )
        for item in run_result.parsed.day_compact
    )

    judgments: list[DayThreadJudgmentDraft] = []
    result_judgments: list[dict[str, object]] = []
    for item in run_result.parsed.thread_judgments:
        candidate = candidate_by_ref.get(item.candidate_ref)
        if candidate is None or (
            item.from_event_id != candidate.from_event_id
            or item.match_event_id != candidate.match_event_id
            or item.to_event_id != candidate.to_event_id
            or item.prior_thread_id != candidate.prior_thread_id
        ):
            raise DaySettlementPersistenceError(
                "parsed judgment no longer matches its frozen candidate"
            )
        accepted_link = None
        if (
            item.outcome == "continue"
            and item.confidence >= minimum_continuation_confidence
        ):
            accepted_link = EventLinkDraft(
                from_event_id=item.from_event_id,
                to_event_id=item.to_event_id,
                link_type="continues",
                confidence=item.confidence,
                evidence={
                    "day_settlement_id": settlement_id,
                    "settlement_version": plan.settlement_version,
                    "prompt_version": run_result.prompt_version,
                    "input_digest": input_digest,
                    "execution_digest": run_result.execution_digest,
                    "candidate_ref": item.candidate_ref,
                    "match_event_id": item.match_event_id,
                    "reason_codes": list(item.reason_codes),
                },
            )
        judgments.append(
            DayThreadJudgmentDraft(
                candidate_ref=item.candidate_ref,
                outcome=item.outcome,
                confidence=item.confidence,
                reason_codes=item.reason_codes,
                from_event_id=item.from_event_id,
                match_event_id=item.match_event_id,
                to_event_id=item.to_event_id,
                prior_thread_id=item.prior_thread_id,
                accepted_link=accepted_link,
                judgment_id=_stable_id(
                    "day_judgment", settlement_id, item.candidate_ref
                ),
            )
        )
        result_judgments.append(
            {
                "candidate_ref": item.candidate_ref,
                "outcome": item.outcome,
                "confidence": item.confidence,
                "reason_codes": list(item.reason_codes),
                "from_event_id": item.from_event_id,
                "match_event_id": item.match_event_id,
                "to_event_id": item.to_event_id,
                "prior_thread_id": item.prior_thread_id,
                "accepted": accepted_link is not None,
            }
        )
    result_digest = _digest(
        {
            "day_compact": [
                {
                    "ordinal": item.ordinal,
                    "summary": item.summary,
                    "event_ids": list(item.event_ids),
                }
                for item in compact_items
            ],
            "thread_judgments": result_judgments,
        }
    )
    return DaySettlementDraft(
        settlement_id=settlement_id,
        source_namespace=plan.source_namespace,
        active_date=plan.active_date,
        settlement_version=plan.settlement_version,
        assembler_version=assembly.assembler_version,
        prompt_version=run_result.prompt_version,
        source_digest=plan.source_digest,
        input_digest=input_digest,
        execution_digest=run_result.execution_digest,
        result_digest=result_digest,
        minimum_continuation_confidence=float(minimum_continuation_confidence),
        day_event_ids=tuple(event.event_id for event in plan.day_events),
        candidates=candidates,
        compact_items=compact_items,
        judgments=tuple(judgments),
    )
