"""Content-safe, repeatable recall@k evaluation for Memory V2 shadow retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .conversation_source import ConversationSource
from .recall import EventRecallIndex, RecallQuery


_CASE_FIELDS = {
    "case_id",
    "query",
    "variants",
    "target_source_message_ids",
    "match_mode",
    "subject_ids",
    "event_types",
    "source_kinds",
    "date_from",
    "date_to",
}


@dataclass(frozen=True)
class RecallBenchmarkCase:
    case_id: str
    query: RecallQuery
    target_source_message_ids: tuple[str, ...]
    match_mode: str = "any"

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("benchmark case_id must not be empty")
        if not self.target_source_message_ids:
            raise ValueError("benchmark case needs at least one stable source anchor")
        if self.match_mode not in {"any", "all"}:
            raise ValueError("benchmark match_mode must be any or all")


def _string_tuple(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    clean = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if required and not clean:
        raise ValueError(f"{field} must not be empty")
    return clean


def parse_benchmark_cases(raw: str | bytes) -> tuple[RecallBenchmarkCase, ...]:
    try:
        body = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("recall benchmark is not valid JSON") from exc
    if not isinstance(body, list) or not body:
        raise ValueError("recall benchmark must be a non-empty array")
    cases: list[RecallBenchmarkCase] = []
    seen: set[str] = set()
    for item in body:
        if not isinstance(item, Mapping) or set(item) != _CASE_FIELDS:
            raise ValueError("recall benchmark case fields do not match the contract")
        case_id = str(item["case_id"] or "").strip()
        if case_id in seen:
            raise ValueError(f"duplicate benchmark case_id: {case_id}")
        seen.add(case_id)
        query = RecallQuery(
            text=str(item["query"] or ""),
            variants=_string_tuple(item["variants"], "variants"),
            subject_ids=_string_tuple(item["subject_ids"], "subject_ids"),
            event_types=_string_tuple(item["event_types"], "event_types"),
            source_kinds=_string_tuple(item["source_kinds"], "source_kinds"),
            date_from=str(item["date_from"] or ""),
            date_to=str(item["date_to"] or ""),
        )
        cases.append(
            RecallBenchmarkCase(
                case_id=case_id,
                query=query,
                target_source_message_ids=_string_tuple(
                    item["target_source_message_ids"],
                    "target_source_message_ids",
                    required=True,
                ),
                match_mode=str(item["match_mode"] or ""),
            )
        )
    return tuple(cases)


def load_benchmark_cases(path: str | Path) -> tuple[RecallBenchmarkCase, ...]:
    return parse_benchmark_cases(Path(path).read_text(encoding="utf-8"))


def _query_digest(query: RecallQuery) -> str:
    payload = {
        "text": query.text,
        "variants": query.variants,
        "entity_terms": query.entity_terms,
        "subject_ids": query.subject_ids,
        "event_types": query.event_types,
        "source_kinds": query.source_kinds,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "date_basis": query.date_basis,
        "sort_mode": query.sort_mode,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rank_for_case(case: RecallBenchmarkCase, hits: Sequence[Any]) -> int | None:
    expected = set(case.target_source_message_ids)
    for rank, hit in enumerate(hits, start=1):
        actual = set(hit.document.source_message_ids)
        matched = expected.issubset(actual) if case.match_mode == "all" else bool(expected & actual)
        if matched:
            return rank
    return None


def run_recall_benchmark(
    index: EventRecallIndex,
    cases: Iterable[RecallBenchmarkCase],
    *,
    k_values: Sequence[int] = (1, 3, 5),
) -> dict[str, Any]:
    clean_k = tuple(sorted({int(value) for value in k_values if int(value) > 0}))
    if not clean_k:
        raise ValueError("benchmark needs at least one positive k")
    case_list = list(cases)
    if not case_list:
        raise ValueError("benchmark needs at least one case")
    maximum = max(clean_k)
    reports: list[dict[str, Any]] = []
    reciprocal_rank_total = 0.0
    semantic_case_count = 0
    hits_at = {value: 0 for value in clean_k}
    for case in case_list:
        result = index.search(case.query, limit=maximum)
        rank = _rank_for_case(case, result.hits)
        if rank is not None:
            reciprocal_rank_total += 1.0 / rank
        semantic_case_count += int(result.semantic_available)
        case_hits = {str(value): rank is not None and rank <= value for value in clean_k}
        for value in clean_k:
            hits_at[value] += int(case_hits[str(value)])
        reports.append(
            {
                "case_id": case.case_id,
                "query_digest": _query_digest(case.query),
                "candidate_count": result.candidate_count,
                "evaluated_count": result.evaluated_count,
                "returned_count": len(result.hits),
                "target_rank": rank,
                "hit_at_k": case_hits,
                "semantic_available": result.semantic_available,
                "semantic_status": result.semantic_status,
                "top_hit_observations": [
                    hit.safe_observation(position)
                    for position, hit in enumerate(result.hits, start=1)
                ],
            }
        )
    count = len(case_list)
    return {
        "status": "ok",
        "case_count": count,
        "k_values": list(clean_k),
        "recall_at_k": {
            str(value): round(hits_at[value] / count, 4) for value in clean_k
        },
        "mean_reciprocal_rank": round(reciprocal_rank_total / count, 4),
        "semantic_case_count": semantic_case_count,
        "cases": reports,
    }


async def run_planned_recall_benchmark(
    index: EventRecallIndex,
    cases: Iterable[RecallBenchmarkCase],
    *,
    k_values: Sequence[int] = (1, 3, 5),
) -> dict[str, Any]:
    """Measure planner plus retrieval while retaining only content-free diagnostics."""

    from .experiment import _call_flash_once, _combined_usage, _safe_usage
    from .recall_planner import (
        RECALL_PLANNER_SCHEMA_NAME,
        RECALL_PLANNER_VERSION,
        RecallPlannerContractError,
        apply_recall_query_plan,
        build_recall_planner_messages,
        parse_recall_query_plan,
        recall_planner_schema,
    )

    case_list = list(cases)
    planned_cases: list[RecallBenchmarkCase] = []
    planner_reports: list[dict[str, Any]] = []
    planner_usages: list[dict[str, Any]] = []
    for case in case_list:
        provider = await _call_flash_once(
            build_recall_planner_messages(case.query.text),
            max_tokens=1_024,
            schema_name=RECALL_PLANNER_SCHEMA_NAME,
            schema=recall_planner_schema(),
            usage_tag="memory_v2_recall_planner_benchmark",
        )
        usage = _safe_usage(provider.usage) or {}
        planner_usages.append(usage)
        observation: dict[str, Any] = {
            "case_id": case.case_id,
            "status": provider.status,
            "usage": usage,
            "raw_output_chars": len(provider.raw_content),
        }
        planned_case = case
        if provider.status == "ok":
            try:
                plan = parse_recall_query_plan(
                    provider.raw_content,
                    case.query.text,
                )
            except RecallPlannerContractError as exc:
                observation["status"] = "contract_error"
                observation["error"] = str(exc)
            else:
                observation.update(plan.safe_observation())
                observation["status"] = (
                    "ok" if plan.needs_history else "history_not_selected"
                )
                if plan.needs_history:
                    planned_case = replace(
                        case,
                        query=apply_recall_query_plan(case.query, plan),
                    )
        planner_reports.append(observation)
        planned_cases.append(planned_case)

    report = run_recall_benchmark(index, planned_cases, k_values=k_values)
    status_counts = Counter(item["status"] for item in planner_reports)
    report["planner"] = {
        "version": RECALL_PLANNER_VERSION,
        "request_count": len(planner_reports),
        "usage": _combined_usage(*planner_usages),
        "status_counts": dict(sorted(status_counts.items())),
        "history_not_selected_count": status_counts.get(
            "history_not_selected", 0
        ),
        "cases": planner_reports,
    }
    return report


def validate_benchmark_anchors(
    source: ConversationSource,
    cases: Iterable[RecallBenchmarkCase],
) -> dict[str, Any]:
    """Verify private gold anchors against the raw authority without reporting IDs."""

    case_list = list(cases)
    all_ids = tuple(
        dict.fromkeys(
            message_id
            for case in case_list
            for message_id in case.target_source_message_ids
        )
    )
    found = {
        message.message_id for message in source.read_message_ids(all_ids)
    }
    invalid = [
        case.case_id
        for case in case_list
        if not set(case.target_source_message_ids).issubset(found)
    ]
    return {
        "status": "ok" if not invalid else "invalid_anchor",
        "case_count": len(case_list),
        "unique_anchor_count": len(all_ids),
        "resolved_anchor_count": len(found),
        "invalid_case_ids": invalid,
    }


__all__ = [
    "RecallBenchmarkCase",
    "load_benchmark_cases",
    "parse_benchmark_cases",
    "run_recall_benchmark",
    "run_planned_recall_benchmark",
    "validate_benchmark_anchors",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a content-safe lexical Memory V2 recall shadow benchmark"
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument(
        "--source-db",
        type=Path,
        help="optional raw conversation DB used only to validate gold anchors",
    )
    parser.add_argument(
        "--k",
        action="append",
        type=int,
        help="positive recall cutoff; repeat for multiple values (default: 1,3,5)",
    )
    args = parser.parse_args(argv)
    cases = load_benchmark_cases(args.cases)
    report = run_recall_benchmark(
        EventRecallIndex(args.db),
        cases,
        k_values=tuple(args.k) if args.k else (1, 3, 5),
    )
    if args.source_db is not None:
        report["anchor_validation"] = validate_benchmark_anchors(
            ConversationSource(args.source_db),
            cases,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
