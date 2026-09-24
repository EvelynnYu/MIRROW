"""Strictly bounded, attributed context projection for shadow recall."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Literal, Sequence

from behavior_scheduler.bookshelf_search import sanitize_archive_text

from .recall import RecallHit, RecallResult
from .recall_evidence import (
    RecallEvidenceBundle,
    render_recall_evidence_for_context,
)


RecallPerspective = Literal["canonical", "agent_second_person"]


@dataclass(frozen=True)
class RecallContextPolicy:
    max_hits: int = 8
    target_hits: int = 5
    min_hits_before_cliff: int = 3
    dense_expansion_min_top_score: float = 0.50
    max_cognition_hits: int = 3
    max_hit_chars: int = 280
    max_summary_chars: int = 1_100
    max_evidence_chars: int = 1_100
    max_total_chars: int = 2_200

    def __post_init__(self) -> None:
        if not 1 <= self.max_hits <= 10:
            raise ValueError("recall context max_hits must be within 1..10")
        if not 1 <= self.min_hits_before_cliff <= self.target_hits <= 10:
            raise ValueError(
                "recall context hit targets must satisfy 1 <= min <= target <= 10"
            )
        if not 0 <= self.dense_expansion_min_top_score <= 1:
            raise ValueError(
                "dense_expansion_min_top_score must be within 0..1"
            )
        if not 1 <= self.max_cognition_hits <= 5:
            raise ValueError("max_cognition_hits must be within 1..5")
        if not 120 <= self.max_hit_chars <= 1_200:
            raise ValueError("recall context max_hit_chars must be within 120..1200")
        if not 400 <= self.max_summary_chars <= 6_000:
            raise ValueError("recall context max_summary_chars must be within 400..6000")
        if not 0 <= self.max_evidence_chars <= 8_000:
            raise ValueError("recall context max_evidence_chars must be within 0..8000")
        if not 600 <= self.max_total_chars <= 12_000:
            raise ValueError("recall context max_total_chars must be within 600..12000")
        if self.max_summary_chars > self.max_total_chars:
            raise ValueError("summary budget cannot exceed total budget")


@dataclass(frozen=True)
class RecallContextProjection:
    text: str
    rendered_hit_count: int
    rendered_layers: tuple[str, ...]
    rendered_documents: tuple[tuple[str, str], ...]
    evidence_included: bool
    truncated: bool
    hard_char_limit: int

    def safe_observation(self) -> dict[str, object]:
        return {
            "rendered_hit_count": self.rendered_hit_count,
            "rendered_layers": list(self.rendered_layers),
            "evidence_included": self.evidence_included,
            "rendered_chars": len(self.text),
            "truncated": self.truncated,
            "hard_char_limit": self.hard_char_limit,
        }


_LAYER_LABELS = {
    "event": "事件记录",
    "period": "时期事实",
    "conversation": "原始对话命中",
    "cognition": "认知记录",
}
_DOMAIN_LABELS = {
    "self": "你自己",
    "other": "他人",
    "world": "外部事实",
}
_KIND_LABELS = {
    "fact": "事实记录",
    "self_reflection": "自我认识",
    "interpretation": "主观理解",
    "theory": "理论",
    "agreement": "约定",
    "preference": "偏好",
}


def _entity_label(entity_id: str, perspective: RecallPerspective) -> str:
    labels = {
        "agent": "你" if perspective == "agent_second_person" else "Agent",
        "human": "人类伙伴",
        "peer": "Peer",
        "shared": "Agent 与人类伙伴",
        "unknown": "未标注主体",
    }
    return labels.get(entity_id, entity_id)


def _reliability(confidence: float) -> str:
    if confidence >= 0.85:
        return "来源可靠"
    if confidence >= 0.65:
        return "可靠度中等"
    return "可靠度较低"


def _human_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text or "时间未标注"
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _date_label(hit: RecallHit) -> str:
    start = hit.document.date_from or hit.document.active_date_from
    end = hit.document.date_to or hit.document.active_date_to
    if not start:
        return "时间未标注"
    return _human_date(start) if not end or start == end else f"{_human_date(start)}至{_human_date(end)}"


_HUMAN_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9_])Human(?![A-Za-z0-9_])", re.I)
_K_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9_])Agent(?![A-Za-z0-9_])")


def _normalize_derived_text(value: str, perspective: RecallPerspective) -> str:
    """Normalize model-derived aliases without altering source quotations."""

    text = _HUMAN_ALIAS_RE.sub("人类伙伴", str(value or ""))
    if perspective == "agent_second_person":
        text = _K_ALIAS_RE.sub("你", text)
    return text


def _hit_text(hit: RecallHit) -> str:
    document = hit.document
    if document.source_layer == "conversation":
        return "原始对话窗已命中；正文仅在下方来源展开中出现。"
    if document.display_text:
        value = document.display_text
    elif document.source_layer == "event" and len(document.summaries) > 3:
        terms = tuple(term.casefold() for term in hit.matched_terms if term)
        best_index = max(
            range(len(document.summaries)),
            key=lambda index: (
                sum(
                    term in document.summaries[index].casefold()
                    for term in terms
                ),
                -index,
            ),
        )
        representative_indexes = tuple(
            dict.fromkeys((0, best_index, len(document.summaries) - 1))
        )
        value = "；".join(
            document.summaries[index] for index in representative_indexes
        )
    else:
        value = "；".join(document.summaries)
    return sanitize_archive_text(value, limit=8_000)


def select_summary_hits(
    hits: Sequence[RecallHit],
    *,
    max_hits: int,
    target_hits: int = 5,
    min_hits_before_cliff: int = 3,
    dense_expansion_min_top_score: float = 0.50,
    max_cognition_hits: int = 3,
) -> tuple[tuple[int, RecallHit], ...]:
    """Adaptively keep coherent summary groups and leave raw chat to evidence.

    Five groups are the normal target.  A clear score cliff may stop the list
    after three, while a dense cluster of similarly relevant memories may grow
    to the caller's (normally eight) hard cap.  The final character budget is
    still enforced by :func:`build_recall_context`.
    """

    candidates = tuple(
        (retrieval_rank, hit)
        for retrieval_rank, hit in enumerate(hits, start=1)
        if hit.document.source_layer != "conversation"
    )
    if not candidates:
        return ()

    def adaptive(
        lane: Sequence[tuple[int, RecallHit]],
        *,
        lane_max: int,
        lane_target: int,
        lane_min: int,
    ) -> tuple[tuple[int, RecallHit], ...]:
        if not lane or lane_max <= 0:
            return ()
        selected: list[tuple[int, RecallHit]] = []
        top_score = max(0.0, lane[0][1].score)
        previous_score = top_score
        normal_target = min(lane_target, lane_max)
        minimum_before_cliff = min(lane_min, normal_target)
        for retrieval_rank, hit in lane:
            if len(selected) >= lane_max:
                break
            score = max(0.0, hit.score)
            drop = max(0.0, previous_score - score)
            if len(selected) >= minimum_before_cliff:
                if score < max(0.12, top_score * 0.50) or drop >= 0.16:
                    break
            if len(selected) >= normal_target:
                if (
                    top_score < dense_expansion_min_top_score
                    or score < max(0.18, top_score * 0.82)
                    or drop >= 0.08
                ):
                    break
            selected.append((retrieval_rank, hit))
            previous_score = score
        return tuple(selected)

    episodic = tuple(
        item for item in candidates if item[1].document.source_layer != "cognition"
    )
    cognition = tuple(
        item for item in candidates if item[1].document.source_layer == "cognition"
    )
    if not episodic or not cognition:
        return adaptive(
            candidates,
            lane_max=max_hits,
            lane_target=target_hits,
            lane_min=min_hits_before_cliff,
        )

    episodic_selected = adaptive(
        episodic,
        lane_max=max_hits,
        lane_target=target_hits,
        lane_min=min_hits_before_cliff,
    )
    cognition_selected = adaptive(
        cognition,
        lane_max=min(max_hits, max_cognition_hits),
        lane_target=min(2, target_hits),
        lane_min=1,
    )
    combined = sorted((*episodic_selected, *cognition_selected), key=lambda item: item[0])
    if len(combined) <= max_hits:
        return tuple(combined)

    # Preserve one item from each independent lane, then fill by retrieval
    # order.  This prevents score-scale drift from erasing a whole memory type.
    required = {episodic_selected[0][0], cognition_selected[0][0]}
    chosen = [item for item in combined if item[0] in required]
    chosen_ranks = {item[0] for item in chosen}
    for item in combined:
        if len(chosen) >= max_hits:
            break
        if item[0] not in chosen_ranks:
            chosen.append(item)
            chosen_ranks.add(item[0])
    return tuple(sorted(chosen, key=lambda item: item[0]))


def _truncate_natural_text(value: str, limit: int) -> tuple[str, bool]:
    text = str(value or "").strip()
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    search_limit = max(1, limit - 1)
    boundary = max(
        (text.rfind(mark, 0, search_limit) for mark in ("\n", "。", "！", "？", "；", ". ", "! ", "? ")),
        default=-1,
    )
    if boundary >= max(16, int(limit * 0.35)):
        suffix = 1 if text[boundary:boundary + 1] in "。！？；\n" else 0
        return text[:boundary + suffix].rstrip() + "…", True
    return text[:search_limit].rstrip(" `#*_，,、:：；;") + "…", True


def _render_hit(
    hit: RecallHit,
    rank: int,
    *,
    perspective: RecallPerspective,
    max_chars: int,
) -> tuple[str, bool]:
    document = hit.document
    body = _normalize_derived_text(_hit_text(hit), perspective)
    if perspective == "agent_second_person":
        when = _date_label(hit)
        if document.source_layer == "period":
            prefix = f"- {when}那段时间，你大致记得：\n"
        elif document.source_layer == "cognition":
            prefix = f"- 你在{when}前后形成的一项认识：\n"
        elif document.display_text:
            prefix = f"- {when}，你记得：\n"
        else:
            prefix = f"- {when}，你知道当时发生过：\n"
        if document.confidence < 0.65:
            prefix = prefix.rstrip("\n") + "（这段来源的把握较低）\n"
        allowed_body = max(0, max_chars - len(prefix))
        body, truncated = _truncate_natural_text(body, allowed_body)
        return prefix + body, truncated

    source_label = _LAYER_LABELS.get(document.source_layer, document.source_layer)
    notes = [_reliability(document.confidence)]
    if document.subject_ids:
        notes.append(
            "涉及"
            + "、".join(
                _entity_label(entity_id, perspective)
                for entity_id in document.subject_ids
            )
        )
    if document.source_layer == "cognition":
        if document.knower_ids:
            notes.append(
                "由"
                + "、".join(
                    _entity_label(entity_id, perspective)
                    for entity_id in document.knower_ids
                )
                + "形成"
            )
        if document.facets:
            notes.append(f"关于{_DOMAIN_LABELS.get(document.facets[0], document.facets[0])}")
        if len(document.facets) > 1:
            notes.append(f"属于{_KIND_LABELS.get(document.facets[1], document.facets[1])}")
    prefix = f"- {_date_label(hit)}的{source_label}（" + "；".join(notes) + "）：\n"
    allowed_body = max(0, max_chars - len(prefix))
    body, truncated = _truncate_natural_text(body, allowed_body)
    return prefix + body, truncated


def build_recall_context(
    result: RecallResult,
    evidence: RecallEvidenceBundle,
    *,
    policy: RecallContextPolicy | None = None,
    perspective: RecallPerspective = "agent_second_person",
) -> RecallContextProjection:
    """Render facts, provenance and reliability without response instructions."""

    if perspective not in {"canonical", "agent_second_person"}:
        raise ValueError("recall context perspective is invalid")
    selected_policy = policy or RecallContextPolicy()
    if not result.hits:
        return RecallContextProjection(
            text="",
            rendered_hit_count=0,
            rendered_layers=(),
            rendered_documents=(),
            evidence_included=False,
            truncated=False,
            hard_char_limit=selected_policy.max_total_chars,
        )

    lines = [
        "[你想起的相关经历｜本轮只呈现与眼前内容最相关的部分]"
        if perspective == "agent_second_person"
        else "[相关历史材料｜本轮自动筛选，不代表全部历史]"
    ]
    rendered_layers: list[str] = []
    rendered_documents: list[tuple[str, str]] = []
    summary_truncated = False
    for retrieval_rank, hit in select_summary_hits(
        result.hits,
        max_hits=selected_policy.max_hits,
        target_hits=selected_policy.target_hits,
        min_hits_before_cliff=selected_policy.min_hits_before_cliff,
        dense_expansion_min_top_score=(
            selected_policy.dense_expansion_min_top_score
        ),
        max_cognition_hits=selected_policy.max_cognition_hits,
    ):
        block, hit_truncated = _render_hit(
            hit,
            retrieval_rank,
            perspective=perspective,
            max_chars=selected_policy.max_hit_chars,
        )
        projected = "\n\n".join((*lines, block))
        if len(projected) > selected_policy.max_summary_chars:
            summary_truncated = True
            break
        lines.append(block)
        summary_truncated = summary_truncated or hit_truncated
        rendered_layers.append(hit.document.source_layer)
        rendered_documents.append(
            (hit.document.source_layer, hit.document.document_id)
        )
    summary = "\n\n".join(lines) if rendered_layers else ""

    evidence_text = render_recall_evidence_for_context(
        evidence,
        perspective=perspective,
    )
    evidence_included = bool(evidence_text and selected_policy.max_evidence_chars)
    evidence_truncated = False
    if evidence_included:
        separator_chars = 2 if summary else 0
        remaining = max(
            0,
            selected_policy.max_total_chars - len(summary) - separator_chars,
        )
        evidence_budget = min(selected_policy.max_evidence_chars, remaining)
        if len(evidence_text) > evidence_budget:
            evidence_truncated = True
            evidence_text, _ = _truncate_natural_text(evidence_text, evidence_budget)
        if evidence_text:
            summary += ("\n\n" if summary else "") + evidence_text
        else:
            evidence_included = False

    total_truncated = summary_truncated or evidence_truncated or evidence.truncated
    if len(summary) > selected_policy.max_total_chars:
        total_truncated = True
        summary, _ = _truncate_natural_text(summary, selected_policy.max_total_chars)
    return RecallContextProjection(
        text=summary,
        rendered_hit_count=len(rendered_layers),
        rendered_layers=tuple(rendered_layers),
        rendered_documents=tuple(rendered_documents),
        evidence_included=evidence_included,
        truncated=total_truncated,
        hard_char_limit=selected_policy.max_total_chars,
    )


__all__ = [
    "RecallContextPolicy",
    "RecallContextProjection",
    "RecallPerspective",
    "build_recall_context",
    "select_summary_hits",
]
