"""Append-only period-view contracts for Memory V2.

Calendar lineage and complete-period grouping borrow the useful shape of
AionsHome's calendar compression (MIT).  Unlike destructive compression, these
drafts define rebuildable views over immutable MIRROW events and never archive
their inputs.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
import json
from typing import Any, Literal, Mapping


PeriodKind = Literal["day", "week", "month"]
PeriodItemKind = Literal["timeline", "continuity", "state_change"]
PeriodDateBasis = Literal["active_date", "calendar_date"]
PeriodJobState = Literal["planned", "running", "completed", "error"]


def period_input_digest(
    *,
    event_ids: tuple[str, ...] = (),
    parent_summary_ids: tuple[str, ...] = (),
) -> str:
    """Hash one exact lineage manifest using the store's canonical shape."""

    payload = {
        "event_ids": list(event_ids),
        "parent_summary_ids": list(parent_summary_ids),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PeriodWindow:
    kind: PeriodKind
    key: str
    date_from: str
    date_to: str


@dataclass(frozen=True)
class PeriodGenerationCandidateReceipt:
    candidate_order: int
    period_key: str
    revision: int
    input_digest: str
    input_count: int
    model_call_required: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.candidate_order, bool)
            or not isinstance(self.candidate_order, int)
            or self.candidate_order < 0
        ):
            raise ValueError("candidate_order must be a non-negative integer")
        if not self.period_key.strip() or not self.input_digest.strip():
            raise ValueError("period candidate identity fields are required")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise ValueError("period candidate revision must be positive")
        if (
            isinstance(self.input_count, bool)
            or not isinstance(self.input_count, int)
            or self.input_count <= 0
        ):
            raise ValueError("period candidate input_count must be positive")
        if not isinstance(self.model_call_required, bool):
            raise ValueError("model_call_required must be boolean")


@dataclass(frozen=True)
class PeriodGenerationJobDraft:
    namespace: str
    period_kind: PeriodKind
    date_basis: PeriodDateBasis
    reference_date: str
    generator_version: str
    prompt_version: str
    plan_digest: str
    candidates: tuple[PeriodGenerationCandidateReceipt, ...] = ()
    job_id: str = ""

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ValueError("period job namespace is required")
        if self.period_kind not in {"day", "week", "month"}:
            raise ValueError("period job kind must be day, week, or month")
        if self.date_basis not in {"active_date", "calendar_date"}:
            raise ValueError("period job date_basis is invalid")
        try:
            date.fromisoformat(self.reference_date)
        except ValueError as exc:
            raise ValueError("period job reference_date must be an ISO date") from exc
        if not self.generator_version.strip() or not self.prompt_version.strip():
            raise ValueError("period job generator and prompt versions are required")
        if not self.plan_digest.strip():
            raise ValueError("period job plan_digest is required")
        orders = tuple(candidate.candidate_order for candidate in self.candidates)
        if orders != tuple(range(len(self.candidates))):
            raise ValueError("period job candidates must be contiguous from zero")
        keys = tuple(candidate.period_key for candidate in self.candidates)
        if len(set(keys)) != len(keys):
            raise ValueError("period job candidate keys must be unique")


def calendar_period_window(kind: PeriodKind, member_date: date) -> PeriodWindow:
    """Resolve calendar periods from an already-authoritative stored date."""

    if kind == "day":
        start = end = member_date
        key = member_date.isoformat()
    elif kind == "week":
        start = member_date - timedelta(days=member_date.weekday())
        end = start + timedelta(days=6)
        iso_year, iso_week, _ = start.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
    elif kind == "month":
        start = member_date.replace(day=1)
        end = member_date.replace(day=monthrange(member_date.year, member_date.month)[1])
        key = f"{member_date.year:04d}-{member_date.month:02d}"
    else:
        raise ValueError("period kind must be day, week, or month")
    return PeriodWindow(
        kind=kind,
        key=key,
        date_from=start.isoformat(),
        date_to=end.isoformat(),
    )


def _clean_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    raw = tuple(str(value or "") for value in values)
    clean = tuple(value.strip() for value in raw)
    if any(not value for value in clean):
        raise ValueError(f"{field_name} must not contain empty IDs")
    if len(set(clean)) != len(clean):
        raise ValueError(f"{field_name} must contain unique IDs")
    if raw != clean:
        raise ValueError(f"{field_name} must contain canonical IDs without whitespace")
    return clean


@dataclass(frozen=True)
class PeriodSummaryItemDraft:
    ordinal: int
    item_kind: PeriodItemKind
    summary: str
    importance: float = 0.5
    confidence: float = 1.0
    source_event_ids: tuple[str, ...] = ()
    source_item_ids: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("period item ordinal must be a non-negative integer")
        if self.item_kind not in {"timeline", "continuity", "state_change"}:
            raise ValueError("unsupported period item kind")
        if not self.summary.strip():
            raise ValueError("period item summary must not be empty")
        if not 0 <= float(self.importance) <= 1:
            raise ValueError("period item importance must be within 0..1")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("period item confidence must be within 0..1")
        events = _clean_ids(self.source_event_ids, "source_event_ids")
        items = _clean_ids(self.source_item_ids, "source_item_ids")
        if bool(events) == bool(items):
            raise ValueError(
                "period item must cite exactly one lineage layer: events or parent items"
            )
        if not isinstance(self.attributes, Mapping):
            raise ValueError("period item attributes must be an object")


@dataclass(frozen=True)
class PeriodSummaryDraft:
    namespace: str
    period_kind: PeriodKind
    period_key: str
    revision: int
    date_from: str
    date_to: str
    generator_version: str
    prompt_version: str
    date_basis: PeriodDateBasis = "active_date"
    input_event_ids: tuple[str, ...] = ()
    input_parent_summary_ids: tuple[str, ...] = ()
    items: tuple[PeriodSummaryItemDraft, ...] = ()
    summary_id: str = ""

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ValueError("period namespace must not be empty")
        if self.period_kind not in {"day", "week", "month"}:
            raise ValueError("period kind must be day, week, or month")
        if self.date_basis not in {"active_date", "calendar_date"}:
            raise ValueError("period date_basis must be active_date or calendar_date")
        if not self.period_key.strip():
            raise ValueError("period key must not be empty")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision <= 0:
            raise ValueError("period revision must be a positive integer")
        try:
            start = date.fromisoformat(self.date_from)
            end = date.fromisoformat(self.date_to)
        except ValueError as exc:
            raise ValueError("period dates must be ISO dates") from exc
        if start > end:
            raise ValueError("period date_from must not be after date_to")
        window = calendar_period_window(self.period_kind, start)
        if (
            window.key != self.period_key
            or window.date_from != self.date_from
            or window.date_to != self.date_to
        ):
            raise ValueError("period key and bounds must match the calendar period")
        if not self.generator_version.strip() or not self.prompt_version.strip():
            raise ValueError("period generator and prompt versions are required")
        events = _clean_ids(self.input_event_ids, "input_event_ids")
        parents = _clean_ids(
            self.input_parent_summary_ids, "input_parent_summary_ids"
        )
        if self.period_kind == "day":
            if not events or parents:
                raise ValueError("day summaries require event inputs only")
        elif events or not parents:
            raise ValueError("week and month summaries require parent summaries only")
        ordinals = tuple(item.ordinal for item in self.items)
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("period item ordinals must be unique")
        if ordinals and sorted(ordinals) != list(range(len(ordinals))):
            raise ValueError("period item ordinals must be contiguous from zero")
        expected_source = "events" if self.period_kind == "day" else "items"
        for item in self.items:
            actual_source = "events" if item.source_event_ids else "items"
            if actual_source != expected_source:
                raise ValueError(
                    f"{self.period_kind} items must cite parent {expected_source}"
                )


__all__ = [
    "PeriodItemKind",
    "PeriodGenerationCandidateReceipt",
    "PeriodGenerationJobDraft",
    "PeriodJobState",
    "PeriodDateBasis",
    "PeriodKind",
    "PeriodSummaryDraft",
    "PeriodSummaryItemDraft",
    "PeriodWindow",
    "calendar_period_window",
    "period_input_digest",
]
