"""Read-only candidate fusion across heterogeneous Memory V2 read models.

Event/thread and cognition summaries are the normal recall surface.  Raw
conversation windows remain a last-resort source fallback: they never compete
for the bounded summary shortlist, but can still be returned when no summary
view matched at all.  No source is promoted to a new fact authority.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import re
from typing import Protocol, Sequence

from behavior_scheduler.bookshelf_search import sanitize_archive_text

from .conversation_source import ConversationMessage, ConversationSource
from .recall import (
    EventRecallIndex,
    RecallDocument,
    RecallDecision,
    RecallQuery,
    RecallResult,
    RecallSourceAnchor,
    SemanticScorer,
)


class RecallCandidateSelector(Protocol):
    def synchronize(self, documents: Sequence[RecallDocument]) -> bool: ...

    def invalidate(self) -> None: ...

    def select(
        self,
        documents: Sequence[RecallDocument],
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RecallDocument, ...]: ...


class _StaticRecallIndex(EventRecallIndex):
    """Use the shared ranker over an already-built source partition."""

    def __init__(
        self,
        db_path: str,
        documents: Sequence[RecallDocument],
        *,
        semantic_scorer: SemanticScorer | None = None,
    ):
        super().__init__(db_path, semantic_scorer=semantic_scorer)
        self.documents = tuple(documents)

    def _load_documents(self) -> list[RecallDocument]:
        return list(self.documents)


_EPISODIC_QUERY_RE = re.compile(
    r"那次|那回|哪次|当时|后来|发生|第一次|上次|有一次|哪一天|什么时候"
)
_COGNITION_QUERY_RE = re.compile(
    r"平时|通常|一般|习惯|偏好|喜不喜欢|爱不爱|怎么看|认为|了解我|什么样的人"
)
_PERIOD_QUERY_RE = re.compile(
    r"那天|那周|那个月|一整天|整天|那阵子|一段时间|这段时间|最近几(?:天|周|月)"
)


def _explicit_other_subject_ids(query: RecallQuery) -> set[str]:
    """Return other-book subjects explicitly named by the retrieval query.

    Human is the fixed interlocutor and is always in scope.  Other people are
    admitted only by a registered name or alias in the current query envelope;
    recalled event text must never widen this set.
    """

    allowed = {"human"}
    texts = tuple(
        str(value or "").casefold()
        for value in (query.text, *query.variants)
        if str(value or "").strip()
    )
    try:
        from cognition.other_book import entities

        known = entities()
    except Exception:
        return allowed
    for subject_id, raw in known.items():
        if subject_id == "human" or not isinstance(raw, dict):
            continue
        names = (
            raw.get("name"),
            raw.get("preferred_name"),
            *(raw.get("aliases") if isinstance(raw.get("aliases"), list) else ()),
        )
        if any(
            (name := str(candidate or "").strip().casefold())
            and len(name) >= 2
            and any(name in text for text in texts)
            for candidate in names
        ):
            allowed.add(str(subject_id))
    return allowed


def _cognition_subject_is_in_scope(
    document: RecallDocument,
    allowed_other_subject_ids: set[str],
) -> bool:
    if document.source_layer != "cognition":
        return True
    domain = document.facets[0] if document.facets else ""
    if domain != "other":
        return True
    return bool(set(document.subject_ids).intersection(allowed_other_subject_ids))


def _cognition_hit_is_relevant(
    hit,
    query: RecallQuery,
    *,
    top_episodic_score: float,
) -> bool:
    """Keep the cognition lane independent without forcing a weak entry."""

    if hit.document.source_layer != "cognition":
        return True
    if _COGNITION_QUERY_RE.search(query.text) or hit.direct_match:
        return True
    if top_episodic_score > 0:
        return hit.score >= max(0.18, top_episodic_score * 0.72)
    # A cognition-only answer is valid when it still clears a modest absolute
    # relevance floor.  This rejects the ~0.05 generic tail observed on
    # unrelated episodic questions without requiring an event competitor.
    return hit.score >= 0.12


def _query_source_weight(layer: str, base: float, query: RecallQuery) -> float:
    """Use query shape only as a soft granularity preference, never a gate."""

    text = query.text
    episodic = bool(_EPISODIC_QUERY_RE.search(text))
    cognition = bool(_COGNITION_QUERY_RE.search(text))
    period = bool(_PERIOD_QUERY_RE.search(text))
    weight = base
    if episodic and not cognition and layer == "cognition":
        weight *= 0.55
    if cognition and not episodic:
        if layer == "cognition":
            weight = min(1.0, weight * 1.1)
        elif layer in {"event", "period"}:
            weight *= 0.85
        elif layer == "conversation":
            weight *= 0.65
    if period:
        if layer == "period":
            weight = 1.0
        elif layer == "event":
            weight *= 0.72
        elif layer == "conversation":
            weight *= 0.6
        elif layer == "cognition":
            weight *= 0.75
    return weight


class ConversationWindowRecallIndex(EventRecallIndex):
    """Project exact raw-message windows into the shared recall ranker."""

    def __init__(
        self,
        source: ConversationSource,
        *,
        window_size: int = 6,
        stride: int = 4,
        semantic_scorer: SemanticScorer | None = None,
    ):
        if not 2 <= window_size <= 20:
            raise ValueError("conversation recall window_size must be within 2..20")
        if not 1 <= stride <= window_size:
            raise ValueError("conversation recall stride must be within 1..window_size")
        super().__init__(source.db_path, semantic_scorer=semantic_scorer)
        self.source = source
        self.window_size = window_size
        self.stride = stride

    def _load_documents(self) -> list[RecallDocument]:
        grouped: dict[tuple[str, str], list[ConversationMessage]] = defaultdict(list)
        for message in self.source.read_all():
            grouped[(message.active_date, message.session_id)].append(message)

        documents: list[RecallDocument] = []
        labels = {"user": "人类伙伴", "assistant": "Agent"}
        for (active_date, session_id), messages in grouped.items():
            for start in range(0, len(messages), self.stride):
                window = messages[start : start + self.window_size]
                if not window:
                    continue
                first, last = window[0], window[-1]
                calendar_dates = sorted(
                    {message.calendar_date for message in window if message.calendar_date}
                )
                subjects = tuple(
                    dict.fromkeys(
                        "human" if message.role == "user" else "agent"
                        for message in window
                    )
                )
                summaries = tuple(
                    sanitize_archive_text(
                        f"[{labels.get(message.role, message.role)}] {message.content}",
                        limit=900,
                    )
                    for message in window
                )
                documents.append(
                    RecallDocument(
                        document_id=(
                            f"conversation:{session_id}:{first.message_id}:{last.message_id}"
                        ),
                        event_ids=(),
                        source_message_ids=tuple(
                            message.message_id for message in window
                        ),
                        summaries=summaries,
                        subject_ids=subjects,
                        participant_ids=subjects,
                        event_types=tuple(
                            dict.fromkeys(
                                message.event_type
                                for message in window
                                if message.event_type
                            )
                        ),
                        facets=("conversation",),
                        source_kinds=tuple(
                            dict.fromkeys(message.source_kind for message in window)
                        ),
                        active_date_from=active_date,
                        active_date_to=active_date,
                        date_from=calendar_dates[0] if calendar_dates else active_date,
                        date_to=calendar_dates[-1] if calendar_dates else active_date,
                        importance=0.35,
                        confidence=1.0,
                        source_anchors=tuple(
                            RecallSourceAnchor(message_id=message.message_id)
                            for message in window
                        ),
                        source_layer="conversation",
                    )
                )
                if start + self.window_size >= len(messages):
                    break
        return documents


class MultiSourceRecallIndex(EventRecallIndex):
    """Rank heterogeneous read models together, then remove overlapping raw windows."""

    def __init__(
        self,
        indexes: Sequence[EventRecallIndex],
        *,
        semantic_scorer: SemanticScorer | None = None,
        source_weights: dict[str, float] | None = None,
        candidate_selector: RecallCandidateSelector | None = None,
        candidate_limit_per_layer: int = 120,
        max_summary_hits: int = 10,
        max_conversation_hits: int = 2,
        max_cognition_hits: int = 3,
    ):
        if not indexes:
            raise ValueError("multi-source recall needs at least one index")
        super().__init__(indexes[0].db_path, semantic_scorer=semantic_scorer)
        self.indexes = tuple(indexes)
        if candidate_limit_per_layer < 30:
            raise ValueError("candidate_limit_per_layer must be at least 30")
        if not 1 <= max_summary_hits <= 10:
            raise ValueError("max_summary_hits must be within 1..10")
        if not 1 <= max_conversation_hits <= 10:
            raise ValueError("max_conversation_hits must be within 1..10")
        if not 1 <= max_cognition_hits <= 5:
            raise ValueError("max_cognition_hits must be within 1..5")
        self.candidate_selector = candidate_selector
        self.candidate_limit_per_layer = int(candidate_limit_per_layer)
        self.max_summary_hits = int(max_summary_hits)
        self.max_conversation_hits = int(max_conversation_hits)
        self.max_cognition_hits = int(max_cognition_hits)
        self.source_weights = {
            "event": 1.0,
            "conversation": 0.85,
            "period": 0.85,
            "cognition": 0.9,
            **(source_weights or {}),
        }
        if any(not 0 < float(value) <= 1 for value in self.source_weights.values()):
            raise ValueError("multi-source weights must be within (0, 1]")
        self._documents_cache: tuple[RecallDocument, ...] | None = None
        self._documents_by_layer_cache: dict[str, tuple[RecallDocument, ...]] | None = None

    def refresh(self) -> None:
        """Drop the materialized read model after an authoritative source advances."""

        self._documents_cache = None
        self._documents_by_layer_cache = None
        if self.candidate_selector is not None:
            self.candidate_selector.invalidate()

    def _load_documents(self) -> list[RecallDocument]:
        if self._documents_cache is not None:
            return list(self._documents_cache)
        documents: list[RecallDocument] = []
        seen: set[tuple[str, str]] = set()
        for index in self.indexes:
            for document in index.load_documents():
                identity = (document.source_layer, document.document_id)
                if identity in seen:
                    continue
                seen.add(identity)
                documents.append(document)
        self._documents_cache = tuple(documents)
        by_layer: dict[str, list[RecallDocument]] = defaultdict(list)
        for document in self._documents_cache:
            by_layer[document.source_layer].append(document)
        self._documents_by_layer_cache = {
            layer: tuple(items) for layer, items in by_layer.items()
        }
        if self.candidate_selector is not None:
            self.candidate_selector.synchronize(self._documents_cache)
        return list(self._documents_cache)

    def search(self, query: RecallQuery, *, limit: int = 5) -> RecallResult:
        if limit <= 0:
            raise ValueError("recall result limit must be positive")
        expanded_limit = max(30, limit * 6)
        documents = self._load_documents()
        by_layer = self._documents_by_layer_cache or {}
        candidate_count = sum(len(documents) for documents in by_layer.values())
        evaluated_count = 0
        evaluated_counts_by_layer: dict[str, int] = {}
        weighted_hits = []
        semantic_available = False
        semantic_statuses: list[str] = []
        for layer, documents in sorted(by_layer.items()):
            # Ordinary raw chat rows have no semantic event classification.
            # A planner-proposed event type is therefore an event-index hint,
            # never negative evidence against the conversation authority.
            layer_query = replace(
                query,
                event_types=(
                    ()
                    if layer in {"conversation", "cognition"}
                    else query.event_types
                ),
                source_kinds=(
                    () if layer == "cognition" else query.source_kinds
                ),
            )
            ranked_documents: Sequence[RecallDocument] = documents
            if self.candidate_selector is not None:
                ranked_documents = self.candidate_selector.select(
                    documents,
                    layer_query,
                    limit=max(self.candidate_limit_per_layer, expanded_limit * 4),
                )
            evaluated_count += len(ranked_documents)
            evaluated_counts_by_layer[layer] = len(ranked_documents)
            layer_index = _StaticRecallIndex(
                self.db_path,
                ranked_documents,
                # Raw chat is an authoritative lexical fallback, not an
                # embedding corpus.  Keeping it out of semantic scoring means
                # active-day appends do not churn the stable summary index.
                semantic_scorer=(
                    None if layer == "conversation" else self.semantic_scorer
                ),
            )
            try:
                layer_result = layer_index.search(layer_query, limit=expanded_limit)
            except Exception as exc:
                semantic_statuses.append(f"{layer}:error:{type(exc).__name__}")
                continue
            semantic_available = semantic_available or layer_result.semantic_available
            semantic_statuses.append(f"{layer}:{layer_result.semantic_status}")
            weight = _query_source_weight(
                layer,
                float(self.source_weights.get(layer, 0.8)),
                query,
            )
            weighted_hits.extend(
                replace(hit, score=hit.score * weight)
                for hit in layer_result.hits
            )
        weighted_hits.sort(
            key=lambda hit: (
                -hit.score,
                -int(hit.direct_match),
                hit.document.date_from,
                hit.document.document_id,
            )
        )

        selected = []
        decisions: list[RecallDecision] = []
        # Raw windows can outscore paraphrased summaries because they contain
        # exact surface words.  Inspect a broad, still-bounded pool so those
        # windows cannot push the source-backed event view outside the set we
        # consider for summary-first selection.
        decision_limit = min(len(weighted_hits), max(40, limit * 8))
        considered_hits = weighted_hits[:decision_limit]
        episodic_source_sets: list[tuple[str, set[str]]] = []
        selected_summary_count = 0
        selected_conversation_count = 0

        # When an event/thread and a raw window point to substantially the same
        # source, the narrative memory is the context candidate and the raw
        # window remains only a possible evidence source.  Score order must not
        # let the raw adapter suppress its own event summary.
        summary_source_sets = [
            set(hit.document.source_message_ids)
            for hit in considered_hits
            if hit.document.source_layer in {"event", "period"}
            and hit.document.source_message_ids
        ]

        summary_hits = tuple(
            hit
            for hit in considered_hits
            if hit.document.source_layer != "conversation"
        )
        conversation_hits = tuple(
            hit
            for hit in considered_hits
            if hit.document.source_layer == "conversation"
        )

        # The first pass is summary-only.  Event/period and cognition are two
        # bounded lanes: neither may erase the other merely because their raw
        # score scales differ.  Other-book cognition also has an explicit
        # entity gate derived only from the query envelope.
        allowed_other_subject_ids = _explicit_other_subject_ids(query)
        top_episodic_score = max(
            (
                hit.score
                for hit in summary_hits
                if hit.document.source_layer in {"event", "period"}
            ),
            default=0.0,
        )
        eligible_summary_hits: list = []
        pre_eliminated: dict[tuple[str, str], str] = {}
        for hit in summary_hits:
            layer = hit.document.source_layer
            source_ids = set(hit.document.source_message_ids)
            identity = (layer, hit.document.document_id)
            if not _cognition_subject_is_in_scope(
                hit.document, allowed_other_subject_ids
            ):
                pre_eliminated[identity] = "cognition_entity_not_in_scope"
                continue
            if not _cognition_hit_is_relevant(
                hit, query, top_episodic_score=top_episodic_score
            ):
                pre_eliminated[identity] = "cognition_lane_not_relevant"
                continue
            if layer in {"event", "period"}:
                duplicate = False
                for previous_layer, previous in episodic_source_sets:
                    intersection = len(source_ids.intersection(previous))
                    if not intersection:
                        continue
                    # Period items deliberately contain their event sources, so
                    # subset overlap is the useful duplicate signal here.
                    overlap = intersection / max(1, min(len(source_ids), len(previous)))
                    if overlap >= 0.5:
                        duplicate = True
                        break
                if duplicate:
                    pre_eliminated[identity] = "overlapping_source_evidence"
                    continue
                if source_ids:
                    episodic_source_sets.append((layer, source_ids))
            eligible_summary_hits.append(hit)

        summary_cap = min(limit, self.max_summary_hits)
        episodic_hits = [
            hit for hit in eligible_summary_hits
            if hit.document.source_layer in {"event", "period"}
        ]
        cognition_hits = [
            hit for hit in eligible_summary_hits
            if hit.document.source_layer == "cognition"
        ]
        chosen: set[tuple[str, str]] = set()
        if summary_cap == 1 and eligible_summary_hits:
            first = eligible_summary_hits[0]
            chosen.add((first.document.source_layer, first.document.document_id))
        elif summary_cap >= 2:
            # Give each genuinely matching lane one seat before global fill.
            for lane in (episodic_hits, cognition_hits):
                if lane:
                    first = lane[0]
                    chosen.add((first.document.source_layer, first.document.document_id))
        cognition_count = sum(
            layer == "cognition" for layer, _document_id in chosen
        )
        for hit in eligible_summary_hits:
            if len(chosen) >= summary_cap:
                break
            identity = (hit.document.source_layer, hit.document.document_id)
            if identity in chosen:
                continue
            if (
                hit.document.source_layer == "cognition"
                and cognition_count >= self.max_cognition_hits
            ):
                continue
            chosen.add(identity)
            if hit.document.source_layer == "cognition":
                cognition_count += 1

        for hit in summary_hits:
            identity = (hit.document.source_layer, hit.document.document_id)
            if identity in chosen:
                selected.append(hit)
                selected_summary_count += 1
                decisions.append(RecallDecision(hit, "selected", "selected_top_k"))
                continue
            reason = pre_eliminated.get(identity)
            if reason is None:
                if (
                    hit.document.source_layer == "cognition"
                    and sum(
                        layer == "cognition" for layer, _document_id in chosen
                    ) >= self.max_cognition_hits
                ):
                    reason = "source_layer_limit"
                else:
                    reason = "output_limit"
            decisions.append(RecallDecision(hit, "eliminated", reason))

        if selected:
            for hit in conversation_hits:
                decisions.append(
                    RecallDecision(
                        hit=hit,
                        decision="eliminated",
                        reason=(
                            "overlapping_source_evidence"
                            if hit.document.source_message_ids
                            and any(
                                len(
                                    set(hit.document.source_message_ids).intersection(
                                        summary_sources
                                    )
                                )
                                / max(
                                    1,
                                    len(
                                        set(hit.document.source_message_ids).union(
                                            summary_sources
                                        )
                                    ),
                                )
                                >= 0.5
                                for summary_sources in summary_source_sets
                            )
                            else "raw_reserved_for_source_fallback"
                        ),
                    )
                )
        else:
            # No summary view matched.  Return a tiny set of disjoint raw
            # windows so evidence expansion can still expose authoritative
            # source text without making raw chat the ordinary memory format.
            raw_source_sets: list[set[str]] = []
            for hit in conversation_hits:
                source_ids = set(hit.document.source_message_ids)
                if any(source_ids.intersection(previous) for previous in raw_source_sets):
                    decisions.append(
                        RecallDecision(
                            hit=hit,
                            decision="eliminated",
                            reason="overlapping_source_evidence",
                        )
                    )
                    continue
                if (
                    len(selected) < limit
                    and selected_conversation_count
                    < min(limit, self.max_conversation_hits)
                ):
                    selected.append(hit)
                    selected_conversation_count += 1
                    if source_ids:
                        raw_source_sets.append(source_ids)
                    decisions.append(
                        RecallDecision(
                            hit=hit,
                            decision="selected",
                            reason="raw_only_source_fallback",
                        )
                    )
                else:
                    decisions.append(
                        RecallDecision(
                            hit=hit,
                            decision="eliminated",
                            reason="source_layer_limit",
                        )
                    )
        return RecallResult(
            hits=tuple(selected),
            candidate_count=candidate_count,
            semantic_available=semantic_available,
            semantic_status=";".join(semantic_statuses) or "unavailable",
            evaluated_count=evaluated_count,
            decisions=tuple(decisions),
            candidate_counts_by_layer=tuple(
                sorted((layer, len(items)) for layer, items in by_layer.items())
            ),
            evaluated_counts_by_layer=tuple(sorted(evaluated_counts_by_layer.items())),
        )


__all__ = [
    "ConversationWindowRecallIndex",
    "MultiSourceRecallIndex",
    "RecallCandidateSelector",
]
