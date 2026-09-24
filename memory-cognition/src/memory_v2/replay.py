"""Deterministic, content-safe planning for offline Memory V2 replay."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .conversation_source import (
    DEFAULT_MEMORY_ROLES,
    ConversationMessage,
    ConversationSource,
    digest_messages,
)
from .models import EncodingBatch
from .models import BatchSourceRef


class ReplayPlanningError(RuntimeError):
    """The selected raw range cannot be represented as replay-safe batches."""


DEFAULT_MAX_MESSAGES = 96
DEFAULT_MAX_ESTIMATED_TOKENS = 6_000
DEFAULT_SOFT_MIN_MESSAGES = 36
DEFAULT_SOFT_GAP_SECONDS = 0


@dataclass(frozen=True)
class ReplayConfig:
    max_messages: int = DEFAULT_MAX_MESSAGES
    max_estimated_tokens: int = DEFAULT_MAX_ESTIMATED_TOKENS
    soft_min_messages: int = DEFAULT_SOFT_MIN_MESSAGES
    soft_gap_seconds: int = DEFAULT_SOFT_GAP_SECONDS
    encoder_version: str = "event-encoder-v2-lightweight-2"
    prompt_version: str = "episode-encoding-source-provenance-quality-v38"
    source_namespace: str = "mainline"
    roles: tuple[str, ...] = DEFAULT_MEMORY_ROLES

    def validate(self) -> None:
        if self.max_messages <= 0 or self.max_estimated_tokens <= 0:
            raise ValueError("hard replay limits must be positive")
        if self.soft_min_messages <= 0 or self.soft_min_messages > self.max_messages:
            raise ValueError("soft_min_messages must be within the message limit")
        if self.soft_gap_seconds < 0:
            raise ValueError("soft_gap_seconds must not be negative")
        if not self.encoder_version or not self.prompt_version or not self.source_namespace:
            raise ValueError("replay identity versions must not be empty")
        if not self.roles:
            raise ValueError("replay roles must not be empty")


@dataclass(frozen=True)
class ReplayBatchPlan:
    batch: EncodingBatch
    batch_sources: tuple[BatchSourceRef, ...]
    estimated_tokens: int
    content_chars: int
    boundary_reason: str
    oversized_single_message: bool
    root_batch_id: str = ""
    parent_batch_id: str = ""
    shard_depth: int = 0

    def safe_dict(self) -> dict:
        """Return observability fields without source bodies."""

        return {
            "batch_id": self.batch.batch_id,
            "session_id": self.batch.session_id,
            "active_date": self.batch.active_date,
            "from_message_row_id": self.batch.from_message_row_id,
            "to_message_row_id": self.batch.to_message_row_id,
            "from_message_id": self.batch.from_message_id,
            "to_message_id": self.batch.to_message_id,
            "source_count": self.batch.source_count,
            "source_digest": self.batch.source_digest,
            "estimated_tokens": self.estimated_tokens,
            "content_chars": self.content_chars,
            "boundary_reason": self.boundary_reason,
            "oversized_single_message": self.oversized_single_message,
            "root_batch_id": self.root_batch_id or self.batch.batch_id,
            "parent_batch_id": self.parent_batch_id,
            "shard_depth": self.shard_depth,
        }


@dataclass(frozen=True)
class ReplayDayPlan:
    active_date: str
    roles: tuple[str, ...]
    message_count: int
    session_count: int
    role_counts: dict[str, int]
    content_chars: int
    estimated_tokens: int
    first_timestamp: str
    last_timestamp: str
    batches: tuple[ReplayBatchPlan, ...]

    def safe_dict(self) -> dict:
        return {
            "active_date": self.active_date,
            "roles": list(self.roles),
            "message_count": self.message_count,
            "session_count": self.session_count,
            "role_counts": dict(self.role_counts),
            "content_chars": self.content_chars,
            "estimated_tokens": self.estimated_tokens,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "batch_count": len(self.batches),
            "batches": [batch.safe_dict() for batch in self.batches],
        }


def estimate_tokens(text: str) -> int:
    """Match MIRROW's existing inexpensive CJK/ASCII planning estimate."""

    if not text:
        return 0
    cjk = sum(1 for char in text if "一" <= char <= "鿿" or "　" <= char <= "〿")
    ascii_chars = len(text) - cjk
    return int(cjk / 1.5 + ascii_chars / 4)


def _utc_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def _gap_seconds(left: ConversationMessage, right: ConversationMessage) -> float | None:
    left_ts = _utc_timestamp(left.timestamp)
    right_ts = _utc_timestamp(right.timestamp)
    if left_ts is None or right_ts is None:
        return None
    return (right_ts - left_ts).total_seconds()


class ReplayPlanner:
    """Split raw rows into deterministic technical batches, not semantic scenes."""

    def __init__(self, source: ConversationSource, config: ReplayConfig | None = None):
        self.source = source
        self.config = config or ReplayConfig()
        self.config.validate()

    def plan_active_date(self, active_date: str) -> ReplayDayPlan:
        messages = self.source.read_active_date(active_date, roles=self.config.roles)
        missing_ids = [message.row_id for message in messages if not message.message_id]
        if missing_ids:
            raise ReplayPlanningError(
                f"active date {active_date} has {len(missing_ids)} source rows without message_id"
            )

        by_session: dict[str, list[ConversationMessage]] = {}
        for message in messages:
            if not message.session_id:
                raise ReplayPlanningError(
                    f"source row {message.row_id} has no session_id"
                )
            by_session.setdefault(message.session_id, []).append(message)

        batch_plans: list[ReplayBatchPlan] = []
        ordered_sessions = sorted(
            by_session.values(),
            key=lambda rows: (rows[0].timestamp, rows[0].row_id),
        )
        for session_messages in ordered_sessions:
            batch_plans.extend(self._plan_session(session_messages))

        return ReplayDayPlan(
            active_date=active_date,
            roles=tuple(self.config.roles),
            message_count=len(messages),
            session_count=len(by_session),
            role_counts=dict(sorted(Counter(message.role for message in messages).items())),
            content_chars=sum(len(message.content) for message in messages),
            estimated_tokens=sum(estimate_tokens(message.content) for message in messages),
            first_timestamp=messages[0].timestamp if messages else "",
            last_timestamp=messages[-1].timestamp if messages else "",
            batches=tuple(batch_plans),
        )

    def _plan_session(
        self, messages: Sequence[ConversationMessage]
    ) -> list[ReplayBatchPlan]:
        plans: list[ReplayBatchPlan] = []
        current: list[ConversationMessage] = []
        current_tokens = 0

        def finish(reason: str) -> None:
            nonlocal current, current_tokens
            if not current:
                return
            plans.append(self._build_batch(current, current_tokens, reason))
            current = []
            current_tokens = 0

        for index, message in enumerate(messages):
            message_tokens = estimate_tokens(message.content)
            causal_pair = bool(
                current
                and current[-1].event_type == "ambient_listening_observation"
                and message.event_type == "ambient_listening_reply"
            )
            if current and not causal_pair:
                if len(current) >= self.config.max_messages:
                    finish("max_messages")
                elif current_tokens + message_tokens > self.config.max_estimated_tokens:
                    finish("max_estimated_tokens")

            current.append(message)
            current_tokens += message_tokens

            if index + 1 < len(messages):
                gap = _gap_seconds(message, messages[index + 1])
                if (
                    not (message.event_type == "ambient_listening_observation"
                         and messages[index + 1].event_type == "ambient_listening_reply")
                    and
                    self.config.soft_gap_seconds > 0
                    and len(current) >= self.config.soft_min_messages
                    and gap is not None
                    and gap >= self.config.soft_gap_seconds
                ):
                    finish("soft_gap")

        finish("end_of_range")
        return plans

    def _build_batch(
        self,
        messages: Sequence[ConversationMessage],
        estimated_tokens: int,
        boundary_reason: str,
    ) -> ReplayBatchPlan:
        first = messages[0]
        last = messages[-1]
        batch = EncodingBatch(
            source_namespace=self.config.source_namespace,
            session_id=first.session_id,
            active_date=first.active_date,
            from_message_row_id=first.row_id,
            to_message_row_id=last.row_id,
            from_message_id=first.message_id,
            to_message_id=last.message_id,
            source_count=len(messages),
            source_digest=digest_messages(messages),
            encoder_version=self.config.encoder_version,
            prompt_version=self.config.prompt_version,
        )
        batch_id = self.source_batch_id(batch)
        batch = EncodingBatch(**{**batch.__dict__, "batch_id": batch_id})
        return ReplayBatchPlan(
            batch=batch,
            batch_sources=tuple(
                message.as_batch_source_ref() for message in messages
            ),
            estimated_tokens=estimated_tokens,
            content_chars=sum(len(message.content) for message in messages),
            boundary_reason=boundary_reason,
            oversized_single_message=(
                len(messages) == 1
                and estimated_tokens > self.config.max_estimated_tokens
            ),
        )

    @staticmethod
    def source_batch_id(batch: EncodingBatch) -> str:
        # Local import keeps the read-only planner independent of store setup.
        from .store import MemoryV2Store

        return MemoryV2Store.resolve_batch_id(batch)

    def validate(self, plan: ReplayDayPlan) -> bool:
        return all(
            self.source.validate_batch(
                batch_plan.batch, batch_sources=batch_plan.batch_sources
            )
            for batch_plan in plan.batches
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan a read-only Memory V2 replay")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--date", action="append", dest="dates", required=True)
    parser.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES)
    parser.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_ESTIMATED_TOKENS
    )
    parser.add_argument(
        "--soft-min-messages", type=int, default=DEFAULT_SOFT_MIN_MESSAGES
    )
    parser.add_argument(
        "--soft-gap-seconds",
        type=int,
        default=DEFAULT_SOFT_GAP_SECONDS,
        help="optional technical split threshold; 0 keeps semantic pauses out of encoding batches",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    source = ConversationSource(args.db)
    planner = ReplayPlanner(
        source,
        ReplayConfig(
            max_messages=args.max_messages,
            max_estimated_tokens=args.max_tokens,
            soft_min_messages=args.soft_min_messages,
            soft_gap_seconds=args.soft_gap_seconds,
        ),
    )
    reports = []
    for active_date in args.dates:
        plan = planner.plan_active_date(active_date)
        report = plan.safe_dict()
        report["source_validation"] = planner.validate(plan)
        reports.append(report)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
