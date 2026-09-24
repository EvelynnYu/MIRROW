"""Bounded, source-backed projection for Agent's active multi-stage deep search.

This is a transient read operation over the normal Memory V2 index.  A date
shared by separate search branches narrows a *candidate* scene; it is not a
new event edge or evidence that one occurrence caused another.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import re
from typing import Sequence

from .active_query_plan import ActiveQueryPlan
from .conversation_source import ConversationSource
from .recall import EventRecallIndex, RecallHit, RecallQuery, RecallResult
from .recall_context import RecallContextPolicy, build_recall_context
from .recall_evidence import (
    RecallEvidenceBundle,
    expand_recall_evidence,
    render_recall_evidence_for_context,
)
from .recall_service import run_shadow_recall


_STAGE_HIT_LIMITS = (2, 2, 2, 2)
_MAX_OUTPUT_CHARS = 3_800
_TIME_OF_DAY_RE = re.compile(r"凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|深夜")
_CROSS_DAY_RE = re.compile(r"隔天|次日|第二天|几天后|数天后|一周后|隔周|跨日|过了几天")
_EXPLICIT_DAY_RE = re.compile(r"(?:\d{4}[年/-])?\d{1,2}[月/-]\d{1,2}日?")


@dataclass(frozen=True)
class StagedSearchResult:
    text: str
    searched_branch_count: int
    date_scope_used: bool
    date_scope_candidate_count: int
    selected_hit_count: int
    evidence_expanded: bool
    semantic_available: bool
    semantic_status: str
    candidate_count: int
    evaluated_count: int
    truncated: bool

    def safe_observation(self) -> dict[str, object]:
        return {
            "status": "recalled" if self.text else "no_hits",
            "injected": bool(self.text),
            "multi_stage": True,
            "searched_branch_count": self.searched_branch_count,
            "date_scope_used": self.date_scope_used,
            "date_scope_candidate_count": self.date_scope_candidate_count,
            "returned_count": self.selected_hit_count,
            "rendered_hit_count": self.selected_hit_count,
            "rendered_chars": len(self.text),
            "evidence_expanded": self.evidence_expanded,
            "semantic_available": self.semantic_available,
            "semantic_status": self.semantic_status,
            "candidate_count": self.candidate_count,
            "evaluated_count": self.evaluated_count,
            "truncated": self.truncated,
        }


def _candidate_date(hits_by_query: Sequence[Sequence[RecallHit]]) -> tuple[str, int]:
    """Require agreement across search branches; never infer causation."""

    branches: dict[str, set[int]] = defaultdict(set)
    strengths: dict[str, float] = defaultdict(float)
    original_ranks: dict[str, int] = {}
    anchor_ranks: dict[str, int] = {}
    for branch_index, hits in enumerate(hits_by_query):
        seen_in_branch: set[str] = set()
        for rank, hit in enumerate(hits[:8], start=1):
            document = hit.document
            minimum_score = 0.05 if branch_index == 0 else 0.08
            if document.source_layer != "event" or hit.score < minimum_score:
                continue
            day = document.date_from
            if not day or day in seen_in_branch:
                continue
            seen_in_branch.add(day)
            branches[day].add(branch_index)
            strengths[day] += max(0.08, hit.score) / rank
            if branch_index == 0:
                original_ranks[day] = rank
            elif branch_index == 1:
                anchor_ranks[day] = rank
    qualifying = [day for day, indexes in branches.items() if len(indexes) >= 2]
    if not qualifying:
        return "", 0
    chosen = max(
        qualifying,
        key=lambda day: (
            len(branches[day]),
            1 / original_ranks[day] if day in original_ranks else 0.0,
            1 / anchor_ranks[day] if day in anchor_ranks else 0.0,
            strengths[day],
            day,
        ),
    )
    return chosen, len(qualifying)


def _unique_event_hits(hits: Sequence[RecallHit]) -> list[RecallHit]:
    return [hit for hit in hits if hit.document.source_layer == "event"]


def _matches_requested_time(hit: RecallHit, query: str) -> bool:
    """Use explicit time-of-day evidence, not an inferred event boundary."""

    requested = set(_TIME_OF_DAY_RE.findall(query))
    if not requested:
        return True
    summary = " ".join((*hit.document.summaries, hit.document.display_text))
    return any(value in summary for value in requested)


def search_active_stages(
    index: EventRecallIndex,
    source: ConversationSource,
    question: str,
    plan: ActiveQueryPlan,
    *,
    reference_date: date,
    current_message_id: str = "",
    excluded_message_ids: Sequence[str] = (),
    limit: int = 8,
    include_source_detail: bool = False,
) -> StagedSearchResult:
    """Retrieve each stage, then connect only through source-backed candidates."""

    bounded_limit = max(1, min(int(limit), 10))
    current_active_dates = tuple(
        dict.fromkeys(
            message.active_date
            for message in source.read_message_ids(
                (current_message_id,) if current_message_id else ()
            )
            if message.active_date
        )
    )
    excluded = tuple(
        dict.fromkeys(
            str(value)
            for value in (*excluded_message_ids, current_message_id)
            if str(value)
        )
    )
    branch_queries = (question, *plan.queries)
    global_executions = [
        run_shadow_recall(
            index,
            source,
            RecallQuery(
                text=query,
                exclude_message_ids=excluded,
                exclude_active_dates=current_active_dates,
            ),
            reference_date=reference_date,
            limit=10,
            force_source_detail=False,
        )
        for query in branch_queries
    ]
    scope_date, scope_candidate_count = _candidate_date(
        tuple(execution.result.hits for execution in global_executions)
    )
    if _CROSS_DAY_RE.search(question) or len(_EXPLICIT_DAY_RE.findall(question)) >= 2:
        # One date cannot contain an explicitly multi-day question.  Existing
        # event threads can still be retrieved globally as one source view.
        scope_date = ""
    stage_hits: list[list[RecallHit]] = []
    evaluated = sum(
        execution.result.evaluated_count or 0 for execution in global_executions
    )
    semantic_available = any(
        execution.result.semantic_available for execution in global_executions
    )
    semantic_status = global_executions[0].result.semantic_status
    for stage_index, query in enumerate(plan.queries):
        stage_result = global_executions[stage_index + 1].result
        if scope_date:
            scoped = run_shadow_recall(
                index,
                source,
                RecallQuery(
                    text=query,
                    date_from=scope_date,
                    date_to=scope_date,
                    exclude_message_ids=excluded,
                    exclude_active_dates=current_active_dates,
                ),
                reference_date=reference_date,
                limit=10,
                force_source_detail=False,
            )
            evaluated += scoped.result.evaluated_count or 0
            semantic_available |= scoped.result.semantic_available
            stage_result = scoped.result
        candidates = [
            hit for hit in _unique_event_hits(stage_result.hits)
            if _matches_requested_time(hit, query)
        ]
        stage_hits.append(candidates[:_STAGE_HIT_LIMITS[min(stage_index, 3)]])

    seen: set[tuple[str, str]] = set()
    selected_by_stage: list[list[RecallHit]] = []
    selected_all: list[RecallHit] = []
    for hits in stage_hits:
        unique: list[RecallHit] = []
        for hit in hits:
            identity = (hit.document.source_layer, hit.document.document_id)
            if identity in seen or len(selected_all) >= bounded_limit:
                continue
            seen.add(identity)
            unique.append(hit)
            selected_all.append(hit)
        selected_by_stage.append(unique)

    # The search score locates candidates; source timestamps supply their
    # presentation order within a stage.  Row IDs and rank are not a clock.
    source_ids = tuple(
        dict.fromkeys(
            message_id
            for hit in selected_all
            for message_id in hit.document.source_message_ids
        )
    )
    source_times = {
        message.message_id: message.timestamp
        for message in source.read_message_ids(source_ids)
    }
    for hits in selected_by_stage:
        hits.sort(
            key=lambda hit: min(
                (
                    source_times[message_id]
                    for message_id in hit.document.source_message_ids
                    if message_id in source_times
                ),
                default=hit.document.date_from,
            )
        )

    if not selected_all:
        return StagedSearchResult(
            "", len(plan.queries), bool(scope_date), scope_candidate_count,
            0, False, semantic_available, semantic_status,
            global_executions[0].result.candidate_count, evaluated, False,
        )

    empty_evidence = RecallEvidenceBundle(expanded=False, reason="not_requested")
    lines = [
        "[你按几条线索找回的相关经历]",
        (
            "以下段落来自同一日期的记录；时间接近不单独证明因果或事件延续。"
            if scope_date else
            "以下是分线索找到的候选；是否属于同一件事尚未由来源确认。"
        ),
    ]
    truncated = False
    rendered_hits: list[RecallHit] = []
    for stage_index, hits in enumerate(selected_by_stage):
        if not hits:
            continue
        result = RecallResult(
            hits=tuple(hits),
            candidate_count=len(hits),
            semantic_available=semantic_available,
            semantic_status=semantic_status,
        )
        projection = build_recall_context(
            result,
            empty_evidence,
            policy=RecallContextPolicy(
                max_hits=min(3, bounded_limit),
                target_hits=min(3, bounded_limit),
                min_hits_before_cliff=1,
                dense_expansion_min_top_score=0.0,
                max_hit_chars=310,
                max_summary_chars=950,
                max_evidence_chars=0,
                max_total_chars=950,
            ),
        )
        body = projection.text.split("\n", 1)[-1].strip()
        if not body:
            continue
        label = "起点相关线索" if stage_index == 0 else f"后续线索{stage_index}"
        block = f"{label}：\n{body}"
        if len("\n\n".join((*lines, block))) > _MAX_OUTPUT_CHARS:
            truncated = True
            break
        lines.append(block)
        rendered_identities = set(projection.rendered_documents)
        rendered_hits.extend(
            hit for hit in hits
            if (hit.document.source_layer, hit.document.document_id)
            in rendered_identities
        )
        truncated |= projection.truncated

    if not rendered_hits:
        return StagedSearchResult(
            "", len(plan.queries), bool(scope_date), scope_candidate_count,
            0, False, semantic_available, semantic_status,
            global_executions[0].result.candidate_count, evaluated, truncated,
        )

    evidence_expanded = False
    if include_source_detail:
        evidence = expand_recall_evidence(
            source, rendered_hits, needs_source_detail=True
        )
        evidence_text = render_recall_evidence_for_context(
            evidence, perspective="agent_second_person"
        )
        remaining = _MAX_OUTPUT_CHARS - len("\n\n".join(lines)) - 2
        if evidence_text and remaining >= 120:
            lines.append(evidence_text[: min(1_100, remaining)])
            evidence_expanded = evidence.expanded
            truncated |= len(evidence_text) > remaining
    text = "\n\n".join(lines)
    return StagedSearchResult(
        text, len(plan.queries), bool(scope_date), scope_candidate_count,
        len(rendered_hits), evidence_expanded, semantic_available, semantic_status,
        global_executions[0].result.candidate_count, evaluated, truncated,
    )


__all__ = ["StagedSearchResult", "search_active_stages"]
