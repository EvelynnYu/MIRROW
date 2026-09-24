"""Small, privacy-safe Flash experiment runner for Memory V2 Phase 1."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .conversation_source import ConversationSource
from .encoding import (
    ENCODING_SCHEMA_NAME,
    EncodingContractError,
    assess_encoding_quality,
    build_encoding_messages,
    build_encoding_json_schema,
    measure_encoding,
    parse_encoding_output,
)
from .linking import (
    LINKING_SCHEMA_NAME,
    MIN_CONTINUATION_CONFIDENCE,
    LinkingContractError,
    accepted_link_drafts,
    build_boundary_judgment_draft,
    build_linking_messages,
    linking_response_schema,
    parse_linking_output,
    plan_boundary_links,
)
from .overflow import OverflowAssemblyError, encode_complete_tree
from .quality_review import (
    GROUPING_AUDITOR_VERSION,
    GROUPING_AUDIT_SCHEMA_NAME,
    GroupingAuditContractError,
    build_grouping_audit_messages,
    build_grouping_repair_instruction,
    grouping_audit_schema,
    parse_grouping_audit,
)
from .replay import ReplayPlanner
from .store import MemoryV2Store


@dataclass(frozen=True)
class _ProviderResult:
    status: str
    raw_content: str
    usage: dict[str, Any]
    model: str
    duration_ms: int
    finish_reason: str = ""
    error: str = ""


class _SampleAbort(RuntimeError):
    def __init__(self, status: str, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


def _responses_url(api_url: str) -> str:
    """Derive DeepSeek's Responses endpoint without exposing endpoint data."""

    value = api_url.rstrip("/")
    if value.endswith("/responses"):
        return value
    for suffix in (
        "/v1/chat/completions",
        "/beta/chat/completions",
        "/chat/completions",
    ):
        if value.endswith(suffix):
            return value[: -len(suffix)] + "/responses"
    raise ValueError("responses_endpoint_unavailable")


def _responses_output_text(body: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in body.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
    return "".join(texts)


def _responses_usage(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    prompt_tokens = int(raw.get("input_tokens") or 0)
    completion_tokens = int(raw.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(raw.get("total_tokens") or prompt_tokens + completion_tokens),
        "prompt_tokens_details": raw.get("input_tokens_details") or {},
        "completion_tokens_details": raw.get("output_tokens_details") or {},
    }


def _classify_response(
    response_status: str,
    incomplete_reason: str,
    content: str,
) -> tuple[str, str]:
    if response_status == "incomplete":
        error = "response_incomplete"
        if incomplete_reason in {"max_output_tokens", "content_filter"}:
            error += f":{incomplete_reason}"
        return "incomplete", error
    if content.strip():
        return "ok", ""
    return "empty_content", "model_returned_no_content"


def _build_responses_payload(
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    *,
    schema_name: str = ENCODING_SCHEMA_NAME,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "input": messages,
        "stream": False,
        "temperature": 0.0,
        "max_output_tokens": max_tokens,
        "reasoning": {"effort": "none"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema or build_encoding_json_schema(),
            }
        },
    }


async def _call_flash_once(
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    schema_name: str = ENCODING_SCHEMA_NAME,
    schema: dict[str, Any] | None = None,
    usage_tag: str = "memory_v2_experiment",
) -> _ProviderResult:
    """One transport attempt, with no prompt-body logging or parse retry."""

    import httpx

    from llm_client import (
        DEEPSEEK_API_KEY,
        DEEPSEEK_API_URL,
        DEEPSEEK_FLASH_API_KEY,
        DEEPSEEK_FLASH_API_URL,
        DEEPSEEK_FLASH_MODEL,
        LLM_LIMITS,
        LLM_LONG_TIMEOUT,
        http_client,
        llm_semaphore,
    )

    api_key = DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY
    api_url = DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL
    if not api_key:
        return _ProviderResult(
            status="configuration_error",
            raw_content="",
            usage={},
            model=DEEPSEEK_FLASH_MODEL,
            duration_ms=0,
            error="api_key_not_configured",
        )
    try:
        responses_api_url = _responses_url(api_url)
    except ValueError as exc:
        return _ProviderResult(
            status="configuration_error",
            raw_content="",
            usage={},
            model=DEEPSEEK_FLASH_MODEL,
            duration_ms=0,
            error=str(exc),
        )
    payload = _build_responses_payload(
        DEEPSEEK_FLASH_MODEL,
        messages,
        max_tokens,
        schema_name=schema_name,
        schema=schema,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=LLM_LONG_TIMEOUT,
        limits=LLM_LIMITS,
        proxy=None,
        trust_env=False,
    )
    started = time.perf_counter()
    try:
        async with llm_semaphore:
            response = await client.post(responses_api_url, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("response_body_is_not_an_object")
        content = _responses_output_text(body)
        usage = _responses_usage(body)
        try:
            from token_tracker import log_token_usage

            await log_token_usage(usage_tag, DEEPSEEK_FLASH_MODEL, usage)
        except Exception:
            pass
        response_status = str(body.get("status") or "")
        incomplete = body.get("incomplete_details")
        incomplete_reason = (
            str(incomplete.get("reason") or "")
            if isinstance(incomplete, dict)
            else ""
        )
        status, error = _classify_response(
            response_status,
            incomplete_reason,
            content,
        )
        return _ProviderResult(
            status=status,
            raw_content=content,
            usage=usage,
            model=DEEPSEEK_FLASH_MODEL,
            duration_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=response_status,
            error=error,
        )
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if owned_client:
            await client.aclose()
    return _ProviderResult(
        status="error",
        raw_content="",
        usage={},
        model=DEEPSEEK_FLASH_MODEL,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )


def _safe_usage(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): safe
            for key, item in value.items()
            if (safe := _safe_usage(item)) is not None
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _parse_sample(value: str) -> tuple[str, int]:
    try:
        active_date, batch_number = value.rsplit(":", 1)
        number = int(batch_number)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("sample must look like YYYY-MM-DD:NUMBER") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("batch number must be positive")
    return active_date, number


def _usage_totals(shards: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    return {
        key: sum(
            int(shard.get("usage", {}).get(key, 0))
            for shard in shards
            if isinstance(shard.get("usage"), dict)
        )
        for key in keys
    }


def _combined_usage(*usages: dict[str, Any]) -> dict[str, int]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    return {
        key: sum(int(usage.get(key, 0)) for usage in usages if isinstance(usage, dict))
        for key in keys
    }


def _event_counts(events: Any) -> dict[str, dict[str, int]]:
    facets = Counter(
        facet
        for event in events
        for facet in event.attributes.get("memory_facets", [])
    )
    return {
        "event_type_counts": dict(
            sorted(Counter(event.event_type for event in events).items())
        ),
        "aspect_type_counts": dict(
            sorted(
                Counter(
                    str(aspect.get("event_type") or "")
                    for event in events
                    for aspect in event.attributes.get("event_aspects", [])
                    if aspect.get("event_type")
                ).items()
            )
        ),
        "subject_counts": dict(
            sorted(Counter(event.subject_id for event in events).items())
        ),
        "epistemic_counts": dict(
            sorted(Counter(event.epistemic_status for event in events).items())
        ),
        "time_precision_counts": dict(
            sorted(
                Counter(
                    str(event.attributes.get("time_precision") or "")
                    for event in events
                ).items()
            )
        ),
        "memory_facet_counts": dict(sorted(facets.items())),
    }


def _event_shapes(events: Any, plan: Any) -> list[dict[str, Any]]:
    """Return content-free event grouping diagnostics for semantic review."""

    positions = {
        source.message_id: index
        for index, source in enumerate(plan.batch_sources, start=1)
    }
    source_manifest = {
        source.message_id: source for source in plan.batch_sources
    }

    def subject_kind(subject_id: str) -> str:
        value = str(subject_id or "")
        return value.split(":", 1)[0] if ":" in value else value

    shapes: list[dict[str, Any]] = []
    for event in events:
        source_positions = sorted(
            {
                positions[source.message_id]
                for source in event.sources
                if source.message_id in positions
            }
        )
        shapes.append(
            {
                "event_ordinal": event.ordinal,
                "subject_kind": subject_kind(event.subject_id),
                "participant_kinds": sorted(
                    {subject_kind(value) for value in event.participant_ids}
                ),
                "event_type": event.event_type,
                "source_kinds": sorted(
                    {
                        source_manifest[source.message_id].source_kind
                        for source in event.sources
                        if source.message_id in source_manifest
                    }
                ),
                "source_event_types": sorted(
                    {
                        source_manifest[source.message_id].source_event_type
                        for source in event.sources
                        if source.message_id in source_manifest
                        and source_manifest[source.message_id].source_event_type
                    }
                ),
                "aspects": [
                    {
                        "event_type": str(aspect.get("event_type") or ""),
                        "source_positions": sorted(
                            {
                                positions[message_id]
                                for message_id in aspect.get("source_message_ids", [])
                                if message_id in positions
                            }
                        ),
                    }
                    for aspect in event.attributes.get("event_aspects", [])
                ],
                "source_positions": source_positions,
                "source_count": len(source_positions),
            }
        )
    return shapes


def _provider_observation(provider: _ProviderResult) -> dict[str, Any]:
    """Return provider diagnostics without retaining generated memory content."""

    return {
        "status": provider.status,
        "model": provider.model,
        "duration_ms": provider.duration_ms,
        "finish_reason": provider.finish_reason,
        "usage": _safe_usage(provider.usage) or {},
        "raw_output_chars": len(provider.raw_content),
    }


def _local_source_positions(source_refs: Any) -> list[int]:
    """Normalize batch-local refs without exposing persistent message ids."""

    positions: list[int] = []
    for source_ref in source_refs:
        value = str(source_ref or "")
        if value.startswith("s") and value[1:].isdigit():
            positions.append(int(value[1:]))
    return sorted(set(positions))


def _parse_encoding_with_safe_narrowing(raw_content, shard_plan, messages):
    """Accept only the one projection that can remove unsupported aspect evidence."""

    try:
        return parse_encoding_output(raw_content, shard_plan, messages)
    except EncodingContractError as exc:
        if str(exc) != "aspect source refs must be members of the episode sources":
            raise
        return parse_encoding_output(
            raw_content,
            shard_plan,
            messages,
            allow_conservative_aspect_projection=True,
        )


async def _encode_plan(
    source: ConversationSource,
    plan: Any,
    base: dict[str, Any],
    *,
    max_requests: int = 1,
) -> tuple[dict[str, Any], Any | None]:
    shard_reports: list[dict[str, Any]] = []

    async def encode_shard(shard_plan, messages):
        prompt = build_encoding_messages(shard_plan, messages)
        contract_retries = 0
        while True:
            if len(shard_reports) >= max_requests:
                raise _SampleAbort("request_limit", "encoding_request_limit_reached")
            provider = await _call_flash_once(prompt, max_tokens=4_096)
            report: dict[str, Any] = {
                "batch_id": shard_plan.batch.batch_id,
                "shard_depth": shard_plan.shard_depth,
                "attempt_number": contract_retries + 1,
                "source_message_count": shard_plan.batch.source_count,
                "estimated_source_tokens": shard_plan.estimated_tokens,
                "provider_status": provider.status,
                "model": provider.model,
                "duration_ms": provider.duration_ms,
                "finish_reason": provider.finish_reason,
                "usage": _safe_usage(provider.usage) or {},
                "raw_output_chars": len(provider.raw_content),
            }
            if provider.status != "ok":
                report["status"] = "provider_error"
                report["error"] = provider.error or "missing_parsed_output"
                shard_reports.append(report)
                raise _SampleAbort(report["status"], report["error"])
            try:
                parsed = _parse_encoding_with_safe_narrowing(
                    provider.raw_content, shard_plan, messages
                )
            except EncodingContractError as exc:
                report["status"] = "contract_error"
                report["error"] = str(exc)
                shard_reports.append(report)
                if contract_retries == 0 and len(shard_reports) < max_requests:
                    contract_retries += 1
                    prompt = [dict(item) for item in prompt]
                    prompt[0]["content"] += (
                        " The previous attempt was rejected by local validation: "
                        f"{exc}. Re-encode the same source from scratch and use every exact "
                        "canonical field name; do not add aliases or commentary."
                    )
                    if str(exc) == "Human events require at least one Human source row":
                        prompt[0]["content"] += (
                            " Inspect every episode independently: primary_subject_id=human "
                            "requires a cited U row in that same episode. An episode whose "
                            "source_refs are all Agent rows must use k as primary subject when it "
                            "records Agent's sourced expression/action, or must be non-event."
                        )
                    continue
                raise _SampleAbort(report["status"], report["error"]) from exc
            report.update(
                {
                    "status": "ok",
                    "metrics": measure_encoding(parsed, shard_plan).safe_dict(),
                    "event_shapes": _event_shapes(parsed.events, shard_plan),
                    "non_event_source_positions": _local_source_positions(
                        parsed.non_event_source_refs
                    ),
                    "ignored_episode_fields": list(parsed.ignored_episode_fields),
                    "projected_episode_fields": list(
                        parsed.projected_episode_fields
                    ),
                    "provider_episode_count": parsed.provider_episode_count,
                    "backend_overflow_applied": parsed.backend_overflow_applied,
                    "backend_deferred_source_count": (
                        parsed.backend_deferred_source_count
                    ),
                    "backend_time_projection_count": (
                        parsed.backend_time_projection_count
                    ),
                    **_event_counts(parsed.events),
                }
            )
            shard_reports.append(report)
            return parsed

    try:
        merged, shards = await encode_complete_tree(
            source,
            plan,
            encode_shard,
            max_shards=max_requests,
        )
    except _SampleAbort as exc:
        return ({
            **base,
            "status": exc.status,
            "error": exc.error,
            "request_count": len(shard_reports),
            "usage": _usage_totals(shard_reports),
            "temporary_commit_valid": False,
            "shards": shard_reports,
        }, None)
    except OverflowAssemblyError as exc:
        return ({
            **base,
            "status": "assembly_error",
            "error": str(exc),
            "request_count": len(shard_reports),
            "usage": _usage_totals(shard_reports),
            "temporary_commit_valid": False,
            "shards": shard_reports,
        }, None)

    return ({
        **base,
        "status": "ok",
        "contract_valid": True,
        "request_count": len(shard_reports),
        "shard_count": len(shards),
        "usage": _usage_totals(shard_reports),
        "metrics": measure_encoding(merged, plan).safe_dict(),
        "quality": assess_encoding_quality(merged, plan).safe_dict(),
        "event_shapes": _event_shapes(merged.events, plan),
        "non_event_source_positions": _local_source_positions(
            merged.non_event_source_refs
        ),
        "ignored_episode_fields": list(merged.ignored_episode_fields),
        "projected_episode_fields": list(merged.projected_episode_fields),
        "provider_episode_count": merged.provider_episode_count,
        "backend_overflow_applied": merged.backend_overflow_applied,
        "backend_deferred_source_count": merged.backend_deferred_source_count,
        "backend_time_projection_count": merged.backend_time_projection_count,
        **_event_counts(merged.events),
        "shards": shard_reports,
    }, merged)


async def _resolve_encoding_quality(
    source: ConversationSource,
    plan: Any,
    report: dict[str, Any],
    parsed: Any,
    *,
    enabled: bool,
) -> tuple[dict[str, Any], Any | None]:
    """Audit an anomalous grouping once and permit at most one fresh re-encode."""

    initial_quality = report.get("quality")
    if not isinstance(initial_quality, dict):
        initial_quality = assess_encoding_quality(parsed, plan).safe_dict()
    if not enabled:
        return ({
            **report,
            "quality_resolution": {
                "status": "disabled",
                "auditor_version": GROUPING_AUDITOR_VERSION,
            },
        }, parsed)
    if not initial_quality.get("review_required"):
        return ({
            **report,
            "quality_resolution": {
                "status": "not_required",
                "auditor_version": GROUPING_AUDITOR_VERSION,
            },
        }, parsed)

    messages = source.read_batch(plan.batch_sources)
    audit_provider = await _call_flash_once(
        build_grouping_audit_messages(plan, messages, parsed),
        max_tokens=2_048,
        schema_name=GROUPING_AUDIT_SCHEMA_NAME,
        schema=grouping_audit_schema(len(parsed.events)),
        usage_tag="memory_v2_grouping_audit",
    )
    audit_observation = _provider_observation(audit_provider)
    total_usage = _combined_usage(report.get("usage", {}), audit_provider.usage)
    total_requests = int(report.get("request_count", 0)) + 1
    resolution: dict[str, Any] = {
        "status": "audit_pending",
        "auditor_version": GROUPING_AUDITOR_VERSION,
        "initial_event_count": len(parsed.events),
        "initial_review_reasons": list(
            initial_quality.get("review_reasons", [])
        ),
        "audit_provider": audit_observation,
    }

    def rejected(status: str, error: str) -> tuple[dict[str, Any], None]:
        resolution["status"] = status
        return ({
            **report,
            "status": status,
            "error": error,
            "request_count": total_requests,
            "usage": total_usage,
            "temporary_commit_valid": False,
            "quality_resolution": resolution,
        }, None)

    if audit_provider.status != "ok":
        return rejected(
            "quality_audit_provider_error",
            audit_provider.error or "grouping_audit_missing_output",
        )
    try:
        audit = parse_grouping_audit(
            audit_provider.raw_content,
            len(parsed.events),
        )
    except GroupingAuditContractError as exc:
        return rejected("quality_audit_contract_error", str(exc))
    resolution["audit"] = audit.safe_dict()
    if audit.verdict != "over_fragmented":
        return rejected(
            "quality_manual_review_required",
            f"grouping_audit_{audit.verdict}",
        )

    repair_prompt = build_encoding_messages(plan, messages)
    repair_prompt = [dict(item) for item in repair_prompt]
    repair_prompt[0]["content"] += build_grouping_repair_instruction(
        plan, parsed, audit
    )
    repair_provider = await _call_flash_once(
        repair_prompt,
        max_tokens=4_096,
        schema_name=ENCODING_SCHEMA_NAME,
        schema=build_encoding_json_schema(),
        usage_tag="memory_v2_grouping_repair",
    )
    total_requests += 1
    total_usage = _combined_usage(total_usage, repair_provider.usage)
    resolution["repair_provider"] = _provider_observation(repair_provider)
    if repair_provider.status != "ok":
        return rejected(
            "quality_repair_provider_error",
            repair_provider.error or "grouping_repair_missing_output",
        )
    try:
        repaired = parse_encoding_output(
            repair_provider.raw_content,
            plan,
            messages,
            allow_conservative_aspect_projection=True,
        )
    except EncodingContractError as exc:
        return rejected("quality_repair_contract_error", str(exc))
    if repaired.overflow:
        return rejected(
            "quality_repair_overflow",
            "one-shot grouping repair may not start an overflow tree",
        )
    repaired_quality = assess_encoding_quality(repaired, plan)
    resolution["repaired_event_count"] = len(repaired.events)
    resolution["status"] = (
        "reencoded_passed"
        if not repaired_quality.review_required
        else "quality_repair_rejected"
    )
    if repaired_quality.review_required:
        resolution["repaired_review_reasons"] = list(
            repaired_quality.review_reasons
        )
        return ({
            **report,
            "status": "quality_repair_rejected",
            "error": "one-shot grouping repair remains structurally anomalous",
            "request_count": total_requests,
            "usage": total_usage,
            "temporary_commit_valid": False,
            "quality_resolution": resolution,
        }, None)

    return ({
        **report,
        "status": "ok",
        "contract_valid": True,
        "request_count": total_requests,
        "shard_count": 1,
        "usage": total_usage,
        "metrics": measure_encoding(repaired, plan).safe_dict(),
        "quality": repaired_quality.safe_dict(),
        "event_shapes": _event_shapes(repaired.events, plan),
        "non_event_source_positions": _local_source_positions(
            repaired.non_event_source_refs
        ),
        "ignored_episode_fields": list(repaired.ignored_episode_fields),
        "projected_episode_fields": list(repaired.projected_episode_fields),
        "provider_episode_count": repaired.provider_episode_count,
        "backend_overflow_applied": repaired.backend_overflow_applied,
        "backend_deferred_source_count": repaired.backend_deferred_source_count,
        "backend_time_projection_count": repaired.backend_time_projection_count,
        **_event_counts(repaired.events),
        "quality_resolution": resolution,
    }, repaired)


def _commit_encoding(
    store: MemoryV2Store,
    source: ConversationSource,
    plan: Any,
    parsed: Any,
) -> list[str]:
    return store.commit_encoding(
        plan.batch,
        parsed.events,
        batch_sources=plan.batch_sources,
        source_validator=source.validate_refs,
        batch_source_validator=source.validate_batch_sources,
    )


async def run_sample(
    source: ConversationSource,
    planner: ReplayPlanner,
    active_date: str,
    batch_number: int,
    *,
    max_requests: int = 1,
    repair_quality: bool = False,
) -> dict[str, Any]:
    day = planner.plan_active_date(active_date)
    if batch_number > len(day.batches):
        return {
            "active_date": active_date,
            "batch_number": batch_number,
            "status": "selection_error",
            "error": f"day_has_{len(day.batches)}_batches",
        }
    plan = day.batches[batch_number - 1]
    base: dict[str, Any] = {
        "active_date": active_date,
        "batch_number": batch_number,
        "batch_id": plan.batch.batch_id,
        "source_message_count": plan.batch.source_count,
        "estimated_source_tokens": plan.estimated_tokens,
    }
    report, parsed = await _encode_plan(
        source, plan, base, max_requests=max_requests
    )
    if parsed is None:
        return report
    report, parsed = await _resolve_encoding_quality(
        source,
        plan,
        report,
        parsed,
        enabled=repair_quality,
    )
    if parsed is None:
        return report
    with tempfile.TemporaryDirectory() as temp_dir:
        store = MemoryV2Store(Path(temp_dir) / "memory_v2_experiment.db")
        event_ids = _commit_encoding(store, source, plan, parsed)
        committed = len(event_ids) == len(parsed.events)
    return {**report, "temporary_commit_valid": committed}


async def run_boundary_sample(
    source: ConversationSource,
    planner: ReplayPlanner,
    active_date: str,
    boundary_after_batch: int,
    *,
    max_requests: int = 1,
) -> dict[str, Any]:
    """Encode two adjacent batches, judge their edge, and commit only to a temp store."""

    day = planner.plan_active_date(active_date)
    if boundary_after_batch <= 0 or boundary_after_batch >= len(day.batches):
        return {
            "active_date": active_date,
            "boundary_after_batch": boundary_after_batch,
            "status": "selection_error",
            "error": f"day_has_{len(day.batches)}_batches",
        }
    previous_plan = day.batches[boundary_after_batch - 1]
    next_plan = day.batches[boundary_after_batch]
    previous_base = {
        "active_date": active_date,
        "batch_number": boundary_after_batch,
        "batch_id": previous_plan.batch.batch_id,
        "source_message_count": previous_plan.batch.source_count,
        "estimated_source_tokens": previous_plan.estimated_tokens,
    }
    next_base = {
        "active_date": active_date,
        "batch_number": boundary_after_batch + 1,
        "batch_id": next_plan.batch.batch_id,
        "source_message_count": next_plan.batch.source_count,
        "estimated_source_tokens": next_plan.estimated_tokens,
    }
    previous_report, previous = await _encode_plan(
        source, previous_plan, previous_base, max_requests=max_requests
    )
    if previous is None:
        return {
            "active_date": active_date,
            "boundary_after_batch": boundary_after_batch,
            "status": "previous_encoding_error",
            "error": previous_report.get("error", "encoding_failed"),
            "temporary_commit_valid": False,
            "request_count": previous_report.get("request_count", 0),
            "usage": previous_report.get("usage", {}),
            "encoding_reports": [previous_report],
        }
    next_report, following = await _encode_plan(
        source, next_plan, next_base, max_requests=max_requests
    )
    encoding_reports = [previous_report, next_report]
    encoding_usage = _combined_usage(*(item.get("usage", {}) for item in encoding_reports))
    encoding_requests = sum(int(item.get("request_count", 0)) for item in encoding_reports)
    if following is None:
        return {
            "active_date": active_date,
            "boundary_after_batch": boundary_after_batch,
            "status": "next_encoding_error",
            "error": next_report.get("error", "encoding_failed"),
            "temporary_commit_valid": False,
            "request_count": encoding_requests,
            "usage": encoding_usage,
            "encoding_reports": encoding_reports,
        }

    link_plan = plan_boundary_links(
        previous_plan,
        previous.events,
        next_plan,
        following.events,
        previous_messages=source.read_batch(previous_plan.batch_sources),
        next_messages=source.read_batch(next_plan.batch_sources),
    )
    link_provider_report: dict[str, Any] = {
        "status": "skipped_empty_side",
        "usage": {},
        "raw_output_chars": 0,
    }
    parsed_links = None
    if link_plan.previous_events and link_plan.next_events:
        provider = await _call_flash_once(
            build_linking_messages(link_plan),
            max_tokens=1_024,
            schema_name=LINKING_SCHEMA_NAME,
            schema=linking_response_schema(),
            usage_tag="memory_v2_link_experiment",
        )
        link_provider_report = {
            "status": provider.status,
            "model": provider.model,
            "duration_ms": provider.duration_ms,
            "finish_reason": provider.finish_reason,
            "usage": _safe_usage(provider.usage) or {},
            "raw_output_chars": len(provider.raw_content),
        }
        if provider.status != "ok":
            return {
                "active_date": active_date,
                "boundary_after_batch": boundary_after_batch,
                "status": "link_provider_error",
                "error": provider.error or "missing_parsed_output",
                "temporary_commit_valid": False,
                "request_count": encoding_requests + 1,
                "usage": _combined_usage(encoding_usage, provider.usage),
                "boundary": link_plan.safe_dict(),
                "encoding_reports": encoding_reports,
                "link_report": link_provider_report,
            }
        try:
            parsed_links = parse_linking_output(provider.raw_content, link_plan)
        except LinkingContractError as exc:
            return {
                "active_date": active_date,
                "boundary_after_batch": boundary_after_batch,
                "status": "link_contract_error",
                "error": str(exc),
                "temporary_commit_valid": False,
                "request_count": encoding_requests + 1,
                "usage": _combined_usage(encoding_usage, provider.usage),
                "boundary": link_plan.safe_dict(),
                "encoding_reports": encoding_reports,
                "link_report": link_provider_report,
            }

    drafts = accepted_link_drafts(parsed_links, link_plan) if parsed_links else ()
    judgment = build_boundary_judgment_draft(
        link_plan,
        parsed_links,
        skipped_empty_side=parsed_links is None,
    )
    thread_ids: list[str] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        store = MemoryV2Store(Path(temp_dir) / "memory_v2_boundary_experiment.db")
        previous_ids = _commit_encoding(store, source, previous_plan, previous)
        next_ids = _commit_encoding(store, source, next_plan, following)
        _, thread_id, _ = store.commit_boundary_judgment(
            judgment, drafts[0] if drafts else None
        )
        if thread_id:
            thread_ids.append(thread_id)
        stored_judgment = store.get_boundary_judgment(link_plan.boundary_id)
        committed = (
            len(previous_ids) == len(previous.events)
            and len(next_ids) == len(following.events)
            and len(thread_ids) == len(drafts)
            and stored_judgment is not None
            and stored_judgment["outcome"] == judgment.outcome
        )

    proposed = parsed_links.judgments if parsed_links else ()
    link_usage = link_provider_report.get("usage", {})
    return {
        "active_date": active_date,
        "boundary_after_batch": boundary_after_batch,
        "status": "ok",
        "contract_valid": True,
        "temporary_commit_valid": committed,
        "request_count": encoding_requests + int(parsed_links is not None),
        "encoding_request_count": encoding_requests,
        "link_request_count": int(parsed_links is not None),
        "usage": _combined_usage(encoding_usage, link_usage),
        "boundary": link_plan.safe_dict(),
        "proposed_continuation_count": len(proposed),
        "accepted_continuation_count": len(drafts),
        "low_confidence_count": len(proposed) - len(drafts),
        "boundary_judgment_outcome": judgment.outcome,
        "boundary_receipt_persisted": stored_judgment is not None,
        "ignored_link_fields": list(parsed_links.ignored_link_fields) if parsed_links else [],
        "accepted_link_shapes": [
            {
                "previous_ref": item.previous_ref,
                "next_ref": item.next_ref,
                "confidence": item.confidence,
                "reason_codes": list(item.reason_codes),
            }
            for item in (parsed_links.accepted() if parsed_links else ())
        ],
        "proposed_link_shapes": [
            {
                "previous_ref": item.previous_ref,
                "next_ref": item.next_ref,
                "confidence": item.confidence,
                "accepted": item.confidence >= MIN_CONTINUATION_CONFIDENCE,
                "reason_codes": list(item.reason_codes),
            }
            for item in proposed
        ],
        "encoding_reports": encoding_reports,
        "link_report": link_provider_report,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run selected Memory V2 batches through Flash without retaining content"
    )
    parser.add_argument("--db", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--sample",
        action="append",
        type=_parse_sample,
        help="one-based batch selection, for example 2025-01-10:2",
    )
    selection.add_argument(
        "--boundary",
        action="append",
        type=_parse_sample,
        help="boundary after a one-based batch, for example 2025-01-10:1",
    )
    selection.add_argument(
        "--day",
        action="append",
        help="whole active date, encoding each batch once, for example 2025-01-10",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1,
        help="hard request limit per selected root batch (default: 1)",
    )
    parser.add_argument(
        "--work-db",
        type=Path,
        help="isolated resumable Memory V2 DB; valid only with --day",
    )
    parser.add_argument(
        "--quality-repair",
        action="store_true",
        help="opt into the legacy anomaly audit and one-shot re-encode experiment",
    )
    return parser


async def _main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    source = ConversationSource(args.db, canonical_sessions_only=True)
    planner = ReplayPlanner(source)
    if args.max_requests <= 0:
        raise SystemExit("--max-requests must be positive")
    if args.work_db is not None and not args.day:
        raise SystemExit("--work-db is valid only with --day")
    if args.sample:
        reports = [
            await run_sample(
                source,
                planner,
                active_date,
                batch_number,
                max_requests=args.max_requests,
                repair_quality=args.quality_repair,
            )
            for active_date, batch_number in args.sample
        ]
    elif args.boundary:
        reports = [
            await run_boundary_sample(
                source,
                planner,
                active_date,
                batch_number,
                max_requests=args.max_requests,
            )
            for active_date, batch_number in args.boundary
        ]
    else:
        from .day_backfill_runner import run_backfill_day

        reports = [
            await run_backfill_day(
                source,
                planner,
                active_date,
                max_requests=args.max_requests,
                work_db_path=args.work_db,
                repair_quality=args.quality_repair,
            )
            for active_date in args.day
        ]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all(report.get("status") == "ok" for report in reports) else 1


def main(argv: Iterable[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
