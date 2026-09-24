"""Hybrid, read-only search over Agent's bookshelf archives.

The archive SQLite database remains authoritative.  This module builds a
rebuildable sidecar vector cache whose rows retain stable source identifiers;
cache loss or embedding failure therefore degrades to lexical/fuzzy search
without changing archive data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Optional

from message_roles import is_ui_only_role


logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")
_LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_DATA_URL_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+", re.I)
_LONG_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{300,}={0,2}")
_PROTOCOL_REPLACEMENTS = {
    "TOOL_CALL": "ＴＯＯＬ＿ＣＡＬＬ",
    "NATURAL_LANGUAGE": "ＮＡＴＵＲＡＬ＿ＬＡＮＧＵＡＧＥ",
    "RESULTS_USED": "ＲＥＳＵＬＴＳ＿ＵＳＥＤ",
    "MIRROW:AFFECT": "ＭＩＲＲＯＷ：ＡＦＦＥＣＴ",
}
_STOP_WORDS = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "的", "了", "是",
    "在", "和", "就", "都", "也", "还", "把", "被", "给", "让", "这", "那",
    "一个", "一下", "什么", "怎么", "哪", "吗", "呢", "吧", "啊", "想", "帮",
    "搜索", "搜", "找", "查", "记录", "对话", "收藏", "书柜",
}
_SOURCE_LABELS = {
    "conversation": "历史对话",
    "bookmarks": "收藏夹",
    "events": "重要事件",
    "diary": "日记",
    "calendar": "纪念日",
    "open_loops": "已结束开放线索",
}


def sanitize_archive_text(value: Any, *, limit: int = 2600) -> str:
    """Return bounded quoted evidence that cannot masquerade as live protocol."""
    text = str(value or "")
    try:
        from context_builder.text_utils import strip_internal_history_markers
        from tool_action_protocol import strip_tool_protocol_text

        text = strip_internal_history_markers(text)
        text = strip_tool_protocol_text(text)
    except Exception:
        pass
    text = _DATA_URL_RE.sub("[图片数据已省略]", text)
    text = _LONG_BASE64_RE.sub("[大字段已省略]", text)
    text = _BEARER_RE.sub("Bearer [已隐藏]", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[已隐藏]", text)
    text = _IPV4_RE.sub("[IP已隐藏]", text)
    for marker, replacement in _PROTOCOL_REPLACEMENTS.items():
        text = re.sub(re.escape(marker), replacement, text, flags=re.I)
    text = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    text = "\n".join(line for line in text.splitlines() if line).strip()
    return text[:limit]


def _normalized(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text


def focused_archive_excerpt(text: str, terms: Iterable[str], *, limit: int = 1200) -> str:
    """Center a bounded result on the first real match instead of window start."""
    clean = sanitize_archive_text(text, limit=max(limit * 4, limit))
    if len(clean) <= limit:
        return clean
    folded = clean.casefold()
    positions = [folded.find(str(term).casefold()) for term in terms if str(term).strip()]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(clean), start + limit)
    start = max(0, end - limit)
    excerpt = clean[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(clean):
        excerpt += "…"
    return excerpt


def _query_terms(query: str) -> list[str]:
    """Extract useful terms while preserving Latin titles such as ``Hush``."""
    terms: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP_WORDS or term in seen:
            return
        if len(term) == 1 and not ("\u4e00" <= term <= "\u9fff"):
            return
        seen.add(term)
        terms.append(term)

    for match in _LATIN_RE.findall(query):
        add(match)
    try:
        import jieba

        for item in jieba.lcut(query, cut_all=False):
            add(item)
    except Exception:
        for item in re.findall(r"[\u4e00-\u9fff]{1,4}|[A-Za-z0-9_.-]{2,}", query):
            add(item)
    return terms[:16]


@dataclass
class SearchDocument:
    id: str
    source: str
    text: str
    date: str = ""
    session_id: str = ""
    role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:20]


@dataclass
class SearchHit:
    document: SearchDocument
    score: float
    tier: str
    matched_terms: list[str]
    semantic_score: Optional[float]
    fuzzy_score: float

    def as_dict(self) -> dict[str, Any]:
        doc = self.document
        return {
            "record_id": doc.id,
            "source": doc.source,
            "source_label": _SOURCE_LABELS.get(doc.source, doc.source),
            "date": doc.date,
            "session_id": doc.session_id,
            "role": doc.role,
            "content": focused_archive_excerpt(doc.text, self.matched_terms),
            "match_score": round(self.score, 1),
            "match_tier": self.tier,
            "matched_terms": self.matched_terms,
            "semantic_score": (
                round(self.semantic_score, 3) if self.semantic_score is not None else None
            ),
            "fuzzy_score": round(self.fuzzy_score, 1),
            **doc.metadata,
        }


def _rows_to_conversation_documents(rows: Iterable[sqlite3.Row]) -> list[SearchDocument]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        if is_ui_only_role(item.get("role")):
            continue
        content = sanitize_archive_text(item.get("content"), limit=900)
        if not content:
            continue
        key = (str(item.get("active_date") or ""), str(item.get("session_id") or ""))
        item["content"] = content
        groups.setdefault(key, []).append(item)

    documents: list[SearchDocument] = []
    labels = {"user": "人类伙伴", "assistant": "Agent", "system": "系统事实"}
    # Two-message overlap catches facts split at a boundary without turning
    # the whole archive into thousands of near-duplicate result windows.
    window_size, stride = 10, 8
    for (active_date, session_id), messages in groups.items():
        for start in range(0, len(messages), stride):
            window = messages[start:start + window_size]
            if not window:
                continue
            first, last = window[0], window[-1]
            first_id = str(first.get("message_id") or first.get("id") or start)
            last_id = str(last.get("message_id") or last.get("id") or start + len(window) - 1)
            lines = [
                f"[{labels.get(str(msg.get('role') or ''), 'Agent')}] {msg['content']}"
                for msg in window
            ]
            documents.append(SearchDocument(
                id=f"conversation:{session_id}:{first_id}:{last_id}",
                source="conversation",
                text=sanitize_archive_text("\n".join(lines)),
                date=active_date or str(first.get("timestamp") or "")[:10],
                session_id=session_id,
                role="conversation_window",
                metadata={"first_message_id": first_id, "last_message_id": last_id},
            ))
            if start + window_size >= len(messages):
                break
    return documents


class BookshelfSearchService:
    """Search archive originals with lexical, fuzzy and cached semantic signals."""

    def __init__(self, db_path: str, *, cache_path: Optional[str] = None, embedder: Any = None):
        self.db_path = str(db_path)
        default_cache = str(Path(self.db_path).with_name("bookshelf_search_vectors.npz"))
        self.cache_path = cache_path or default_cache
        self._embedder = embedder
        self._index_lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    def _load_documents(self, sources: set[str]) -> list[SearchDocument]:
        if not os.path.isfile(self.db_path):
            return []
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        docs: list[SearchDocument] = []
        try:
            if "conversation" in sources:
                rows = conn.execute(
                    "SELECT id, active_date, session_id, timestamp, role, content, message_id "
                    "FROM conversation_messages ORDER BY active_date, session_id, timestamp, id"
                ).fetchall()
                docs.extend(_rows_to_conversation_documents(rows))
            if "bookmarks" in sources:
                rows = conn.execute(
                    "SELECT id, original_msg_id, session_id, original_timestamp, collected_by, "
                    "role, created, content FROM bookmarks ORDER BY created DESC"
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    content = sanitize_archive_text(item.get("content"), limit=1800)
                    if not content and item.get("original_msg_id"):
                        fallback = conn.execute(
                            "SELECT content FROM conversation_messages WHERE message_id=? LIMIT 1",
                            (item["original_msg_id"],),
                        ).fetchone()
                        content = sanitize_archive_text(fallback[0] if fallback else "", limit=1800)
                    if content:
                        docs.append(SearchDocument(
                            id=f"bookmarks:{item['id']}", source="bookmarks", text=content,
                            date=str(item.get("original_timestamp") or item.get("created") or "")[:10],
                            session_id=str(item.get("session_id") or ""), role=str(item.get("role") or ""),
                            metadata={"bookmark_id": item["id"], "original_msg_id": item.get("original_msg_id", "")},
                        ))
            if "events" in sources:
                for row in conn.execute(
                    "SELECT id, date_string, event_text, emotion FROM important_events ORDER BY date_string"
                ).fetchall():
                    item = dict(row)
                    content = sanitize_archive_text(
                        f"{item.get('event_text', '')} {item.get('emotion', '')}", limit=1800
                    )
                    if content:
                        docs.append(SearchDocument(
                            id=f"events:{item['id']}", source="events", text=content,
                            date=str(item.get("date_string") or ""), metadata={"event_id": item["id"]},
                        ))
            if "diary" in sources:
                for row in conn.execute(
                    "SELECT id, date, content FROM diary_entries ORDER BY date"
                ).fetchall():
                    item = dict(row)
                    content = sanitize_archive_text(item.get("content"))
                    if content:
                        docs.append(SearchDocument(
                            id=f"diary:{item['id']}", source="diary", text=content,
                            date=str(item.get("date") or ""), metadata={"diary_id": item["id"]},
                        ))
        finally:
            conn.close()
        return docs

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            from vector_embedder import get_global_embedder

            self._embedder = get_global_embedder()
        return self._embedder

    def _document_vectors(self, documents: list[SearchDocument]):
        """Reuse unchanged vectors from a safe NPZ sidecar and encode the rest."""
        import numpy as np

        with self._index_lock:
            cached: dict[tuple[str, str], Any] = {}
            cached_by_id: dict[str, tuple[str, Any]] = {}
            if os.path.isfile(self.cache_path):
                try:
                    data = np.load(self.cache_path, allow_pickle=False)
                    ids = data["ids"].tolist()
                    hashes = data["hashes"].tolist()
                    vectors = data["vectors"]
                    if len(ids) == len(hashes) == len(vectors):
                        cached = {(str(i), str(h)): vectors[n] for n, (i, h) in enumerate(zip(ids, hashes))}
                        cached_by_id = {
                            str(i): (str(h), vectors[n])
                            for n, (i, h) in enumerate(zip(ids, hashes))
                        }
                except Exception as exc:
                    logger.info("bookshelf vector cache ignored: %s", type(exc).__name__)

            vectors: list[Any] = [None] * len(documents)
            missing_indices: list[int] = []
            for index, doc in enumerate(documents):
                vector = cached.get((doc.id, doc.content_hash))
                if vector is None:
                    missing_indices.append(index)
                else:
                    vectors[index] = vector
            if missing_indices:
                embedder = self._get_embedder()
                encoded = embedder.embed_texts(
                    [documents[index].text for index in missing_indices], batch_size=32
                )
                for index, vector in zip(missing_indices, encoded):
                    vectors[index] = vector

            matrix = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.maximum(norms, 1e-8)
            try:
                cache_dir = os.path.dirname(self.cache_path)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                temp_path = self.cache_path + ".tmp.npz"
                # Preserve vectors from other bookshelf partitions. A search
                # restricted to bookmarks must not evict the already-built
                # conversation index and make the next query rebuild it.
                for index, doc in enumerate(documents):
                    cached_by_id[doc.id] = (doc.content_hash, matrix[index])
                saved_ids = list(cached_by_id)
                np.savez_compressed(
                    temp_path,
                    ids=np.asarray(saved_ids),
                    hashes=np.asarray([cached_by_id[item][0] for item in saved_ids]),
                    vectors=np.asarray([cached_by_id[item][1] for item in saved_ids], dtype=np.float32),
                )
                os.replace(temp_path, self.cache_path)
            except Exception as exc:
                logger.info("bookshelf vector cache write skipped: %s", type(exc).__name__)
            return matrix

    @staticmethod
    def _fuzzy(query: str, text: str) -> float:
        try:
            from rapidfuzz import fuzz

            return max(float(fuzz.token_set_ratio(query, text)), float(fuzz.partial_ratio(query, text)))
        except Exception:
            from difflib import SequenceMatcher

            return SequenceMatcher(None, query, text[: max(len(query) * 5, 200)]).ratio() * 100

    async def search(
        self,
        query: str,
        *,
        sources: Optional[set[str]] = None,
        limit: int = 5,
        min_score: float = 55.0,
        extra_documents: Optional[list[SearchDocument]] = None,
        exclude_text: str = "",
    ) -> dict[str, Any]:
        query = sanitize_archive_text(query, limit=300)
        selected_sources = (
            {"conversation", "bookmarks", "events", "diary"}
            if sources is None else set(sources)
        )
        documents = await asyncio.to_thread(self._load_documents, selected_sources)
        documents.extend(extra_documents or [])
        # The current user message is persisted before tools run. Without this
        # exclusion, a search can rank its own request as an exact historical
        # match ("find Hush" -> the just-written "find Hush" line).
        excluded = _normalized(exclude_text)
        if excluded:
            documents = [
                doc for doc in documents
                if not (doc.source == "conversation" and excluded in _normalized(doc.text))
            ]
        if not query or not documents:
            return {"hits": [], "semantic_available": False, "query": query}

        normalized_query = _normalized(query)
        terms = _query_terms(query)
        latin_terms = [term for term in terms if _LATIN_RE.fullmatch(term)]
        semantic_scores: list[Optional[float]] = [None] * len(documents)
        semantic_available = False
        async with self._async_lock:
            try:
                import numpy as np

                matrix = await asyncio.to_thread(self._document_vectors, documents)
                embedder = self._get_embedder()
                query_vector = await asyncio.to_thread(embedder.embed_text, query)
                query_vector = np.asarray(query_vector, dtype=np.float32)
                query_vector /= max(float(np.linalg.norm(query_vector)), 1e-8)
                semantic_scores = [float(value) for value in matrix.dot(query_vector)]
                semantic_available = True
            except Exception as exc:
                logger.info("bookshelf semantic search unavailable: %s", type(exc).__name__)

        scored: list[SearchHit] = []
        for index, doc in enumerate(documents):
            normalized_text = _normalized(doc.text)
            matched = [term for term in terms if _normalized(term) in normalized_text]
            weights = [2.0 if term in latin_terms or len(_normalized(term)) >= 4 else 1.0 for term in terms]
            matched_weight = sum(weight for term, weight in zip(terms, weights) if term in matched)
            coverage = matched_weight / max(sum(weights), 1.0)
            named_coverage = (
                sum(1 for term in latin_terms if _normalized(term) in normalized_text) / len(latin_terms)
                if latin_terms else 0.0
            )
            exact = bool(normalized_query and normalized_query in normalized_text)
            fuzzy_score = self._fuzzy(query, doc.text)
            fuzzy_component = fuzzy_score / 100.0
            semantic = semantic_scores[index]
            semantic_component = max(0.0, min(1.0, ((semantic or 0.0) - 0.15) / 0.65))
            combined = 100.0 * (
                0.34 * coverage
                + 0.34 * semantic_component
                + 0.16 * fuzzy_component
                + 0.10 * float(exact)
                + 0.06 * named_coverage
            )
            if exact:
                combined = max(combined, 92.0)
            elif latin_terms and named_coverage >= 1.0 and coverage >= 0.55:
                combined = max(combined, 78.0)
            elif latin_terms and named_coverage >= 1.0 and coverage >= 0.30:
                combined = max(combined, 55.0 + (coverage - 0.30) * 45.0)
            elif coverage >= 0.66 or (semantic is not None and semantic >= 0.68):
                combined = max(combined, 70.0)
            combined = min(99.0, combined)
            if not matched and fuzzy_score < 35 and (semantic is None or semantic < 0.35):
                continue
            tier = "high" if combined >= 75 else ("possible" if combined >= min_score else "weak")
            scored.append(SearchHit(doc, combined, tier, matched, semantic, fuzzy_score))

        first_intent = any(word in query for word in ("第一次", "最早", "初次", "头一回"))
        if first_intent:
            scored.sort(key=lambda hit: (
                -(1 if latin_terms and all(term in hit.matched_terms for term in latin_terms) else 0),
                -(1 if hit.score >= min_score else 0),
                hit.document.date or "9999-99-99",
                -hit.score,
            ))
        else:
            scored.sort(key=lambda hit: (-hit.score, hit.document.date or "9999-99-99"))

        deduped: list[SearchHit] = []
        seen: set[tuple[str, str, str]] = set()
        for hit in scored:
            key = (hit.document.source, hit.document.date, hit.document.session_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hit)

        strong = [hit for hit in deduped if hit.score >= min_score]
        chosen = strong[: max(1, min(int(limit), 10))]
        used_weak_fallback = False
        if not chosen:
            chosen = deduped[: min(3, max(1, int(limit)))]
            used_weak_fallback = bool(chosen)
        return {
            "query": query,
            "hits": [hit.as_dict() for hit in chosen],
            "semantic_available": semantic_available,
            "used_weak_fallback": used_weak_fallback,
            "min_score": float(min_score),
        }


_services: dict[str, BookshelfSearchService] = {}


def get_bookshelf_search_service(db_path: str) -> BookshelfSearchService:
    key = os.path.abspath(str(db_path))
    if key not in _services:
        _services[key] = BookshelfSearchService(key)
    return _services[key]


__all__ = [
    "BookshelfSearchService", "SearchDocument", "SearchHit",
    "focused_archive_excerpt", "get_bookshelf_search_service", "sanitize_archive_text",
]
