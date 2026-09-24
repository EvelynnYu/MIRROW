"""Rebuildable semantic candidates for Memory V2 recall.

Embeddings are a derived search cache, never a memory authority.  The index
stores stable document keys, text digests and normalized vectors, but no source
text.  A hybrid selector reserves bounded space for both lexical precision and
semantic paraphrase recovery; failure of the optional semantic path leaves the
FTS shortlist usable.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol, Sequence

from .multi_source_recall import RecallCandidateSelector
from .recall import EventRecallIndex, RecallDocument, RecallQuery


_SCHEMA_VERSION = "memory-v2-recall-semantic-v1"


class SemanticIndexNotReadyError(RuntimeError):
    """A read-only semantic cache does not match the current read model."""


class TextEmbedder(Protocol):
    def embed_text(self, text: str) -> Any: ...

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> Any: ...


class QueryInstructionEmbedder:
    """Keep passage text unchanged while prefixing retrieval queries."""

    def __init__(self, embedder: TextEmbedder, instruction: str):
        if not instruction.strip():
            raise ValueError("query instruction must not be empty")
        self.embedder = embedder
        self.instruction = instruction

    def embed_text(self, text: str) -> Any:
        return self.embedder.embed_text(text)

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> Any:
        return self.embedder.embed_texts(texts, batch_size=batch_size)

    def embed_query(self, text: str) -> Any:
        return self.embedder.embed_text(f"{self.instruction}{text}")


def _document_key(document: RecallDocument) -> str:
    return f"{document.source_layer}\x1f{document.document_id}"


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalised_vector(value: Any):
    import numpy as np

    vector = np.asarray(value, dtype=np.float32).reshape(-1)
    if not vector.size or not np.isfinite(vector).all():
        raise ValueError("embedding vector must be finite and non-empty")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        raise ValueError("embedding vector must not be zero")
    return vector / norm


class SQLiteSemanticCandidateIndex:
    """Persist document embeddings and provide both shortlist and score hooks."""

    def __init__(
        self,
        cache_path: str | Path,
        *,
        embedder: TextEmbedder,
        model_id: str,
        allow_rebuild: bool,
        batch_size: int = 32,
        source_layers: Sequence[str] | None = None,
    ):
        if not model_id.strip():
            raise ValueError("semantic model_id must not be empty")
        if batch_size <= 0:
            raise ValueError("semantic batch_size must be positive")
        self.cache_path = str(cache_path)
        self.embedder = embedder
        self.model_id = model_id.strip()
        self.allow_rebuild = bool(allow_rebuild)
        self.batch_size = int(batch_size)
        self.source_layers = (
            frozenset(str(layer) for layer in source_layers if str(layer))
            if source_layers is not None
            else None
        )
        if self.source_layers is not None and not self.source_layers:
            raise ValueError("semantic source_layers must not be empty")
        if not self.allow_rebuild:
            if self.cache_path == ":memory:" or not Path(self.cache_path).is_file():
                raise FileNotFoundError("read-only semantic cache does not exist")
            connection_target = Path(self.cache_path).resolve().as_uri() + "?mode=ro"
            self._connection = sqlite3.connect(
                connection_target,
                timeout=30,
                check_same_thread=False,
                uri=True,
            )
        else:
            if self.cache_path != ":memory:":
                Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.cache_path,
                timeout=30,
                check_same_thread=False,
            )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._revision = ""
        self._ready = False
        self._vectors: dict[str, Any] = {}
        self._vectors_by_digest: dict[str, Any] = {}
        self._query_vectors: dict[str, Any] = {}
        try:
            if self.allow_rebuild:
                self._ensure_schema()
            else:
                self._validate_schema()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _ensure_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_index_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            schema_row = self._connection.execute(
                "SELECT value FROM semantic_index_meta WHERE key='schema_version'"
            ).fetchone()
            if schema_row is not None and str(schema_row["value"]) != _SCHEMA_VERSION:
                self._connection.execute("DROP TABLE IF EXISTS semantic_documents")
                self._connection.execute("DELETE FROM semantic_index_meta")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_documents ("
                "document_key TEXT PRIMARY KEY, source_layer TEXT NOT NULL, "
                "text_digest TEXT NOT NULL, dimension INTEGER NOT NULL, "
                "vector BLOB NOT NULL)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_documents_layer "
                "ON semantic_documents(source_layer)"
            )
            self._connection.execute(
                "INSERT INTO semantic_index_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_SCHEMA_VERSION,),
            )

    def _validate_schema(self) -> None:
        try:
            schema_row = self._connection.execute(
                "SELECT value FROM semantic_index_meta WHERE key='schema_version'"
            ).fetchone()
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(semantic_documents)"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise SemanticIndexNotReadyError(
                "semantic cache schema is unavailable"
            ) from exc
        expected = {
            "document_key",
            "source_layer",
            "text_digest",
            "dimension",
            "vector",
        }
        if (
            schema_row is None
            or str(schema_row["value"]) != _SCHEMA_VERSION
            or not expected.issubset(columns)
        ):
            raise SemanticIndexNotReadyError(
                "semantic cache schema does not match the reader"
            )

    def _documents_revision(self, documents: Sequence[RecallDocument]) -> str:
        digest = hashlib.sha256()
        digest.update(self.model_id.encode("utf-8"))
        digest.update(b"\0")
        for document in sorted(documents, key=_document_key):
            digest.update(_document_key(document).encode("utf-8"))
            digest.update(b"\0")
            digest.update(_text_digest(document.search_text).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _indexed_documents(
        self,
        documents: Sequence[RecallDocument],
    ) -> tuple[RecallDocument, ...]:
        if self.source_layers is None:
            return tuple(documents)
        return tuple(
            document
            for document in documents
            if document.source_layer in self.source_layers
        )

    @staticmethod
    def _decode_vector(row: sqlite3.Row):
        import numpy as np

        vector = np.frombuffer(row["vector"], dtype="<f4").copy()
        if vector.size != int(row["dimension"]):
            raise ValueError("persisted embedding dimension does not match payload")
        return _normalised_vector(vector)

    def _load_persisted_vectors(self) -> None:
        rows = self._connection.execute(
            "SELECT document_key, text_digest, dimension, vector "
            "FROM semantic_documents"
        ).fetchall()
        vectors: dict[str, Any] = {}
        by_digest: dict[str, Any] = {}
        for row in rows:
            vector = self._decode_vector(row)
            vectors[str(row["document_key"])] = vector
            by_digest.setdefault(str(row["text_digest"]), vector)
        self._vectors = vectors
        self._vectors_by_digest = by_digest

    def synchronize(self, documents: Sequence[RecallDocument]) -> bool:
        """Synchronize vectors, embedding only changed or previously unseen text."""

        indexed_documents = self._indexed_documents(documents)
        revision = self._documents_revision(indexed_documents)
        with self._lock:
            if revision == self._revision and len(self._vectors) == len(indexed_documents):
                self._ready = True
                return False
            meta = {
                str(row["key"]): str(row["value"])
                for row in self._connection.execute(
                    "SELECT key, value FROM semantic_index_meta"
                ).fetchall()
            }
            if (
                meta.get("documents_revision") == revision
                and meta.get("model_id") == self.model_id
            ):
                self._load_persisted_vectors()
                self._revision = revision
                self._ready = True
                return False

            if not self.allow_rebuild:
                raise SemanticIndexNotReadyError(
                    "semantic cache is missing or stale; rebuild it offline"
                )

            reusable: dict[tuple[str, str], Any] = {}
            if meta.get("model_id") == self.model_id:
                for row in self._connection.execute(
                    "SELECT document_key, text_digest, dimension, vector "
                    "FROM semantic_documents"
                ).fetchall():
                    reusable[(str(row["document_key"]), str(row["text_digest"]))] = (
                        self._decode_vector(row)
                    )

            pending: list[tuple[str, str, str]] = []
            vectors: dict[str, Any] = {}
            for document in indexed_documents:
                key = _document_key(document)
                digest = _text_digest(document.search_text)
                cached = reusable.get((key, digest))
                if cached is None:
                    pending.append((key, digest, document.search_text))
                else:
                    vectors[key] = cached

            if pending:
                encoded = self.embedder.embed_texts(
                    [text for _, _, text in pending],
                    batch_size=self.batch_size,
                )
                if len(encoded) != len(pending):
                    raise ValueError("embedder returned the wrong document vector count")
                for (key, _digest, _text), value in zip(pending, encoded):
                    vectors[key] = _normalised_vector(value)

            rows = []
            by_digest: dict[str, Any] = {}
            for document in indexed_documents:
                key = _document_key(document)
                digest = _text_digest(document.search_text)
                vector = vectors[key]
                by_digest.setdefault(digest, vector)
                rows.append(
                    (
                        key,
                        document.source_layer,
                        digest,
                        int(vector.size),
                        vector.astype("<f4", copy=False).tobytes(),
                    )
                )
            with self._connection:
                self._connection.execute("DELETE FROM semantic_documents")
                self._connection.executemany(
                    "INSERT INTO semantic_documents("
                    "document_key, source_layer, text_digest, dimension, vector"
                    ") VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                self._connection.executemany(
                    "INSERT INTO semantic_index_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        ("model_id", self.model_id),
                        ("documents_revision", revision),
                    ),
                )
            self._vectors = vectors
            self._vectors_by_digest = by_digest
            self._query_vectors.clear()
            self._revision = revision
            self._ready = True
            return True

    def invalidate(self) -> None:
        with self._lock:
            self._revision = ""
            self._ready = False
            self._vectors.clear()
            self._vectors_by_digest.clear()
            self._query_vectors.clear()

    def _query_vector(self, text: str):
        digest = _text_digest(text)
        vector = self._query_vectors.get(digest)
        if vector is None:
            embed_query = getattr(self.embedder, "embed_query", None)
            vector = _normalised_vector(
                embed_query(text)
                if callable(embed_query)
                else self.embedder.embed_text(text)
            )
            self._query_vectors[digest] = vector
        return vector

    def select(
        self,
        documents: Sequence[RecallDocument],
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RecallDocument, ...]:
        if limit <= 0:
            raise ValueError("semantic candidate limit must be positive")
        if not self._ready:
            raise SemanticIndexNotReadyError(
                "semantic cache has not passed revision validation"
            )
        eligible = [
            document
            for document in self._indexed_documents(documents)
            if EventRecallIndex._matches_filters(document, query)
        ]
        if len(eligible) <= limit:
            return tuple(eligible)
        with self._lock:
            query_vector = self._query_vector(query.text)
            scored = []
            for document in eligible:
                vector = self._vectors.get(_document_key(document))
                if vector is None:
                    raise RuntimeError("semantic index is not synchronized")
                scored.append((float(vector.dot(query_vector)), document))
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].active_date_from,
                item[1].document_id,
            )
        )
        return tuple(document for _, document in scored[:limit])

    def score(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        """Score shortlisted text using the same normalized vector cache."""

        with self._lock:
            if not self._ready:
                raise SemanticIndexNotReadyError(
                    "semantic cache has not passed revision validation"
                )
            query_vector = self._query_vector(query)
            missing: list[tuple[str, str]] = []
            vectors = []
            for text in texts:
                digest = _text_digest(text)
                vector = self._vectors_by_digest.get(digest)
                if vector is None:
                    missing.append((digest, text))
                vectors.append(vector)
            if missing:
                encoded = self.embedder.embed_texts(
                    [text for _, text in missing],
                    batch_size=self.batch_size,
                )
                if len(encoded) != len(missing):
                    raise ValueError("embedder returned the wrong scoring vector count")
                for (digest, _text), value in zip(missing, encoded):
                    self._vectors_by_digest[digest] = _normalised_vector(value)
                vectors = [self._vectors_by_digest[_text_digest(text)] for text in texts]
            return [float(vector.dot(query_vector)) for vector in vectors]

    def __call__(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        return self.score(query, texts)


class HybridRecallCandidateSelector:
    """Reserve bounded shortlist capacity for FTS and semantic candidates."""

    def __init__(
        self,
        lexical: RecallCandidateSelector,
        semantic: RecallCandidateSelector,
        *,
        semantic_share: float = 1 / 3,
    ):
        if not 0 < semantic_share < 1:
            raise ValueError("semantic_share must be within (0, 1)")
        self.lexical = lexical
        self.semantic = semantic
        self.semantic_share = float(semantic_share)
        self.semantic_status = "not_synchronized"

    def synchronize(self, documents: Sequence[RecallDocument]) -> bool:
        lexical_changed = self.lexical.synchronize(documents)
        try:
            semantic_changed = self.semantic.synchronize(documents)
            self.semantic_status = "ok"
        except Exception as exc:
            semantic_changed = False
            self.semantic_status = f"error:{type(exc).__name__}"
        return lexical_changed or semantic_changed

    def invalidate(self) -> None:
        self.lexical.invalidate()
        self.semantic.invalidate()
        self.semantic_status = "not_synchronized"

    def select(
        self,
        documents: Sequence[RecallDocument],
        query: RecallQuery,
        *,
        limit: int,
    ) -> tuple[RecallDocument, ...]:
        if limit <= 0:
            raise ValueError("hybrid candidate limit must be positive")
        lexical_items = self.lexical.select(documents, query, limit=limit)
        if self.semantic_status != "ok":
            return lexical_items
        semantic_budget = min(limit, max(1, round(limit * self.semantic_share)))
        try:
            semantic_items = self.semantic.select(
                documents,
                query,
                limit=semantic_budget,
            )
        except Exception as exc:
            self.semantic_status = f"error:{type(exc).__name__}"
            return lexical_items

        lexical_primary = max(0, limit - semantic_budget)
        selected: list[RecallDocument] = []
        seen: set[str] = set()

        def add(items: Sequence[RecallDocument]) -> None:
            for document in items:
                if len(selected) >= limit:
                    return
                key = _document_key(document)
                if key in seen:
                    continue
                seen.add(key)
                selected.append(document)

        add(lexical_items[:lexical_primary])
        add(semantic_items)
        add(lexical_items[lexical_primary:])
        return tuple(selected)


__all__ = [
    "HybridRecallCandidateSelector",
    "QueryInstructionEmbedder",
    "SemanticIndexNotReadyError",
    "SQLiteSemanticCandidateIndex",
    "TextEmbedder",
]
