"""Whole-day Memory V2 experiment orchestration with no production writes."""

from __future__ import annotations

import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .conversation_source import ConversationSource
from .encoding import ParsedEncoding, assess_encoding_quality
from .linking import (
    LINKING_SCHEMA_NAME,
    MIN_CONTINUATION_CONFIDENCE,
    LinkingContractError,
    accepted_link_drafts,
    boundary_input_digest,
    build_boundary_judgment_draft,
    build_linking_messages,
    linking_response_schema,
    parse_linking_output,
    plan_boundary_links,
    screen_boundary_links,
)
from .replay import ReplayPlanner
from .store import MemoryV2Store


def _day_commit_valid(
    store: MemoryV2Store,
    source: ConversationSource,
    day: Any,
    parsed_batches: list[ParsedEncoding],
    boundary_reports: list[dict[str, Any]],
) -> bool:
    """Verify only this day's immutable scope inside a multi-day work store."""

    if len(parsed_batches) != len(day.batches):
        return False
    committed_event_count = 0
    for plan, parsed in zip(day.batches, parsed_batches):
        cached = store.load_completed_events(
            plan.batch,
            batch_sources=plan.batch_sources,
            batch_source_validator=source.validate_batch_sources,
        )
        if cached is None or len(cached) != len(parsed.events):
            return False
        committed_event_count += len(cached)
    receipt_count = sum(
        store.get_boundary_judgment(item["boundary"]["boundary_id"]) is not None
        for item in boundary_reports
    )
    return (
        committed_event_count == sum(len(parsed.events) for parsed in parsed_batches)
        and receipt_count == max(0, len(day.batches) - 1)
    )


async def run_day_sample(
    source: ConversationSource,
    planner: ReplayPlanner,
    active_date: str,
    *,
    max_requests: int = 1,
    work_db_path: str | Path | None = None,
    repair_quality: bool = True,
) -> dict[str, Any]:
    """Encode each batch once and resume only from an explicitly isolated work DB."""

    # Kept local to avoid making the provider experiment runner depend on this
    # higher-level orchestration module.
    from .experiment import (
        _call_flash_once,
        _combined_usage,
        _commit_encoding,
        _encode_plan,
        _resolve_encoding_quality,
        _safe_usage,
    )

    day = planner.plan_active_date(active_date)
    base = {
        "active_date": active_date,
        "source_message_count": day.message_count,
        "estimated_source_tokens": day.estimated_tokens,
        "batch_count": len(day.batches),
        "boundary_count": max(0, len(day.batches) - 1),
        "work_store_persistent": work_db_path is not None,
    }
    if not day.batches:
        return {
            **base,
            "status": "empty_day",
            "temporary_commit_valid": False,
            "request_count": 0,
            "encoding_request_count": 0,
            "link_request_count": 0,
            "usage": {},
            "encoding_reports": [],
            "boundary_reports": [],
        }

    encoding_reports: list[dict[str, Any]] = []
    boundary_reports: list[dict[str, Any]] = []
    parsed_batches: list[Any] = []
    link_requests = 0
    link_usage: dict[str, Any] = {}

    with ExitStack() as stack:
        if work_db_path is None:
            temp_dir = stack.enter_context(tempfile.TemporaryDirectory())
            resolved_work_db = Path(temp_dir) / "memory_v2_day_experiment.db"
        else:
            resolved_work_db = Path(work_db_path)
        store = MemoryV2Store(resolved_work_db)
        for batch_index, plan in enumerate(day.batches, start=1):
            cached_events = store.load_completed_events(
                plan.batch,
                batch_sources=plan.batch_sources,
                batch_source_validator=source.validate_batch_sources,
            )
            if cached_events is not None:
                parsed_batches.append(ParsedEncoding(events=cached_events))
                cached_quality = assess_encoding_quality(
                    parsed_batches[-1], plan
                ).safe_dict()
                if repair_quality and cached_quality["review_required"]:
                    encoding_reports.append(
                        {
                            "active_date": active_date,
                            "batch_number": batch_index,
                            "batch_id": plan.batch.batch_id,
                            "source_message_count": plan.batch.source_count,
                            "estimated_source_tokens": plan.estimated_tokens,
                            "status": "quality_cache_conflict",
                            "error": "cached encoding lacks a clean quality resolution",
                            "request_count": 0,
                            "usage": {},
                            "event_count": len(cached_events),
                            "quality": cached_quality,
                        }
                    )
                    return {
                        **base,
                        "status": "batch_encoding_error",
                        "failed_batch_number": batch_index,
                        "error": "cached encoding lacks a clean quality resolution",
                        "temporary_commit_valid": False,
                        "request_count": 0,
                        "encoding_request_count": 0,
                        "link_request_count": 0,
                        "usage": {},
                        "encoding_reports": encoding_reports,
                        "boundary_reports": boundary_reports,
                    }
                encoding_reports.append(
                    {
                        "active_date": active_date,
                        "batch_number": batch_index,
                        "batch_id": plan.batch.batch_id,
                        "source_message_count": plan.batch.source_count,
                        "estimated_source_tokens": plan.estimated_tokens,
                        "status": "cached",
                        "request_count": 0,
                        "usage": {},
                        "event_count": len(cached_events),
                        "quality": cached_quality,
                        "quality_resolution": {
                            "status": "cached_clean",
                        },
                    }
                )
                continue
            report, parsed = await _encode_plan(
                source,
                plan,
                {
                    "active_date": active_date,
                    "batch_number": batch_index,
                    "batch_id": plan.batch.batch_id,
                    "source_message_count": plan.batch.source_count,
                    "estimated_source_tokens": plan.estimated_tokens,
                },
                max_requests=max_requests,
            )
            if parsed is not None:
                report, parsed = await _resolve_encoding_quality(
                    source,
                    plan,
                    report,
                    parsed,
                    enabled=repair_quality,
                )
            encoding_reports.append(report)
            if parsed is None:
                encoding_requests = sum(
                    int(item.get("request_count", 0)) for item in encoding_reports
                )
                return {
                    **base,
                    "status": "batch_encoding_error",
                    "failed_batch_number": batch_index,
                    "error": report.get("error", "encoding_failed"),
                    "temporary_commit_valid": False,
                    "request_count": encoding_requests,
                    "encoding_request_count": encoding_requests,
                    "link_request_count": 0,
                    "usage": _combined_usage(
                        *(item.get("usage", {}) for item in encoding_reports)
                    ),
                    "encoding_reports": encoding_reports,
                    "boundary_reports": boundary_reports,
                }
            _commit_encoding(store, source, plan, parsed)
            parsed_batches.append(parsed)

        encoding_requests = sum(
            int(item.get("request_count", 0)) for item in encoding_reports
        )
        encoding_usage = _combined_usage(
            *(item.get("usage", {}) for item in encoding_reports)
        )

        for boundary_index in range(len(day.batches) - 1):
            previous_plan = day.batches[boundary_index]
            next_plan = day.batches[boundary_index + 1]
            previous = parsed_batches[boundary_index]
            following = parsed_batches[boundary_index + 1]
            link_plan = plan_boundary_links(
                previous_plan,
                previous.events,
                next_plan,
                following.events,
                previous_messages=source.read_batch(previous_plan.batch_sources),
                next_messages=source.read_batch(next_plan.batch_sources),
            )
            screening = screen_boundary_links(link_plan)

            existing_judgment = store.get_boundary_judgment(link_plan.boundary_id)
            if existing_judgment is not None:
                if existing_judgment["input_digest"] != boundary_input_digest(link_plan):
                    return {
                        **base,
                        "status": "boundary_cache_conflict",
                        "failed_boundary_after_batch": boundary_index + 1,
                        "error": "stored_boundary_input_digest_mismatch",
                        "temporary_commit_valid": False,
                        "request_count": encoding_requests + link_requests,
                        "encoding_request_count": encoding_requests,
                        "link_request_count": link_requests,
                        "usage": _combined_usage(encoding_usage, link_usage),
                        "encoding_reports": encoding_reports,
                        "boundary_reports": boundary_reports,
                    }
                outcome = str(existing_judgment["outcome"])
                boundary_reports.append(
                    {
                        "boundary_after_batch": boundary_index + 1,
                        "status": "cached",
                        "boundary": link_plan.safe_dict(),
                        "judgment_outcome": outcome,
                        "proposed_continuation_count": int(
                            outcome in {"below_threshold", "accepted"}
                        ),
                        "accepted_continuation_count": int(outcome == "accepted"),
                        "low_confidence_count": int(outcome == "below_threshold"),
                        "ignored_link_fields": [],
                        "screening": screening.safe_dict(),
                        "provider": {"status": "cached", "usage": {}},
                    }
                )
                continue

            parsed_links = None
            provider_report: dict[str, Any] = {
                "status": (
                    "screened_no_candidate"
                    if link_plan.previous_events
                    and link_plan.next_events
                    and not screening.requires_model
                    else "skipped_empty_side"
                ),
                "usage": {},
                "raw_output_chars": 0,
            }
            if screening.requires_model:
                provider = await _call_flash_once(
                    build_linking_messages(link_plan),
                    max_tokens=1_024,
                    schema_name=LINKING_SCHEMA_NAME,
                    schema=linking_response_schema(),
                    usage_tag="memory_v2_day_link_experiment",
                )
                link_requests += 1
                link_usage = _combined_usage(link_usage, provider.usage)
                provider_report = {
                    "status": provider.status,
                    "model": provider.model,
                    "duration_ms": provider.duration_ms,
                    "finish_reason": provider.finish_reason,
                    "usage": _safe_usage(provider.usage) or {},
                    "raw_output_chars": len(provider.raw_content),
                }
                if provider.status != "ok":
                    boundary_reports.append(
                        {
                            "boundary_after_batch": boundary_index + 1,
                            "status": "provider_error",
                            "boundary": link_plan.safe_dict(),
                            "provider": provider_report,
                        }
                    )
                    return {
                        **base,
                        "status": "boundary_provider_error",
                        "failed_boundary_after_batch": boundary_index + 1,
                        "error": provider.error or "missing_parsed_output",
                        "temporary_commit_valid": False,
                        "request_count": encoding_requests + link_requests,
                        "encoding_request_count": encoding_requests,
                        "link_request_count": link_requests,
                        "usage": _combined_usage(encoding_usage, link_usage),
                        "encoding_reports": encoding_reports,
                        "boundary_reports": boundary_reports,
                    }
                try:
                    parsed_links = parse_linking_output(
                        provider.raw_content, link_plan
                    )
                except LinkingContractError as exc:
                    boundary_reports.append(
                        {
                            "boundary_after_batch": boundary_index + 1,
                            "status": "contract_error",
                            "boundary": link_plan.safe_dict(),
                            "provider": provider_report,
                        }
                    )
                    return {
                        **base,
                        "status": "boundary_contract_error",
                        "failed_boundary_after_batch": boundary_index + 1,
                        "error": str(exc),
                        "temporary_commit_valid": False,
                        "request_count": encoding_requests + link_requests,
                        "encoding_request_count": encoding_requests,
                        "link_request_count": link_requests,
                        "usage": _combined_usage(encoding_usage, link_usage),
                        "encoding_reports": encoding_reports,
                        "boundary_reports": boundary_reports,
                    }

            drafts = (
                accepted_link_drafts(parsed_links, link_plan) if parsed_links else ()
            )
            judgment = build_boundary_judgment_draft(
                link_plan,
                parsed_links,
                skipped_empty_side=(
                    parsed_links is None
                    and (not link_plan.previous_events or not link_plan.next_events)
                ),
                screened_no_candidate=(
                    screening
                    if parsed_links is None
                    and link_plan.previous_events
                    and link_plan.next_events
                    else None
                ),
            )
            store.commit_boundary_judgment(
                judgment, drafts[0] if drafts else None
            )
            proposed = parsed_links.judgments if parsed_links else ()
            boundary_reports.append(
                {
                    "boundary_after_batch": boundary_index + 1,
                    "status": "ok",
                    "boundary": link_plan.safe_dict(),
                    "judgment_outcome": judgment.outcome,
                    "proposed_continuation_count": len(proposed),
                    "accepted_continuation_count": len(drafts),
                    "low_confidence_count": len(proposed) - len(drafts),
                    "ignored_link_fields": (
                        list(parsed_links.ignored_link_fields) if parsed_links else []
                    ),
                    "screening": screening.safe_dict(),
                    "provider": provider_report,
                }
            )

        outcome_counts = Counter(
            item["judgment_outcome"] for item in boundary_reports
        )
        accepted_count = int(outcome_counts.get("accepted", 0))
        receipt_count = sum(
            store.get_boundary_judgment(item["boundary"]["boundary_id"])
            is not None
            for item in boundary_reports
        )
        committed = _day_commit_valid(
            store,
            source,
            day,
            parsed_batches,
            boundary_reports,
        )
        review_required_batch_count = sum(
            bool(item.get("quality", {}).get("review_required"))
            for item in encoding_reports
        )

    return {
        **base,
        "status": "ok",
        "contract_valid": True,
        "temporary_commit_valid": committed,
        "request_count": encoding_requests + link_requests,
        "encoding_request_count": encoding_requests,
        "link_request_count": link_requests,
        "usage": _combined_usage(encoding_usage, link_usage),
        "event_count": sum(len(parsed.events) for parsed in parsed_batches),
        "review_required_batch_count": review_required_batch_count,
        "quality_review_required": review_required_batch_count > 0,
        "boundary_receipt_count": receipt_count,
        "accepted_continuation_count": accepted_count,
        "boundary_outcome_counts": dict(sorted(outcome_counts.items())),
        "encoding_reports": encoding_reports,
        "boundary_reports": boundary_reports,
        "minimum_continuation_confidence": MIN_CONTINUATION_CONFIDENCE,
    }
