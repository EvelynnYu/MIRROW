"""Model-free contracts for one completed active-day settlement.

This module deliberately does not build prompts, call a provider, write SQLite,
or decide which candidates are plausible.  It only freezes the bounded input
shape and validates a future provider result against code-selected references.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from .models import SourceRef


DAY_SETTLEMENT_VERSION = "completed-active-day-settlement-v10"
DAY_SETTLEMENT_SCHEMA_NAME = "memory_v2_completed_active_day_settlement"
MIN_CONTINUATION_CONFIDENCE = 0.84
# The original heavy-day fixture produced 64 source-backed events.
# Keep one bounded whole-day call without forcing a second recursive summary layer.
MAX_DAY_EVENTS = 96
MAX_PRIOR_THREADS = 12
MAX_REPRESENTATIVE_EVENTS_PER_THREAD = 3
MAX_CONTINUATION_CANDIDATES = 24
MAX_DAY_COMPACT_ITEMS = 16
TARGET_DAY_COMPACT_EVENT_REFS = 12
MAX_DAY_COMPACT_SUMMARY_CHARS = 1400
MAX_THREAD_JUDGMENTS = 24

_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROOT_FIELDS = {"day_compact", "thread_judgments"}
_COMPACT_FIELDS = {"event_refs", "summary"}
_JUDGMENT_FIELDS = {"candidate_ref", "outcome", "confidence", "reason_codes"}
_OUTCOME_REASONS = {
    "continue": {
        "explicit_reference",
        "same_specific_occurrence",
        "direct_followup",
        "shared_distinctive_detail",
    },
    "separate": {
        "explicit_different_occurrence",
        "incompatible_identity_or_time",
    },
    "unknown": {
        "insufficient_evidence",
        "ambiguous_multiple_matches",
    },
}
_ALL_REASON_CODES = frozenset().union(*_OUTCOME_REASONS.values())


class DaySettlementContractError(ValueError):
    """A completed-day plan or result violates the settlement contract."""


def classify_thread_judgment_reasons(
    outcome: str,
    reason_codes: Sequence[str],
) -> str:
    """Classify reason structure so callers may conservatively handle mismatches."""

    normalized = tuple(str(item).strip() for item in reason_codes)
    if (
        outcome not in _OUTCOME_REASONS
        or not 1 <= len(normalized) <= 3
        or len(set(normalized)) != len(normalized)
        or any(item not in _ALL_REASON_CODES for item in normalized)
    ):
        return "invalid"
    if any(item not in _OUTCOME_REASONS[outcome] for item in normalized):
        return "mismatch"
    return "valid"


@dataclass(frozen=True)
class SettlementEvent:
    """A source-backed event summary; source message bodies are never copied."""

    ref: str
    event_id: str
    batch_id: str
    event_type: str
    subject_id: str
    summary: str
    reported_at: str
    sources: Sequence[SourceRef]
    participant_ids: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class SettlementThreadCandidate:
    """A prior append-only thread projected to at most three representative nodes.

    Nodes are chronological.  The final node is the only legal append endpoint;
    a continuation candidate may point at an earlier representative as its
    semantic match without rewriting or truncating the existing thread.
    """

    ref: str
    thread_id: str
    representative_events: Sequence[SettlementEvent]


@dataclass(frozen=True)
class SettlementContinuationCandidate:
    """One code-screened edge the later settlement model is allowed to judge.

    For a cross-day edge, ``prior_thread_ref`` names the existing thread and
    ``match_event_ref`` may be any projected representative node.  The accepted
    append endpoint is still that thread's latest projected node.  For an
    intra-day edge, ``prior_thread_ref`` is empty and ``match_event_ref`` names
    an earlier event from the same completed active day.
    """

    ref: str
    match_event_ref: str
    to_day_event_ref: str
    local_score: float
    signal_codes: Sequence[str]
    prior_thread_ref: str = ""


@dataclass(frozen=True)
class DaySettlementPlan:
    """Bounded input for one already-completed active date."""

    source_namespace: str
    active_date: str
    source_digest: str
    day_events: Sequence[SettlementEvent]
    prior_threads: Sequence[SettlementThreadCandidate] = field(default_factory=tuple)
    continuation_candidates: Sequence[SettlementContinuationCandidate] = field(
        default_factory=tuple
    )
    settlement_version: str = DAY_SETTLEMENT_VERSION

    def safe_dict(self) -> dict[str, Any]:
        """Return content-free diagnostics suitable for ordinary logs."""

        return {
            "source_namespace": self.source_namespace,
            "active_date": self.active_date,
            "day_event_count": len(self.day_events),
            "prior_thread_count": len(self.prior_threads),
            "continuation_candidate_count": len(self.continuation_candidates),
            "settlement_version": self.settlement_version,
            "input_digest": day_settlement_input_digest(self),
        }


@dataclass(frozen=True)
class DayCompactItem:
    ordinal: int
    summary: str
    event_refs: tuple[str, ...]
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class DayThreadJudgment:
    candidate_ref: str
    outcome: str
    confidence: float
    reason_codes: tuple[str, ...]
    from_event_id: str
    match_event_id: str
    to_event_id: str
    prior_thread_id: str = ""


@dataclass(frozen=True)
class ParsedDaySettlement:
    day_compact: tuple[DayCompactItem, ...]
    thread_judgments: tuple[DayThreadJudgment, ...]

    def accepted_continuations(
        self, minimum_confidence: float = MIN_CONTINUATION_CONFIDENCE
    ) -> tuple[DayThreadJudgment, ...]:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum confidence must be between 0 and 1")
        return tuple(
            item
            for item in self.thread_judgments
            if item.outcome == "continue" and item.confidence >= minimum_confidence
        )


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise DaySettlementContractError("event reported_at must be ISO-8601") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validate_ref(value: str, label: str) -> None:
    if not _REF_PATTERN.fullmatch(str(value or "")):
        raise DaySettlementContractError(f"{label} must be a short stable reference")


def _validate_event(event: SettlementEvent, *, label: str) -> None:
    _validate_ref(event.ref, f"{label} ref")
    if not event.event_id.strip() or not event.batch_id.strip() or not event.event_type.strip():
        raise DaySettlementContractError(f"{label} requires event, batch, and type identity")
    if not event.subject_id.strip() or not event.summary.strip():
        raise DaySettlementContractError(f"{label} requires subject and summary")
    if len(event.summary) > 600:
        raise DaySettlementContractError(f"{label} summary exceeds 600 characters")
    _timestamp(event.reported_at)
    if not event.sources:
        raise DaySettlementContractError(f"{label} requires exact source references")
    seen_sources: set[tuple[int, str]] = set()
    for source in event.sources:
        key = (source.message_row_id, source.message_id)
        if source.message_row_id <= 0 or not source.message_id.strip():
            raise DaySettlementContractError(f"{label} has an invalid source identity")
        if not source.session_id.strip() or not source.source_ts.strip():
            raise DaySettlementContractError(f"{label} has an incomplete source reference")
        if key in seen_sources:
            raise DaySettlementContractError(f"{label} repeats a source reference")
        seen_sources.add(key)


def validate_day_settlement_plan(plan: DaySettlementPlan) -> None:
    """Validate a bounded, summary-only settlement input."""

    if not plan.source_namespace.strip() or not plan.settlement_version.strip():
        raise DaySettlementContractError("settlement namespace and version are required")
    try:
        date.fromisoformat(plan.active_date)
    except ValueError as exc:
        raise DaySettlementContractError("active_date must be an ISO date") from exc
    if not _DIGEST_PATTERN.fullmatch(plan.source_digest):
        raise DaySettlementContractError("source_digest must be a lowercase SHA-256")
    if not 1 <= len(plan.day_events) <= MAX_DAY_EVENTS:
        raise DaySettlementContractError("completed-day plan has an invalid event count")
    if len(plan.prior_threads) > MAX_PRIOR_THREADS:
        raise DaySettlementContractError("too many prior thread candidates")
    if len(plan.continuation_candidates) > MAX_CONTINUATION_CANDIDATES:
        raise DaySettlementContractError("too many continuation candidates")

    day_by_ref: dict[str, SettlementEvent] = {}
    all_event_refs: set[str] = set()
    all_event_ids: set[str] = set()
    for event in plan.day_events:
        _validate_event(event, label="day event")
        if event.ref in all_event_refs or event.event_id in all_event_ids:
            raise DaySettlementContractError("day events require unique refs and IDs")
        day_by_ref[event.ref] = event
        all_event_refs.add(event.ref)
        all_event_ids.add(event.event_id)

    threads_by_ref: dict[str, SettlementThreadCandidate] = {}
    prior_event_owner: dict[str, str] = {}
    prior_event_by_ref: dict[str, SettlementEvent] = {}
    seen_thread_ids: set[str] = set()
    for thread in plan.prior_threads:
        _validate_ref(thread.ref, "prior thread ref")
        if thread.ref in threads_by_ref or not thread.thread_id.strip():
            raise DaySettlementContractError("prior threads require unique refs and IDs")
        if thread.thread_id in seen_thread_ids:
            raise DaySettlementContractError("prior thread IDs must be unique")
        if not 1 <= len(thread.representative_events) <= MAX_REPRESENTATIVE_EVENTS_PER_THREAD:
            raise DaySettlementContractError("prior thread projection must have one to three nodes")
        previous_time: datetime | None = None
        for event in thread.representative_events:
            _validate_event(event, label="prior thread event")
            if event.ref in all_event_refs or event.event_id in all_event_ids:
                raise DaySettlementContractError("settlement event refs and IDs must be unique")
            event_time = _timestamp(event.reported_at)
            if previous_time is not None and event_time < previous_time:
                raise DaySettlementContractError("prior thread nodes must be chronological")
            previous_time = event_time
            all_event_refs.add(event.ref)
            all_event_ids.add(event.event_id)
            prior_event_owner[event.ref] = thread.ref
            prior_event_by_ref[event.ref] = event
        threads_by_ref[thread.ref] = thread
        seen_thread_ids.add(thread.thread_id)

    seen_candidate_refs: set[str] = set()
    seen_candidate_edges: set[tuple[str, str, str]] = set()
    for candidate in plan.continuation_candidates:
        _validate_ref(candidate.ref, "continuation candidate ref")
        if candidate.ref in seen_candidate_refs:
            raise DaySettlementContractError("continuation candidate refs must be unique")
        if not math.isfinite(candidate.local_score) or not 0 <= candidate.local_score <= 1:
            raise DaySettlementContractError("candidate local_score must be between 0 and 1")
        if not 1 <= len(candidate.signal_codes) <= 4:
            raise DaySettlementContractError("candidate requires one to four local signals")
        if len(set(candidate.signal_codes)) != len(candidate.signal_codes) or any(
            not _REF_PATTERN.fullmatch(str(code)) for code in candidate.signal_codes
        ):
            raise DaySettlementContractError("candidate signal codes must be unique identifiers")
        if candidate.to_day_event_ref not in day_by_ref:
            raise DaySettlementContractError("candidate target must be a current-day event")

        if candidate.prior_thread_ref:
            thread = threads_by_ref.get(candidate.prior_thread_ref)
            if thread is None:
                raise DaySettlementContractError("candidate names an unknown prior thread")
            if prior_event_owner.get(candidate.match_event_ref) != thread.ref:
                raise DaySettlementContractError("candidate match is outside its prior thread")
            from_ref = thread.representative_events[-1].ref
            if _timestamp(thread.representative_events[-1].reported_at) > _timestamp(
                day_by_ref[candidate.to_day_event_ref].reported_at
            ):
                raise DaySettlementContractError("cross-day continuation must point forward")
        else:
            if candidate.match_event_ref not in day_by_ref:
                raise DaySettlementContractError("intra-day candidate match must be a day event")
            from_ref = candidate.match_event_ref
            if _timestamp(day_by_ref[from_ref].reported_at) > _timestamp(
                day_by_ref[candidate.to_day_event_ref].reported_at
            ):
                raise DaySettlementContractError("intra-day continuation must point forward")
        if from_ref == candidate.to_day_event_ref:
            raise DaySettlementContractError("continuation candidate cannot self-link")
        edge = (candidate.prior_thread_ref, from_ref, candidate.to_day_event_ref)
        if edge in seen_candidate_edges:
            raise DaySettlementContractError("continuation candidate edge is duplicated")
        seen_candidate_refs.add(candidate.ref)
        seen_candidate_edges.add(edge)


def _source_payload(source: SourceRef) -> dict[str, Any]:
    return {
        "message_row_id": source.message_row_id,
        "message_id": source.message_id,
        "session_id": source.session_id,
        "source_ts": source.source_ts,
        "source_role": source.source_role,
        "source_kind": source.source_kind,
        "source_event_type": source.source_event_type,
        "scene_id": source.scene_id,
        "span_start": source.span_start,
        "span_end": source.span_end,
        "span_digest": source.span_digest,
    }


def _event_payload(event: SettlementEvent) -> dict[str, Any]:
    return {
        "ref": event.ref,
        "event_id": event.event_id,
        "batch_id": event.batch_id,
        "event_type": event.event_type,
        "subject_id": event.subject_id,
        "participant_ids": list(event.participant_ids),
        "summary": event.summary,
        "reported_at": event.reported_at,
        "sources": [_source_payload(source) for source in event.sources],
    }


def day_settlement_input_payload(plan: DaySettlementPlan) -> dict[str, Any]:
    """Return the exact summary-and-anchor input; it never contains source bodies."""

    validate_day_settlement_plan(plan)
    return {
        "settlement_version": plan.settlement_version,
        "source_namespace": plan.source_namespace,
        "active_date": plan.active_date,
        "source_digest": plan.source_digest,
        "day_events": [_event_payload(event) for event in plan.day_events],
        "prior_threads": [
            {
                "ref": thread.ref,
                "thread_id": thread.thread_id,
                "representative_events": [
                    _event_payload(event) for event in thread.representative_events
                ],
            }
            for thread in plan.prior_threads
        ],
        "continuation_candidates": [
            {
                "ref": candidate.ref,
                "prior_thread_ref": candidate.prior_thread_ref,
                "match_event_ref": candidate.match_event_ref,
                "to_day_event_ref": candidate.to_day_event_ref,
                "local_score": round(candidate.local_score, 4),
                "signal_codes": list(candidate.signal_codes),
            }
            for candidate in plan.continuation_candidates
        ],
    }


def day_settlement_input_digest(plan: DaySettlementPlan) -> str:
    canonical = json.dumps(
        day_settlement_input_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_day_settlement_id(plan: DaySettlementPlan) -> str:
    digest = day_settlement_input_digest(plan)
    identity = "\0".join(
        (plan.source_namespace, plan.active_date, plan.settlement_version, digest)
    )
    return "day_settlement_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def day_settlement_response_schema() -> dict[str, Any]:
    """Closed JSON schema for a future single completed-day model call."""

    compact_item = {
        "type": "object",
        "properties": {
            "event_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_DAY_EVENTS,
                "items": {"type": "string"},
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_DAY_COMPACT_SUMMARY_CHARS,
            },
        },
        "required": sorted(_COMPACT_FIELDS),
        "additionalProperties": False,
    }
    judgment = {
        "type": "object",
        "properties": {
            "candidate_ref": {"type": "string"},
            "outcome": {"type": "string", "enum": sorted(_OUTCOME_REASONS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason_codes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "enum": sorted(_ALL_REASON_CODES)},
            },
        },
        "required": sorted(_JUDGMENT_FIELDS),
        "additionalProperties": False,
    }
    return {
        "name": DAY_SETTLEMENT_SCHEMA_NAME,
        "schema": {
            "type": "object",
            "properties": {
                "day_compact": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_DAY_COMPACT_ITEMS,
                    "items": compact_item,
                },
                "thread_judgments": {
                    "type": "array",
                    "maxItems": MAX_THREAD_JUDGMENTS,
                    "items": judgment,
                },
            },
            "required": sorted(_ROOT_FIELDS),
            "additionalProperties": False,
        },
        "strict": True,
    }


def _as_object(value: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise DaySettlementContractError("settlement output must be a JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DaySettlementContractError(f"invalid settlement JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise DaySettlementContractError("settlement output must be a JSON object")
    return parsed


def parse_day_settlement_output(
    value: str | Mapping[str, Any], plan: DaySettlementPlan
) -> ParsedDaySettlement:
    """Map only declared short refs back to immutable IDs.

    Omitted candidates stay unjudged.  ``unknown`` is preserved explicitly;
    neither omission nor uncertainty is converted into event resolution.
    """

    validate_day_settlement_plan(plan)
    body = _as_object(value)
    if set(body) != _ROOT_FIELDS:
        raise DaySettlementContractError("settlement output has missing or unknown root fields")
    raw_compact = body["day_compact"]
    raw_judgments = body["thread_judgments"]
    if not isinstance(raw_compact, list) or not 1 <= len(raw_compact) <= MAX_DAY_COMPACT_ITEMS:
        raise DaySettlementContractError("day_compact must contain one to sixteen items")
    if not isinstance(raw_judgments, list) or len(raw_judgments) > MAX_THREAD_JUDGMENTS:
        raise DaySettlementContractError("thread_judgments exceeds its bound")

    day_by_ref = {event.ref: event for event in plan.day_events}
    used_day_refs: set[str] = set()
    compact_items: list[DayCompactItem] = []
    for ordinal, raw in enumerate(raw_compact):
        if not isinstance(raw, Mapping) or set(raw) != _COMPACT_FIELDS:
            raise DaySettlementContractError("day compact item has missing or unknown fields")
        raw_refs = raw["event_refs"]
        summary = str(raw["summary"] or "").strip()
        if not isinstance(raw_refs, list) or not raw_refs:
            raise DaySettlementContractError("day compact item requires event refs")
        event_refs = tuple(str(ref).strip() for ref in raw_refs)
        if len(set(event_refs)) != len(event_refs) or any(
            ref not in day_by_ref for ref in event_refs
        ):
            raise DaySettlementContractError("day compact item has invalid or duplicate refs")
        if used_day_refs.intersection(event_refs):
            raise DaySettlementContractError("a day event cannot appear in two compact items")
        if not summary:
            raise DaySettlementContractError("day compact summary is empty")
        if len(summary) > MAX_DAY_COMPACT_SUMMARY_CHARS:
            raise DaySettlementContractError(
                "day compact summary exceeds "
                f"{MAX_DAY_COMPACT_SUMMARY_CHARS} characters length {len(summary)}"
            )
        used_day_refs.update(event_refs)
        compact_items.append(
            DayCompactItem(
                ordinal=ordinal,
                summary=summary,
                event_refs=event_refs,
                event_ids=tuple(day_by_ref[ref].event_id for ref in event_refs),
            )
        )
    threads_by_ref = {thread.ref: thread for thread in plan.prior_threads}
    day_event_by_ref = {event.ref: event for event in plan.day_events}
    candidate_by_ref = {
        candidate.ref: candidate for candidate in plan.continuation_candidates
    }
    used_candidates: set[str] = set()
    continued_from: set[str] = set()
    continued_to: set[str] = set()
    judgments: list[DayThreadJudgment] = []
    for raw in raw_judgments:
        if not isinstance(raw, Mapping) or set(raw) != _JUDGMENT_FIELDS:
            raise DaySettlementContractError("thread judgment has missing or unknown fields")
        candidate_ref = str(raw["candidate_ref"] or "").strip()
        candidate = candidate_by_ref.get(candidate_ref)
        if candidate is None or candidate_ref in used_candidates:
            raise DaySettlementContractError("thread judgment has unknown or duplicate candidate")
        outcome = str(raw["outcome"] or "").strip()
        if outcome not in _OUTCOME_REASONS:
            raise DaySettlementContractError("thread judgment has an invalid outcome")
        try:
            confidence = float(raw["confidence"])
        except (TypeError, ValueError) as exc:
            raise DaySettlementContractError("thread judgment confidence must be numeric") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise DaySettlementContractError("thread judgment confidence must be between 0 and 1")
        raw_reasons = raw["reason_codes"]
        if not isinstance(raw_reasons, list) or not 1 <= len(raw_reasons) <= 3:
            raise DaySettlementContractError("thread judgment requires one to three reasons")
        reason_codes = tuple(str(item).strip() for item in raw_reasons)
        if classify_thread_judgment_reasons(outcome, reason_codes) != "valid":
            raise DaySettlementContractError("thread judgment reasons do not match its outcome")

        if candidate.prior_thread_ref:
            thread = threads_by_ref[candidate.prior_thread_ref]
            match_event = next(
                event
                for event in thread.representative_events
                if event.ref == candidate.match_event_ref
            )
            from_event = thread.representative_events[-1]
            prior_thread_id = thread.thread_id
        else:
            match_event = day_event_by_ref[candidate.match_event_ref]
            from_event = match_event
            prior_thread_id = ""
        to_event = day_event_by_ref[candidate.to_day_event_ref]
        if outcome == "continue":
            if from_event.ref in continued_from or to_event.ref in continued_to:
                raise DaySettlementContractError(
                    "accepted continuations must remain a linear chain"
                )
            continued_from.add(from_event.ref)
            continued_to.add(to_event.ref)
        used_candidates.add(candidate_ref)
        judgments.append(
            DayThreadJudgment(
                candidate_ref=candidate_ref,
                outcome=outcome,
                confidence=round(confidence, 4),
                reason_codes=reason_codes,
                from_event_id=from_event.event_id,
                match_event_id=match_event.event_id,
                to_event_id=to_event.event_id,
                prior_thread_id=prior_thread_id,
            )
        )
    return ParsedDaySettlement(tuple(compact_items), tuple(judgments))
