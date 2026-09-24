"""Deterministic candidate screening for completed active-day settlement.

The selector is intentionally only a recall-friendly sieve.  It may nominate
an edge for later judgment, but it cannot create a thread, assert continuity,
or infer resolution.  It reads event summaries and structural metadata only;
authoritative conversation bodies are outside this module's input.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .day_settlement import (
    MAX_CONTINUATION_CANDIDATES,
    DaySettlementPlan,
    SettlementContinuationCandidate,
    SettlementEvent,
    SettlementThreadCandidate,
    validate_day_settlement_plan,
)


CANDIDATE_SELECTOR_VERSION = "day-settlement-candidates-v2"
_LONG_GAP_SECONDS = 30 * 24 * 60 * 60
_LONG_GAP_BACK_REFERENCE_RE = re.compile(
    r"(?:上次|此前|之前|先前|那次|旧伤|旧事|复发|"
    r"接着(?:上次|之前)|继续(?:上次|之前)|"
    r"还没(?:好|完|解决|结束|兑现)|"
    r"仍未(?:好|完成|解决|结束|兑现))"
)
_CORE_PARTICIPANTS = {"human", "agent"}
_GENERIC_EVENT_TYPES = {
    "conversation_moment",
    "interaction",
    "other",
    "status_update",
}
_GENERIC_TERMS = {
    "一个",
    "一些",
    "事情",
    "这件",
    "件事",
    "表示",
    "提到",
    "回应",
    "自己",
    "对话",
    "今天",
    "昨天",
    "现在",
    "开始",
    "发生",
    "吃了",
    "human",
    "assistant",
    "user",
    "人类伙伴",
    "洛月",
    "月凝",
}
_GENERIC_FRAGMENTS = (
    "洛月凝",
    "人类伙伴",
    "表示",
    "提到",
    "回应",
    "说自己",
    "这件事情",
    "这件事",
)
_CONTINUATION_CUES = (
    "继续",
    "接着",
    "仍然",
    "还是",
    "还在",
    "再次",
    "复发",
    "上次",
)
_SIGNAL_PRIORITY = (
    "shared_distinctive_terms",
    "shared_specific_participant",
    "explicit_continuation_cue",
    "same_primary_type",
    "close_in_time",
    "recent_prior_thread",
    "same_subject",
)


class DaySettlementCandidateError(ValueError):
    """The deterministic selector received an invalid plan or policy."""


@dataclass(frozen=True)
class DaySettlementCandidatePolicy:
    min_local_score: float = 0.38
    max_candidates_per_target: int = 3
    max_candidates_total: int = MAX_CONTINUATION_CANDIDATES

    def __post_init__(self) -> None:
        if not 0 <= self.min_local_score <= 1:
            raise ValueError("min_local_score must be between 0 and 1")
        if self.max_candidates_per_target <= 0:
            raise ValueError("max_candidates_per_target must be positive")
        if not 1 <= self.max_candidates_total <= MAX_CONTINUATION_CANDIDATES:
            raise ValueError("max_candidates_total exceeds the settlement contract")


@dataclass(frozen=True)
class DaySettlementCandidateSelection:
    candidates: tuple[SettlementContinuationCandidate, ...]
    evaluated_pair_count: int
    eligible_pair_count: int
    dropped_by_bound_count: int
    selector_version: str = CANDIDATE_SELECTOR_VERSION

    def safe_dict(self) -> dict[str, int | str | bool]:
        return {
            "candidate_count": len(self.candidates),
            "evaluated_pair_count": self.evaluated_pair_count,
            "eligible_pair_count": self.eligible_pair_count,
            "dropped_by_bound_count": self.dropped_by_bound_count,
            "has_continuation_candidates": bool(self.candidates),
            "selector_version": self.selector_version,
        }

    def apply_to(self, plan: DaySettlementPlan) -> DaySettlementPlan:
        if plan.continuation_candidates:
            raise DaySettlementCandidateError(
                "candidate selection can only apply to an unscreened plan"
            )
        updated = replace(plan, continuation_candidates=self.candidates)
        validate_day_settlement_plan(updated)
        return updated


@dataclass(frozen=True)
class SettlementPairScreening:
    local_score: float
    signal_codes: tuple[str, ...]
    supported: bool

    def safe_dict(self) -> dict[str, float | list[str] | bool]:
        return {
            "local_score": self.local_score,
            "signal_codes": list(self.signal_codes),
            "supported": self.supported,
        }


@dataclass(frozen=True)
class _ScoredEdge:
    prior_thread_ref: str
    match_event_ref: str
    to_day_event_ref: str
    score: float
    signal_codes: tuple[str, ...]


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _lexical_terms(value: str) -> set[str]:
    lowered = str(value or "").casefold()
    for fragment in _GENERIC_FRAGMENTS:
        lowered = lowered.replace(fragment, " ")
    terms = {
        item
        for item in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", lowered)
        if len(item) >= 3
    }
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms - _GENERIC_TERMS


def _specific_participants(event: SettlementEvent) -> set[str]:
    return {
        participant.strip().casefold()
        for participant in event.participant_ids
        if participant.strip().casefold() not in _CORE_PARTICIPANTS
    }


def _ordered_signals(signals: set[str]) -> tuple[str, ...]:
    return tuple(signal for signal in _SIGNAL_PRIORITY if signal in signals)[:4]


def has_long_gap_back_reference(summary: str) -> bool:
    """Whether a later event explicitly points back across a long time gap."""

    return bool(_LONG_GAP_BACK_REFERENCE_RE.search(str(summary or "")))


def is_long_gap_without_back_reference(
    earlier_reported_at: str,
    later_reported_at: str,
    later_summary: str,
) -> bool:
    """Identify unsafe month-scale continuation based only on generic similarity."""

    return bool(
        (_timestamp(later_reported_at) - _timestamp(earlier_reported_at)).total_seconds()
        > _LONG_GAP_SECONDS
        and not has_long_gap_back_reference(later_summary)
    )


def common_settlement_terms(
    events: Sequence[SettlementEvent],
) -> frozenset[str]:
    """Find corpus-template terms without suppressing small repeated event chains."""

    if not events:
        return frozenset()
    document_frequency = Counter(
        term for event in events for term in _lexical_terms(event.summary)
    )
    common_cutoff = max(4, math.ceil(len(events) * 0.10))
    return frozenset(
        term for term, count in document_frequency.items() if count > common_cutoff
    )


def screen_settlement_event_pair(
    earlier: SettlementEvent,
    later: SettlementEvent,
    *,
    intra_day: bool,
    ignored_terms: frozenset[str] = frozenset(),
) -> SettlementPairScreening:
    """Return content-free local evidence without asserting continuation."""

    signals: set[str] = set()
    score = 0.0

    earlier_terms = _lexical_terms(earlier.summary) - ignored_terms
    later_terms = _lexical_terms(later.summary) - ignored_terms
    shared_terms = earlier_terms & later_terms
    smaller_term_count = max(1, min(len(earlier_terms), len(later_terms)))
    overlap_ratio = len(shared_terms) / smaller_term_count
    if shared_terms:
        signals.add("shared_distinctive_terms")
        score += min(
            0.62,
            0.12 + 1.8 * overlap_ratio + 0.04 * min(len(shared_terms), 4),
        )

    same_type = bool(
        earlier.event_type
        and earlier.event_type.casefold() == later.event_type.casefold()
    )
    same_specific_type = bool(
        same_type and earlier.event_type.casefold() not in _GENERIC_EVENT_TYPES
    )
    if same_specific_type:
        signals.add("same_primary_type")
        score += 0.12
    if earlier.subject_id.casefold() == later.subject_id.casefold():
        signals.add("same_subject")
        score += 0.04

    shared_specific = _specific_participants(earlier) & _specific_participants(later)
    if shared_specific:
        signals.add("shared_specific_participant")
        score += 0.22

    explicit_cue = any(cue in later.summary for cue in _CONTINUATION_CUES)
    if explicit_cue:
        signals.add("explicit_continuation_cue")
        score += 0.18

    gap_seconds = (
        _timestamp(later.reported_at) - _timestamp(earlier.reported_at)
    ).total_seconds()
    if intra_day:
        if gap_seconds <= 30 * 60:
            signals.add("close_in_time")
            score += 0.10
        elif gap_seconds <= 6 * 60 * 60:
            signals.add("close_in_time")
            score += 0.05
    elif gap_seconds <= 7 * 24 * 60 * 60:
        signals.add("recent_prior_thread")
        score += 0.04

    if intra_day:
        lexical_support = bool(
            (
                same_specific_type
                and shared_terms
                and overlap_ratio >= 0.05
            )
            or (len(shared_terms) >= 4 and overlap_ratio >= 0.08)
        )
    else:
        lexical_support = bool(
            (
                same_specific_type
                and len(shared_terms) >= 2
                and overlap_ratio >= 0.08
            )
            or (len(shared_terms) >= 4 and overlap_ratio >= 0.18)
        )
    participant_support = bool(
        shared_specific and (same_specific_type or shared_terms)
    )
    supported = bool(
        lexical_support
        or participant_support
        or (explicit_cue and same_specific_type)
    )
    if (
        not intra_day
        and is_long_gap_without_back_reference(
            earlier.reported_at,
            later.reported_at,
            later.summary,
        )
    ):
        supported = False
    return SettlementPairScreening(
        local_score=round(min(score, 1.0), 4),
        signal_codes=_ordered_signals(signals),
        supported=supported,
    )


def _candidate_ref(edge: _ScoredEdge) -> str:
    identity = "\0".join(
        (
            CANDIDATE_SELECTOR_VERSION,
            edge.prior_thread_ref,
            edge.match_event_ref,
            edge.to_day_event_ref,
        )
    )
    return "c" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _screen_prior_thread(
    thread: SettlementThreadCandidate,
    target: SettlementEvent,
    minimum_score: float,
    ignored_terms: frozenset[str],
) -> tuple[_ScoredEdge | None, int]:
    best: tuple[float, datetime, SettlementEvent, tuple[str, ...]] | None = None
    evaluated = 0
    target_time = _timestamp(target.reported_at)
    # A thread can contain an older representative that resembles the target
    # while its append tail is actually later than the target.  The match node
    # is only retrieval evidence; a continuation edge must start at the real
    # tail, so such an overlap cannot produce a forward append candidate.
    if _timestamp(thread.representative_events[-1].reported_at) > target_time:
        return None, len(thread.representative_events)
    for representative in thread.representative_events:
        evaluated += 1
        if _timestamp(representative.reported_at) > target_time:
            continue
        screening = screen_settlement_event_pair(
            representative,
            target,
            intra_day=False,
            ignored_terms=ignored_terms,
        )
        if not screening.supported or screening.local_score < minimum_score:
            continue
        candidate = (
            screening.local_score,
            _timestamp(representative.reported_at),
            representative,
            screening.signal_codes,
        )
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None, evaluated
    return (
        _ScoredEdge(
            prior_thread_ref=thread.ref,
            match_event_ref=best[2].ref,
            to_day_event_ref=target.ref,
            score=best[0],
            signal_codes=best[3],
        ),
        evaluated,
    )


def select_day_settlement_candidates(
    plan: DaySettlementPlan,
    policy: DaySettlementCandidatePolicy | None = None,
) -> DaySettlementCandidateSelection:
    """Select bounded cross-batch and prior-thread candidates without a model."""

    if plan.continuation_candidates:
        raise DaySettlementCandidateError(
            "candidate selection requires an unscreened settlement plan"
        )
    validate_day_settlement_plan(plan)
    policy = policy or DaySettlementCandidatePolicy()

    day_events = sorted(
        plan.day_events,
        key=lambda event: (_timestamp(event.reported_at), event.ref),
    )
    edges_by_target: dict[str, list[_ScoredEdge]] = defaultdict(list)
    evaluated_pair_count = 0
    eligible_pair_count = 0
    ignored_terms = common_settlement_terms(
        (
            *day_events,
            *(
                event
                for thread in plan.prior_threads
                for event in thread.representative_events
            ),
        )
    )

    for target_index, target in enumerate(day_events):
        for earlier in day_events[:target_index]:
            if earlier.batch_id == target.batch_id:
                continue
            evaluated_pair_count += 1
            screening = screen_settlement_event_pair(
                earlier,
                target,
                intra_day=True,
                ignored_terms=ignored_terms,
            )
            if screening.supported and screening.local_score >= policy.min_local_score:
                eligible_pair_count += 1
                edges_by_target[target.ref].append(
                    _ScoredEdge(
                        prior_thread_ref="",
                        match_event_ref=earlier.ref,
                        to_day_event_ref=target.ref,
                        score=screening.local_score,
                        signal_codes=screening.signal_codes,
                    )
                )

        for thread in plan.prior_threads:
            edge, evaluated = _screen_prior_thread(
                thread,
                target,
                policy.min_local_score,
                ignored_terms,
            )
            evaluated_pair_count += evaluated
            if edge is not None:
                eligible_pair_count += 1
                edges_by_target[target.ref].append(edge)

    per_target: list[_ScoredEdge] = []
    for target in day_events:
        ranked = sorted(
            edges_by_target.get(target.ref, ()),
            key=lambda edge: (
                -edge.score,
                edge.prior_thread_ref,
                edge.match_event_ref,
            ),
        )
        per_target.extend(ranked[: policy.max_candidates_per_target])

    globally_bounded = sorted(
        per_target,
        key=lambda edge: (-edge.score, edge.to_day_event_ref, edge.match_event_ref),
    )[: policy.max_candidates_total]
    target_order = {event.ref: index for index, event in enumerate(day_events)}
    selected_edges = sorted(
        globally_bounded,
        key=lambda edge: (
            target_order[edge.to_day_event_ref],
            -edge.score,
            edge.prior_thread_ref,
            edge.match_event_ref,
        ),
    )
    candidates = tuple(
        SettlementContinuationCandidate(
            ref=_candidate_ref(edge),
            prior_thread_ref=edge.prior_thread_ref,
            match_event_ref=edge.match_event_ref,
            to_day_event_ref=edge.to_day_event_ref,
            local_score=round(edge.score, 4),
            signal_codes=edge.signal_codes,
        )
        for edge in selected_edges
    )
    selection = DaySettlementCandidateSelection(
        candidates=candidates,
        evaluated_pair_count=evaluated_pair_count,
        eligible_pair_count=eligible_pair_count,
        dropped_by_bound_count=eligible_pair_count - len(candidates),
    )
    selection.apply_to(plan)
    return selection
