"""Bounded, ephemeral source expansion for Memory V2 recall.

The summary-first, expand-on-explicit-detail policy and small context budgets
are adapted from AionsHome's active memory search (MIT).  MIRROW keeps raw
messages in this transient evidence object only; event, period, and cognition
views remain navigation layers while the conversation database remains the
verbatim authority for chat evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from behavior_scheduler.bookshelf_search import sanitize_archive_text

from .conversation_source import ConversationMessage, ConversationSource, digest_text
from .recall import RecallHit, RecallSourceAnchor


@dataclass(frozen=True)
class RecallEvidencePolicy:
    max_hits: int = 1
    max_anchors_per_hit: int = 1
    before: int = 1
    after: int = 1
    max_message_chars: int = 320
    max_block_chars: int = 1_200

    def __post_init__(self) -> None:
        if not 1 <= self.max_hits <= 10:
            raise ValueError("max_hits must be within 1..10")
        if not 1 <= self.max_anchors_per_hit <= 10:
            raise ValueError("max_anchors_per_hit must be within 1..10")
        if not 0 <= self.before <= 4 or not 0 <= self.after <= 4:
            raise ValueError("before and after must be within 0..4")
        if not 80 <= self.max_message_chars <= 2_000:
            raise ValueError("max_message_chars must be within 80..2000")
        if not 500 <= self.max_block_chars <= 12_000:
            raise ValueError("max_block_chars must be within 500..12000")


@dataclass(frozen=True)
class RecallEvidenceMessage:
    message_id: str
    timestamp: str
    role: str
    source_kind: str
    event_type: str
    content: str
    is_anchor: bool
    content_truncated: bool = False
    span_selected: bool = False
    span_fallback: bool = False


@dataclass(frozen=True)
class RecallEvidenceGroup:
    rank: int
    direct_match: bool
    requested_anchor_count: int
    missing_anchor_count: int
    omitted_anchor_count: int
    messages: tuple[RecallEvidenceMessage, ...]


@dataclass(frozen=True)
class RecallEvidenceBundle:
    expanded: bool
    reason: str
    groups: tuple[RecallEvidenceGroup, ...] = ()
    raw_content_chars: int = 0
    truncated: bool = False
    hard_char_limit: int = 1_800

    def safe_observation(self) -> dict[str, object]:
        return {
            "expanded": self.expanded,
            "reason": self.reason,
            "group_count": len(self.groups),
            "message_count": sum(len(group.messages) for group in self.groups),
            "span_selected_count": sum(
                message.span_selected
                for group in self.groups
                for message in group.messages
            ),
            "span_fallback_count": sum(
                message.span_fallback
                for group in self.groups
                for message in group.messages
            ),
            "requested_anchor_count": sum(
                group.requested_anchor_count for group in self.groups
            ),
            "missing_anchor_count": sum(
                group.missing_anchor_count for group in self.groups
            ),
            "omitted_anchor_count": sum(
                group.omitted_anchor_count for group in self.groups
            ),
            "raw_content_chars": self.raw_content_chars,
            "truncated": self.truncated,
            "hard_char_limit": self.hard_char_limit,
        }


def _to_evidence_message(
    message: ConversationMessage,
    *,
    anchor_ids: set[str],
    anchor_spans: dict[str, RecallSourceAnchor],
    max_chars: int,
    remaining_chars: int,
) -> RecallEvidenceMessage | None:
    if remaining_chars <= 0:
        return None
    content = sanitize_archive_text(message.content, limit=max(max_chars * 4, max_chars))
    span_selected = False
    span_fallback = False
    anchor = anchor_spans.get(message.message_id)
    if anchor is not None and anchor.span_start is not None and anchor.span_end is not None:
        if (
            0 <= anchor.span_start < anchor.span_end <= len(message.content)
            and anchor.span_digest
            and digest_text(message.content[anchor.span_start : anchor.span_end])
            == anchor.span_digest
        ):
            content = sanitize_archive_text(
                message.content[anchor.span_start : anchor.span_end],
                limit=max(max_chars * 4, max_chars),
            )
            span_selected = True
        else:
            span_fallback = True
    allowed = min(max_chars, remaining_chars)
    truncated = len(content) > allowed
    if truncated:
        content = content[:allowed]
    return RecallEvidenceMessage(
        message_id=message.message_id,
        timestamp=message.timestamp,
        role=message.role,
        source_kind=message.source_kind,
        event_type=message.event_type,
        content=content,
        is_anchor=message.message_id in anchor_ids,
        content_truncated=truncated,
        span_selected=span_selected,
        span_fallback=span_fallback,
    )


def expand_recall_evidence(
    source: ConversationSource,
    hits: Sequence[RecallHit],
    *,
    needs_source_detail: bool = False,
    policy: RecallEvidencePolicy | None = None,
) -> RecallEvidenceBundle:
    """Resolve exact source anchors and tiny neighborhoods only when needed."""

    selected_policy = policy or RecallEvidencePolicy()
    if not hits:
        return RecallEvidenceBundle(
            expanded=False,
            reason="no_hits",
            hard_char_limit=selected_policy.max_block_chars,
        )
    summary_candidate = any(
        hit.document.source_layer != "conversation" for hit in hits
    )
    raw_candidate = any(
        hit.document.source_layer == "conversation" for hit in hits
    )
    raw_only_fallback = raw_candidate and not summary_candidate
    should_expand = needs_source_detail or raw_only_fallback
    if not should_expand:
        return RecallEvidenceBundle(
            expanded=False,
            reason="summary_sufficient",
            hard_char_limit=selected_policy.max_block_chars,
        )
    if needs_source_detail:
        reason = "explicit_detail"
    else:
        reason = "raw_only_fallback"
    remaining = selected_policy.max_block_chars
    raw_chars = 0
    truncated = False
    seen_messages: set[str] = set()
    groups: list[RecallEvidenceGroup] = []

    expandable_hits = tuple(
        (rank, hit)
        for rank, hit in enumerate(hits, start=1)
        if hit.document.source_message_ids
    )[: selected_policy.max_hits]
    for rank, hit in expandable_hits:
        source_ids = hit.document.source_message_ids
        if hit.matched_terms:
            raw_messages = source.read_message_ids(source_ids)
            source_order = {
                message_id: index for index, message_id in enumerate(source_ids)
            }
            content_by_id = {
                message.message_id: message.content.casefold()
                for message in raw_messages
            }
            terms = tuple(term.casefold() for term in hit.matched_terms if term)
            ranked_ids = sorted(
                source_ids,
                key=lambda message_id: (
                    -sum(
                        term in content_by_id.get(message_id, "")
                        for term in terms
                    ),
                    source_order[message_id],
                ),
            )
        else:
            ranked_ids = list(source_ids)
        anchor_ids = tuple(ranked_ids[: selected_policy.max_anchors_per_hit])
        anchor_set = set(anchor_ids)
        anchor_spans = {
            anchor.message_id: anchor
            for anchor in hit.document.source_anchors
            if anchor.message_id in anchor_set
        }
        omitted = max(0, len(source_ids) - len(anchor_ids))
        missing = 0
        gathered: dict[str, ConversationMessage] = {}
        for anchor_id in anchor_ids:
            neighborhood = source.read_message_neighborhood(
                anchor_id,
                before=selected_policy.before,
                after=selected_policy.after,
            )
            if not neighborhood or not any(
                message.message_id == anchor_id for message in neighborhood
            ):
                missing += 1
                continue
            for message in neighborhood:
                gathered[message.message_id] = message

        messages: list[RecallEvidenceMessage] = []
        for message in sorted(
            gathered.values(), key=lambda item: (item.timestamp, item.row_id)
        ):
            if message.message_id in seen_messages:
                continue
            evidence = _to_evidence_message(
                message,
                anchor_ids=anchor_set,
                anchor_spans=anchor_spans,
                max_chars=selected_policy.max_message_chars,
                remaining_chars=remaining,
            )
            if evidence is None:
                truncated = True
                break
            messages.append(evidence)
            seen_messages.add(message.message_id)
            used = len(evidence.content)
            raw_chars += used
            remaining -= used
            truncated = truncated or evidence.content_truncated
        # Overlapping event/raw hits often resolve to the same source messages.
        # Keep the first attributed group and suppress a second empty shell;
        # missing anchors remain visible because they are not a duplicate.
        if messages or missing:
            groups.append(
                RecallEvidenceGroup(
                    rank=rank,
                    direct_match=hit.direct_match,
                    requested_anchor_count=len(anchor_ids),
                    missing_anchor_count=missing,
                    omitted_anchor_count=omitted,
                    messages=tuple(messages),
                )
            )
        if remaining <= 0:
            truncated = True
            break

    if not groups:
        return RecallEvidenceBundle(
            expanded=False,
            reason="source_unavailable",
            hard_char_limit=selected_policy.max_block_chars,
        )
    return RecallEvidenceBundle(
        expanded=True,
        reason=reason,
        groups=tuple(groups),
        raw_content_chars=raw_chars,
        truncated=truncated,
        hard_char_limit=selected_policy.max_block_chars,
    )


def render_recall_evidence(bundle: RecallEvidenceBundle) -> str:
    """Render source facts only; response strategy remains the final model's job."""

    if not bundle.expanded:
        return ""
    lines = ["[召回来源原文]"]
    speaker = {"user": "人类伙伴", "assistant": "Agent"}
    for group in bundle.groups:
        lines.append(
            f"候选{group.rank}：直接摘要命中={'是' if group.direct_match else '否'}；"
            f"请求锚点={group.requested_anchor_count}；"
            f"缺失锚点={group.missing_anchor_count}；"
            f"省略锚点={group.omitted_anchor_count}"
        )
        for message in group.messages:
            label = speaker.get(message.role, message.role)
            provenance = message.source_kind
            if message.event_type:
                provenance += f"/{message.event_type}"
            if message.span_selected:
                anchor = "锚点摘录"
            elif message.span_fallback:
                anchor = "锚点全文/摘录校验失败"
            else:
                anchor = "锚点" if message.is_anchor else "邻近"
            suffix = "（正文已截断）" if message.content_truncated else ""
            lines.append(
                f"- {message.timestamp}｜{label}｜{provenance}｜{anchor}："
                f"{message.content}{suffix}"
            )
    if bundle.truncated:
        lines.append("[来源展开达到本次字符上限]")
    rendered = "\n".join(lines)
    if len(rendered) <= bundle.hard_char_limit:
        return rendered
    if bundle.hard_char_limit == 0:
        return ""
    return rendered[: bundle.hard_char_limit - 1] + "…"


_BEIJING_TIMEZONE = timezone(timedelta(hours=8))
_SOURCE_KIND_CONTEXT_LABELS = {
    "wander": "你的漫想",
    "sentinel": "你的守望消息",
    "reminder": "提醒消息",
}


def _context_time_label(value: str) -> str:
    """Turn a source timestamp into a stable, human-facing Beijing label."""

    text = str(value or "").strip()
    if not text:
        return "时间未标注"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_BEIJING_TIMEZONE)
        return f"{parsed.year}年{parsed.month}月{parsed.day}日 {parsed:%H:%M}"
    except ValueError:
        compact = text[:16].replace("T", " ")
        return compact or "时间未标注"


def render_recall_evidence_for_context(
    bundle: RecallEvidenceBundle,
    *,
    perspective: str = "agent_second_person",
) -> str:
    """Render only source material useful to Agent, without retrieval diagnostics.

    The structured bundle and ``render_recall_evidence`` retain ranks, anchor
    counts, source kinds and truncation diagnostics for the local inspector.
    This projection deliberately excludes those implementation details from
    the model-facing memory neuron.
    """

    if perspective not in {"canonical", "agent_second_person"}:
        raise ValueError("recall evidence perspective is invalid")
    if not bundle.expanded:
        return ""
    lines = ["[相关原话片段]"]
    speaker = {
        "user": "人类伙伴",
        "assistant": "你" if perspective == "agent_second_person" else "Agent",
    }
    for group in bundle.groups:
        for message in group.messages:
            if message.span_selected:
                relation = "相关原话摘录"
            elif message.is_anchor:
                relation = "相关原话"
            else:
                relation = "同段上下文"
            source_label = _SOURCE_KIND_CONTEXT_LABELS.get(message.source_kind, "")
            if source_label:
                relation += f"，来自{source_label}"
            suffix = "（片段已截取）" if message.content_truncated else ""
            lines.append(
                f"- {_context_time_label(message.timestamp)}，"
                f"{speaker.get(message.role, message.role)}（{relation}）："
                f"{message.content}{suffix}"
            )
    if len(lines) == 1:
        return ""
    rendered = "\n".join(lines)
    if len(rendered) <= bundle.hard_char_limit:
        return rendered
    if bundle.hard_char_limit == 0:
        return ""
    return rendered[: bundle.hard_char_limit - 1] + "…"


__all__ = [
    "RecallEvidenceBundle",
    "RecallEvidenceGroup",
    "RecallEvidenceMessage",
    "RecallEvidencePolicy",
    "expand_recall_evidence",
    "render_recall_evidence",
    "render_recall_evidence_for_context",
]
