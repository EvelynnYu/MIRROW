"""Rebuildable lexical candidate shortlist for Memory V2 recall.

The sidecar is deliberately not a memory authority.  It stores stable document
keys and derived search tokens only; callers always hydrate and rank the
current read-model documents from their authoritative sources.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Sequence

from .recall import EventRecallIndex, RecallDocument, RecallQuery


_INDEX_SCHEMA_VERSION = "memory-v2-recall-fts-v1"
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.IGNORECASE)
_HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _fts_tokens(value: str) -> tuple[str, ...]:
    """Tokenize Chinese as character bigrams and preserve Latin terms.

    SQLite's built-in unicode tokenizer does not segment unspaced Chinese.
    Materialising bigrams gives FTS a deterministic, local candidate signal;
    the existing ranker remains responsible for relevance.
    """

    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        clean = token.casefold().strip()
        if clean and clean not in seen:
            seen.add(clean)
            tokens.append(clean)

    for token in _LATIN_RE.findall(value):
        add(token)
    for run in _HAN_RUN_RE.findall(value):
        if len(run) == 1:
            add(run)
            continue
        for index in range(len(run) - 1):
            add(run[index : index + 2])
    return tuple(tokens)


def _document_key(document: RecallDocument) -> str:
    return f"{document.source_layer}\x1f{document.document_id}"


class SQLiteFTSCandidateSelector:
    """Select a bounded lexical shortlist without becoming a fact source."""

    def __init__(self, cache_path: str | Path):
        self.cache_path = str(cache_path)
        if self.cache_path != ":memory:":
            Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.cache_path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._revision = ""
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _ensure_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS recall_index_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            schema_row = self._connection.execute(
                "SELECT value FROM recall_index_meta WHERE key='schema_version'"
            ).fetchone()
            if schema_row is not None and str(schema_row["value"]) != _INDEX_SCHEMA_VERSION:
                self._connection.execute("DROP TABLE IF EXISTS recall_documents_fts")
                self._connection.execute("DELETE FROM recall_index_meta")
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recall_documents_fts USING fts5("
                "document_key UNINDEXED, source_layer UNINDEXED, "
                "active_date_from UNINDEXED, active_date_to UNINDEXED, "
                "date_from UNINDEXED, date_to UNINDEXED, tokens, "
                "tokenize='unicode61 remove_diacritics 2')"
            )
            self._connection.execute(
                "INSERT INTO recall_index_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_INDEX_SCHEMA_VERSION,),
            )

    @staticmethod
    def _documents_revision(documents: Sequence[RecallDocument]) -> str:
        digest = hashlib.sha256()
        for document in sorted(documents, key=_document_key):
            digest.update(_document_key(document).encode("utf-8"))
            digest.update(b"\0")
            digest.update(document.search_text.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def synchronize(self, documents: Sequence[RecallDocument]) -> bool:
        """Make the sidecar reflect the current materialized read model.

        Returns True when rows were rebuilt.  A matching persisted revision is
        reused across process restarts.
        """

        revision = self._documents_revision(documents)
        with self._lock:
            if revision == self._revision:
                return False
            stored = self._connection.execute(
                "SELECT value FROM recall_index_meta WHERE key='documents_revision'"
            ).fetchone()
            if stored is not None and str(stored["value"]) == revision:
                self._revision = revision
                return False
            rows = [
                (
                    _document_key(document),
                    document.source_layer,
                    document.active_date_from,
                    document.active_date_to,
                    document.date_from,
                    document.date_to,
                    " ".join(_fts_tokens(document.search_text)),
                )
                for document in documents
            ]
            with self._connection:
                self._connection.execute("DELETE FROM recall_documents_fts")
                self._connection.executemany(
                    "INSERT INTO recall_documents_fts("
                    "document_key, source_layer, active_date_from, active_date_to, "
                    "date_from, date_to, tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._connection.execute(
                    "INSERT INTO recall_index_meta(key, value) "
                    "VALUES('documents_revision', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (revision,),
                )
            self._revision = revision
            return True

    def invalidate(self) -> None:
        """Forget the in-process revision; persisted rows remain rebuildable."""

        self._revision = ""

    @staticmethod
    def _fallback_order(
        documents: Sequence[RecallDocument], query: RecallQuery
    ) -> list[RecallDocument]:
        if query.sort_mode == "earliest":
            return sorted(
                documents,
                key=lambda item: (
                    item.active_date_from
                    if query.date_basis == "active_date"
                    else item.date_from
                )
                or "9999-99-99",
            )
        return sorted(
            documents,
            key=lambda item: (
                (
                    item.active_date_to
                    if query.date_basis == "active_date"
                    else item.date_to
                )
                or "",
                item.importance,
                item.confidence,
                item.document_id,
            ),
            reverse=True,
        )

    def select(
        self,
        documents: Sequence[RecallDocument],
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RecallDocument, ...]:
        if limit <= 0:
            raise ValueError("candidate shortlist limit must be positive")
        eligible = [
            document
            for document in documents
            if EventRecallIndex._matches_filters(document, query)
        ]
        if len(eligible) <= limit:
            return tuple(eligible)

        query_tokens = _fts_tokens(
            "\n".join((query.text, *query.variants, *query.entity_terms))
        )
        selected: list[RecallDocument] = []
        selected_keys: set[str] = set()
        by_key = {_document_key(document): document for document in eligible}
        if query_tokens:
            match_expression = " OR ".join(f'"{token}"' for token in query_tokens)
            layer = documents[0].source_layer if documents else ""
            conditions = ["recall_documents_fts MATCH ?", "source_layer=?"]
            parameters: list[object] = [match_expression, layer]
            date_prefix = "active_date" if query.date_basis == "active_date" else "date"
            if query.date_from:
                conditions.append(f"({date_prefix}_to='' OR {date_prefix}_to>=?)")
                parameters.append(query.date_from)
            if query.date_to:
                conditions.append(f"({date_prefix}_from='' OR {date_prefix}_from<=?)")
                parameters.append(query.date_to)
            fetch_size = min(len(documents), max(limit * 4, 256))
            offset = 0
            while len(selected) < limit and offset < len(documents):
                with self._lock:
                    rows = self._connection.execute(
                        "SELECT document_key FROM recall_documents_fts WHERE "
                        + " AND ".join(conditions)
                        + " ORDER BY bm25(recall_documents_fts), rowid LIMIT ? OFFSET ?",
                        (*parameters, fetch_size, offset),
                    ).fetchall()
                if not rows:
                    break
                offset += len(rows)
                for row in rows:
                    key = str(row["document_key"])
                    document = by_key.get(key)
                    if document is None or key in selected_keys:
                        continue
                    selected.append(document)
                    selected_keys.add(key)
                    if len(selected) >= limit:
                        break

        # A bounded deterministic tail preserves date-only/structured queries
        # and gives the final ranker a weak fallback when wording has no overlap.
        fallback_limit = min(12, limit)
        for document in self._fallback_order(eligible, query):
            if len(selected) >= limit or fallback_limit <= 0:
                break
            key = _document_key(document)
            if key in selected_keys:
                continue
            selected.append(document)
            selected_keys.add(key)
            fallback_limit -= 1
        return tuple(selected)


__all__ = ["SQLiteFTSCandidateSelector"]
