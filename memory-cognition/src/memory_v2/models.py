"""Write contracts for the Memory V2 storage boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SourceRef:
    """A verified pointer into the authoritative conversation store.

    ``selected_span`` is intentionally represented only by offsets and a
    digest.  Memory V2 does not copy the private source body into its event
    index.
    """

    message_row_id: int
    message_id: str
    session_id: str
    source_ts: str
    source_role: str
    source_kind: str = "chat"
    source_event_type: str = ""
    scene_id: str = ""
    span_start: int | None = None
    span_end: int | None = None
    span_digest: str = ""


@dataclass(frozen=True)
class BatchSourceRef:
    """One exact member of an encoding batch, without its private body."""

    message_row_id: int
    message_id: str
    session_id: str
    active_date: str
    calendar_date: str
    source_ts: str
    source_role: str
    content_digest: str
    source_kind: str = "chat"
    source_event_type: str = ""


def digest_batch_sources(sources: Sequence[BatchSourceRef]) -> str:
    payload = [
        {
            "message_row_id": source.message_row_id,
            "message_id": source.message_id,
            "session_id": source.session_id,
            "active_date": source.active_date,
            "calendar_date": source.calendar_date,
            "source_ts": source.source_ts,
            "source_role": source.source_role,
            "content_digest": source.content_digest,
            "source_kind": source.source_kind,
            "source_event_type": source.source_event_type,
        }
        for source in sources
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EncodingBatch:
    """One idempotent source range; it is not a semantic scene boundary."""

    source_namespace: str
    session_id: str
    active_date: str
    from_message_row_id: int
    to_message_row_id: int
    from_message_id: str
    to_message_id: str
    source_count: int
    source_digest: str
    encoder_version: str
    prompt_version: str
    batch_id: str = ""


@dataclass(frozen=True)
class EventDraft:
    """An immutable event occurrence produced by one encoding batch."""

    ordinal: int
    subject_id: str
    event_type: str
    summary: str
    reported_at: str
    active_date: str
    calendar_date: str
    sources: Sequence[SourceRef]
    participant_ids: Sequence[str] = field(default_factory=tuple)
    occurred_at: str = ""
    importance: float = 0.5
    emotional_weight: float = 0.0
    confidence: float = 1.0
    epistemic_status: str = "explicit_report"
    attributes: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""


@dataclass(frozen=True)
class EventLinkDraft:
    """One append-only semantic relation between two immutable events."""

    from_event_id: str
    to_event_id: str
    link_type: str
    confidence: float
    evidence: Mapping[str, Any] = field(default_factory=dict)
    link_id: str = ""


@dataclass(frozen=True)
class BoundaryLinkJudgmentDraft:
    """One content-free, completed judgment for a technical batch boundary.

    Absence of this record means the boundary has not been successfully judged.
    Candidate fields are populated only when the linker proposed one bridge.
    """

    boundary_id: str
    previous_batch_id: str
    next_batch_id: str
    source_namespace: str
    linker_version: str
    input_digest: str
    outcome: str
    minimum_confidence: float
    candidate_from_event_id: str = ""
    candidate_to_event_id: str = ""
    confidence: float | None = None
    reason_codes: Sequence[str] = field(default_factory=tuple)
    judgment_id: str = ""


@dataclass(frozen=True)
class DayCompactItemDraft:
    """One immutable compact item derived from a completed active day."""

    ordinal: int
    summary: str
    event_ids: Sequence[str]
    content_digest: str
    item_id: str = ""


@dataclass(frozen=True)
class DaySettlementCandidateDraft:
    """Frozen candidate identity used to validate a settlement at commit time."""

    candidate_ref: str
    from_event_id: str
    match_event_id: str
    to_event_id: str
    prior_thread_id: str = ""


@dataclass(frozen=True)
class DayThreadJudgmentDraft:
    """One returned day-settlement judgment; omission remains unjudged."""

    candidate_ref: str
    outcome: str
    confidence: float
    reason_codes: Sequence[str]
    from_event_id: str
    match_event_id: str
    to_event_id: str
    prior_thread_id: str = ""
    accepted_link: EventLinkDraft | None = None
    judgment_id: str = ""


@dataclass(frozen=True)
class DaySettlementDraft:
    """Atomic write contract for one successfully parsed completed-day result."""

    settlement_id: str
    source_namespace: str
    active_date: str
    settlement_version: str
    assembler_version: str
    prompt_version: str
    source_digest: str
    input_digest: str
    execution_digest: str
    result_digest: str
    minimum_continuation_confidence: float
    day_event_ids: Sequence[str]
    candidates: Sequence[DaySettlementCandidateDraft]
    compact_items: Sequence[DayCompactItemDraft]
    judgments: Sequence[DayThreadJudgmentDraft]
