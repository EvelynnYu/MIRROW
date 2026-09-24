"""Read-only event/thread retrieval for the isolated Memory V2 prototype."""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from .day_settlement_lineage import (
    effective_event_thread_status_sql,
    has_event_thread_status_log,
)


SemanticScorer = Callable[[str, Sequence[str]], Sequence[float]]
RecallSortMode = Literal["relevance", "latest", "earliest"]
RecallDateBasis = Literal["calendar_date", "active_date"]

_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.IGNORECASE)
_HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_NORMALIZE_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_STOP_TERMS = frozenset(
    {
        "一个",
        "一下",
        "什么",
        "事情",
        "之前",
        "以前",
        "后来",
        "那次",
        "这个",
        "那个",
        "记得",
        "想起",
        "我们",
        "你们",
        "他们",
        "怎么",
        "为什么",
    }
)


def _normalized(value: Any) -> str:
    return _NORMALIZE_RE.sub("", str(value or "").casefold())


def _character_grams(value: str) -> set[str]:
    normalized = _normalized(value)
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def _query_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        term = _normalized(raw)
        if not term or term in _STOP_TERMS or term in seen:
            return
        seen.add(term)
        terms.append(term)

    for item in _LATIN_RE.findall(value):
        add(item)
    for run in _HAN_RUN_RE.findall(value):
        if 1 < len(run) <= 6:
            add(run)
        if len(run) > 1:
            for index in range(len(run) - 1):
                add(run[index : index + 2])
    return tuple(terms[:32])


def _rarity_terms(value: str) -> tuple[str, ...]:
    """Return explicit short terms only, never generated character grams."""

    terms: list[str] = []
    for raw in (*_LATIN_RE.findall(value), *_HAN_RUN_RE.findall(value)):
        term = _normalized(raw)
        if 2 <= len(term) <= 8 and term not in _STOP_TERMS and term not in terms:
            terms.append(term)
    return tuple(terms[:16])


def _fuzzy_ratio(query: str, text: str) -> float:
    try:
        from rapidfuzz import fuzz

        return max(
            float(fuzz.token_set_ratio(query, text)),
            float(fuzz.partial_ratio(query, text)),
        ) / 100.0
    except Exception:
        from difflib import SequenceMatcher

        window = text[: max(len(query) * 6, 240)]
        return SequenceMatcher(None, query, window).ratio()


def _iso_date(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        date.fromisoformat(clean)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    return clean


@dataclass(frozen=True)
class RecallQuery:
    text: str
    variants: tuple[str, ...] = ()
    entity_terms: tuple[str, ...] = ()
    subject_ids: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    source_kinds: tuple[str, ...] = ()
    date_from: str = ""
    date_to: str = ""
    date_basis: RecallDateBasis = "calendar_date"
    sort_mode: RecallSortMode = "relevance"
    exclude_message_ids: tuple[str, ...] = ()
    exclude_active_dates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("recall query text must not be empty")
        start = _iso_date(self.date_from, "date_from")
        end = _iso_date(self.date_to, "date_to")
        if start and end and start > end:
            raise ValueError("date_from must not be after date_to")
        if self.date_basis not in {"calendar_date", "active_date"}:
            raise ValueError("date_basis must be calendar_date or active_date")
        if self.sort_mode not in {"relevance", "latest", "earliest"}:
            raise ValueError("sort_mode must be relevance, latest, or earliest")
        for value in self.exclude_active_dates:
            _iso_date(value, "exclude_active_dates")


@dataclass(frozen=True)
class RecallSourceAnchor:
    message_id: str
    span_start: int | None = None
    span_end: int | None = None
    span_digest: str = ""


@dataclass(frozen=True)
class RecallDocument:
    document_id: str
    event_ids: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    summaries: tuple[str, ...]
    subject_ids: tuple[str, ...]
    participant_ids: tuple[str, ...]
    event_types: tuple[str, ...]
    facets: tuple[str, ...]
    source_kinds: tuple[str, ...]
    active_date_from: str
    active_date_to: str
    date_from: str
    date_to: str
    importance: float
    confidence: float
    source_anchors: tuple[RecallSourceAnchor, ...] = ()
    source_layer: str = "event"
    display_text: str = ""
    knower_ids: tuple[str, ...] = ()
    search_addenda: tuple[str, ...] = ()

    @property
    def search_text(self) -> str:
        return "\n".join(
            (
                *self.summaries,
                *self.subject_ids,
                *self.participant_ids,
                *self.event_types,
                *self.facets,
                *self.search_addenda,
            )
        )


@dataclass(frozen=True)
class RecallHit:
    document: RecallDocument
    score: float
    lexical_score: float
    fuzzy_score: float
    semantic_score: float | None
    rarity_score: float
    direct_match: bool
    matched_terms: tuple[str, ...]

    def safe_observation(self, rank: int) -> dict[str, Any]:
        """Return content-free diagnostics suitable for benchmark reports."""

        return {
            "rank": rank,
            "source_layer": self.document.source_layer,
            "event_count": len(self.document.event_ids),
            "source_count": len(self.document.source_message_ids),
            "date_from": self.document.date_from,
            "date_to": self.document.date_to,
            "active_date_from": self.document.active_date_from,
            "active_date_to": self.document.active_date_to,
            "score": round(self.score, 4),
            "lexical_score": round(self.lexical_score, 4),
            "fuzzy_score": round(self.fuzzy_score, 4),
            "rarity_score": round(self.rarity_score, 4),
            "direct_match": self.direct_match,
            "semantic_score": (
                round(self.semantic_score, 4)
                if self.semantic_score is not None
                else None
            ),
            "matched_term_count": len(self.matched_terms),
        }


@dataclass(frozen=True)
class RecallDecision:
    """One bounded shortlist decision retained for local diagnostics."""

    hit: RecallHit
    decision: Literal["selected", "eliminated"]
    reason: str


@dataclass(frozen=True)
class RecallResult:
    hits: tuple[RecallHit, ...]
    candidate_count: int
    semantic_available: bool
    semantic_status: str
    evaluated_count: int | None = None
    decisions: tuple[RecallDecision, ...] = ()
    candidate_counts_by_layer: tuple[tuple[str, int], ...] = ()
    evaluated_counts_by_layer: tuple[tuple[str, int], ...] = ()


class CachedEmbeddingSemanticScorer:
    """In-memory cosine scorer; model/cache lifecycle remains outside recall authority."""

    def __init__(self, embedder: Any):
        self.embedder = embedder
        self._vectors: dict[str, Any] = {}

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def __call__(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        import numpy as np

        keys = [self._key(text) for text in texts]
        missing = [
            (key, text)
            for key, text in zip(keys, texts)
            if key not in self._vectors
        ]
        if missing:
            encoded = self.embedder.embed_texts(
                [text for _, text in missing],
                batch_size=32,
            )
            if len(encoded) != len(missing):
                raise ValueError("embedder returned the wrong document vector count")
            for (key, _), vector in zip(missing, encoded):
                value = np.asarray(vector, dtype=np.float32)
                self._vectors[key] = value / max(float(np.linalg.norm(value)), 1e-8)
        matrix = np.asarray([self._vectors[key] for key in keys], dtype=np.float32)
        query_vector = np.asarray(self.embedder.embed_text(query), dtype=np.float32)
        query_vector /= max(float(np.linalg.norm(query_vector)), 1e-8)
        return matrix.dot(query_vector).tolist()


class EventRecallIndex:
    """Build a small read model from immutable events and continuation threads."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        semantic_scorer: SemanticScorer | None = None,
        recollections_by_message_id: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ):
        self.db_path = str(db_path)
        self.semantic_scorer = semantic_scorer
        self.recollections_by_message_id = recollections_by_message_id or {}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _load_documents(self) -> list[RecallDocument]:
        connection = self._connect()
        try:
            thread_status_sql = effective_event_thread_status_sql(
                "thread",
                status_log_available=has_event_thread_status_log(connection),
            )
            rows = connection.execute(
                f"""
                WITH active_thread_memberships AS (
                    SELECT membership.event_id, membership.thread_id
                    FROM thread_events membership
                    JOIN event_threads thread
                      ON thread.id = membership.thread_id
                    WHERE {thread_status_sql} = 'active'
                )
                SELECT e.*, t.id AS thread_id
                FROM events e
                LEFT JOIN active_thread_memberships te ON te.event_id = e.id
                LEFT JOIN event_threads t ON t.id = te.thread_id
                WHERE COALESCE(
                    (
                        SELECT status
                        FROM event_status_log status_log
                        WHERE status_log.event_id = e.id
                        ORDER BY status_log.created_at DESC, status_log.id DESC
                        LIMIT 1
                    ),
                    'active'
                ) = 'active'
                ORDER BY COALESCE(NULLIF(e.occurred_at, ''), e.reported_at), e.id
                """
            ).fetchall()
            if not rows:
                return []
            event_ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in event_ids)
            source_map: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT event_id, message_id, source_kind, source_order, "
                "span_start, span_end, span_digest "
                f"FROM event_sources WHERE event_id IN ({placeholders}) "
                "ORDER BY event_id, source_order",
                event_ids,
            ).fetchall():
                source_map.setdefault(str(row["event_id"]), []).append(row)
            participant_map: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT event_id, participant_id, participant_order "
                f"FROM event_participants WHERE event_id IN ({placeholders}) "
                "ORDER BY event_id, participant_order",
                event_ids,
            ).fetchall():
                participant_map.setdefault(str(row["event_id"]), []).append(
                    str(row["participant_id"])
                )
        finally:
            connection.close()

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            thread_id = str(row["thread_id"] or "")
            key = f"thread:{thread_id}" if thread_id else f"event:{row['id']}"
            grouped.setdefault(key, []).append(row)

        documents: list[RecallDocument] = []
        for document_id, event_rows in grouped.items():
            ids = [str(row["id"]) for row in event_rows]
            attributes = [json.loads(str(row["attributes_json"] or "{}")) for row in event_rows]
            dates = sorted(
                {
                    str(row["calendar_date"] or "")
                    for row in event_rows
                    if row["calendar_date"]
                }
            )
            active_dates = sorted(
                {
                    str(row["active_date"] or "")
                    for row in event_rows
                    if row["active_date"]
                }
            )
            document_sources = [
                source
                for event_id in ids
                for source in source_map.get(event_id, [])
            ]
            anchor_by_message: dict[str, RecallSourceAnchor] = {}
            for source in document_sources:
                message_id = str(source["message_id"])
                anchor_by_message.setdefault(
                    message_id,
                    RecallSourceAnchor(
                        message_id=message_id,
                        span_start=(
                            int(source["span_start"])
                            if source["span_start"] is not None
                            else None
                        ),
                        span_end=(
                            int(source["span_end"])
                            if source["span_end"] is not None
                            else None
                        ),
                        span_digest=str(source["span_digest"] or ""),
                    ),
                )
            source_message_ids = tuple(anchor_by_message)
            recollections = ()
            if self.recollections_by_message_id:
                from .autobiographical import recollections_for_sources

                recollections = recollections_for_sources(
                    source_message_ids,
                    self.recollections_by_message_id,
                    limit=3,
                )
            recollection_texts = tuple(
                str(item.get("memory") or "").strip()
                for item in recollections
                if str(item.get("memory") or "").strip()
            )
            documents.append(
                RecallDocument(
                    document_id=document_id,
                    event_ids=tuple(ids),
                    source_message_ids=source_message_ids,
                    summaries=tuple(str(row["summary"]) for row in event_rows),
                    subject_ids=tuple(
                        dict.fromkeys(str(row["subject_id"]) for row in event_rows)
                    ),
                    participant_ids=tuple(
                        dict.fromkeys(
                            participant
                            for event_id in ids
                            for participant in participant_map.get(event_id, [])
                        )
                    ),
                    event_types=tuple(
                        dict.fromkeys(str(row["event_type"]) for row in event_rows)
                    ),
                    facets=tuple(
                        dict.fromkeys(
                            str(facet)
                            for item in attributes
                            for facet in item.get("memory_facets", [])
                            if str(facet).strip()
                        )
                    ),
                    source_kinds=tuple(
                        dict.fromkeys(
                            str(source["source_kind"])
                            for event_id in ids
                            for source in source_map.get(event_id, [])
                        )
                    ),
                    active_date_from=active_dates[0] if active_dates else "",
                    active_date_to=active_dates[-1] if active_dates else "",
                    date_from=dates[0] if dates else "",
                    date_to=dates[-1] if dates else "",
                    importance=max(float(row["importance"]) for row in event_rows),
                    confidence=min(float(row["confidence"]) for row in event_rows),
                    source_anchors=tuple(anchor_by_message.values()),
                    display_text="\n".join(recollection_texts),
                    search_addenda=recollection_texts,
                )
            )
        return documents

    def load_documents(self) -> tuple[RecallDocument, ...]:
        """Expose the immutable read model for composite shadow indexes."""

        return tuple(self._load_documents())

    @staticmethod
    def _matches_filters(document: RecallDocument, query: RecallQuery) -> bool:
        if query.exclude_message_ids and set(query.exclude_message_ids).intersection(
            document.source_message_ids
        ):
            return False
        if (
            document.source_layer == "conversation"
            and document.active_date_from == document.active_date_to
            and document.active_date_from in query.exclude_active_dates
        ):
            return False
        if query.subject_ids and not set(query.subject_ids).intersection(
            (*document.subject_ids, *document.participant_ids)
        ):
            return False
        if query.event_types and not set(query.event_types).intersection(
            (*document.event_types, *document.facets)
        ):
            return False
        if query.source_kinds and not set(query.source_kinds).intersection(
            document.source_kinds
        ):
            return False
        document_from = (
            document.active_date_from
            if query.date_basis == "active_date"
            else document.date_from
        )
        document_to = (
            document.active_date_to
            if query.date_basis == "active_date"
            else document.date_to
        )
        if query.date_from and document_to and document_to < query.date_from:
            return False
        if query.date_to and document_from and document_from > query.date_to:
            return False
        return True

    def search(self, query: RecallQuery, *, limit: int = 5) -> RecallResult:
        if limit <= 0:
            raise ValueError("recall result limit must be positive")
        documents = [
            document
            for document in self._load_documents()
            if self._matches_filters(document, query)
        ]
        semantic_scores: list[float | None] = [None] * len(documents)
        semantic_available = False
        semantic_status = "disabled"
        if documents and self.semantic_scorer is not None:
            try:
                values = list(
                    self.semantic_scorer(
                        query.text,
                        [document.search_text for document in documents],
                    )
                )
                if len(values) != len(documents):
                    raise ValueError("semantic scorer returned the wrong vector length")
                semantic_scores = [max(-1.0, min(1.0, float(value))) for value in values]
                semantic_available = True
                semantic_status = "ok"
            except Exception as exc:
                semantic_scores = [None] * len(documents)
                semantic_status = f"error:{type(exc).__name__}"

        query_variants = tuple(
            dict.fromkeys(
                text.strip() for text in (query.text, *query.variants) if text.strip()
            )
        )
        normalized_documents = [
            _normalized(document.search_text) for document in documents
        ]
        variant_terms = {
            variant: _query_terms(variant) for variant in query_variants
        }
        variant_rarity_terms = {
            variant: _rarity_terms(variant) for variant in query_variants
        }
        entity_rarity_terms = tuple(
            dict.fromkeys(
                term
                for value in query.entity_terms
                for term in _rarity_terms(value)
            )
        )
        all_terms = tuple(
            dict.fromkeys(
                term
                for variant in query_variants
                for term in variant_rarity_terms[variant]
            )
            | dict.fromkeys(entity_rarity_terms)
        )
        document_frequency = {
            term: sum(term in text for text in normalized_documents)
            for term in all_terms
        }
        total_documents = max(1, len(documents))

        def rarity(term: str) -> float:
            # Normalised inverse document frequency is only a bounded tie-breaker.
            # It must never turn a non-match into a relevant candidate.
            numerator = math.log(
                (total_documents + 1) / (document_frequency.get(term, 0) + 1)
            )
            denominator = math.log(total_documents + 1)
            return numerator / denominator if denominator else 0.0

        hits: list[RecallHit] = []
        for index, document in enumerate(documents):
            text = document.search_text
            normalized_text = normalized_documents[index]
            best_lexical = 0.0
            best_fuzzy = 0.0
            best_rarity = 0.0
            best_terms: tuple[str, ...] = ()
            exact = False
            direct_match = False
            for variant_index, variant in enumerate(query_variants):
                normalized_query = _normalized(variant)
                terms = variant_terms[variant]
                matched = tuple(term for term in terms if term in normalized_text)
                coverage = len(matched) / max(len(terms), 1)
                query_grams = _character_grams(variant)
                text_grams = _character_grams(text)
                gram_coverage = (
                    len(query_grams.intersection(text_grams)) / len(query_grams)
                    if query_grams
                    else 0.0
                )
                lexical = 0.58 * coverage + 0.42 * gram_coverage
                fuzzy = _fuzzy_ratio(variant, text)
                matched_rarity = max(
                    (
                        rarity(term)
                        for term in variant_rarity_terms[variant]
                        if term in normalized_text
                    ),
                    default=0.0,
                )
                if (lexical, matched_rarity) > (best_lexical, best_rarity):
                    best_lexical = lexical
                    best_rarity = matched_rarity
                    best_terms = matched
                best_fuzzy = max(best_fuzzy, fuzzy)
                exact = exact or bool(normalized_query and normalized_query in normalized_text)
                if variant_index == 0:
                    direct_match = bool(
                        normalized_query and normalized_query in normalized_text
                    )

            entity_terms = tuple(
                term
                for term in (_normalized(value) for value in query.entity_terms)
                if term
            )
            entity_coverage = (
                sum(term in normalized_text for term in entity_terms)
                / len(entity_terms)
                if entity_terms
                else 0.0
            )
            entity_rarity = max(
                (
                    rarity(term)
                    for term in entity_rarity_terms
                    if term in normalized_text
                ),
                default=0.0,
            )
            best_rarity = max(best_rarity, 0.5 * entity_rarity)

            semantic = semantic_scores[index]
            semantic_component = (
                max(0.0, (semantic - 0.15) / 0.75)
                if semantic is not None
                else 0.0
            )
            relevance = (
                0.46 * semantic_component
                + 0.32 * best_lexical
                + 0.16 * best_fuzzy
                + 0.06 * float(exact)
                + 0.06 * best_rarity
            )
            relevance = min(1.0, relevance + 0.06 * entity_coverage)
            structured_filter_count = sum(
                bool(value)
                for value in (
                    query.subject_ids,
                    query.event_types,
                    query.source_kinds,
                    query.date_from or query.date_to,
                )
            )
            if structured_filter_count:
                relevance = max(
                    relevance,
                    min(0.56, 0.14 * structured_filter_count),
                )
            if exact:
                relevance = max(relevance, 0.92)
            elif best_lexical >= 0.66:
                relevance = max(relevance, 0.72)
            if relevance <= 0.05:
                continue
            score = min(
                1.0,
                relevance
                + 0.025 * document.importance
                + 0.015 * document.confidence,
            )
            hits.append(
                RecallHit(
                    document=document,
                    score=score,
                    lexical_score=best_lexical,
                    fuzzy_score=best_fuzzy,
                    semantic_score=semantic,
                    rarity_score=best_rarity,
                    direct_match=direct_match,
                    matched_terms=best_terms,
                )
            )
        if query.sort_mode == "latest":
            hits.sort(
                key=lambda hit: (
                    (
                        hit.document.active_date_to
                        if query.date_basis == "active_date"
                        else hit.document.date_to
                    )
                    or "",
                    hit.score,
                    hit.document.document_id,
                ),
                reverse=True,
            )
        elif query.sort_mode == "earliest":
            hits.sort(
                key=lambda hit: (
                    (
                        hit.document.active_date_from
                        if query.date_basis == "active_date"
                        else hit.document.date_from
                    )
                    or "9999-99-99",
                    -hit.score,
                    hit.document.document_id,
                )
            )
        else:
            hits.sort(
                key=lambda hit: (
                    -hit.score,
                    (
                        hit.document.active_date_from
                        if query.date_basis == "active_date"
                        else hit.document.date_from
                    )
                    or "9999-99-99",
                    hit.document.document_id,
                )
            )
        selected_limit = min(int(limit), 50)
        selected_hits = tuple(hits[:selected_limit])
        decision_limit = min(len(hits), max(10, selected_limit * 2))
        decisions = tuple(
            RecallDecision(
                hit=hit,
                decision="selected" if index < selected_limit else "eliminated",
                reason="selected_top_k" if index < selected_limit else "output_limit",
            )
            for index, hit in enumerate(hits[:decision_limit])
        )
        return RecallResult(
            hits=selected_hits,
            candidate_count=len(documents),
            semantic_available=semantic_available,
            semantic_status=semantic_status,
            evaluated_count=len(documents),
            decisions=decisions,
            candidate_counts_by_layer=tuple(
                sorted(
                    {
                        layer: sum(
                            document.source_layer == layer for document in documents
                        )
                        for layer in {document.source_layer for document in documents}
                    }.items()
                )
            ),
            evaluated_counts_by_layer=tuple(
                sorted(
                    {
                        layer: sum(
                            document.source_layer == layer for document in documents
                        )
                        for layer in {document.source_layer for document in documents}
                    }.items()
                )
            ),
        )


__all__ = [
    "EventRecallIndex",
    "CachedEmbeddingSemanticScorer",
    "RecallDocument",
    "RecallDateBasis",
    "RecallDecision",
    "RecallHit",
    "RecallQuery",
    "RecallResult",
    "RecallSourceAnchor",
    "RecallSortMode",
    "SemanticScorer",
]
