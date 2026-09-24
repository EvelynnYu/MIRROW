"""Local-only semantic model profile for Memory V2 shadow recall."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .semantic_candidates import (
    QueryInstructionEmbedder,
    SQLiteSemanticCandidateIndex,
)


DEFAULT_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DEFAULT_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
QUERY_INSTRUCTION_VERSION = "query-instruction-v1"


@dataclass(frozen=True)
class LocalSemanticProfile:
    model_name: str
    revision: str
    snapshot_path: Path
    vector_cache_path: Path
    query_instruction: str

    @property
    def model_id(self) -> str:
        return (
            f"{self.model_name}@{self.revision}+{QUERY_INSTRUCTION_VERSION}"
        )


def _model_cache_folder(model_name: str) -> str:
    requested = model_name.replace("\\", "/").strip()
    if "/" not in requested:
        requested = f"sentence-transformers/{requested}"
    return "models--" + requested.replace("/", "--")


def _complete_snapshot(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "config.json").is_file()
        and (path / "modules.json").is_file()
        and any(
            (path / filename).is_file()
            for filename in ("model.safetensors", "pytorch_model.bin")
        )
    )


def _configured_value(
    values: Mapping[str, str],
    key: str,
    default: str,
) -> str:
    value = str(values.get(key, "") or "").strip()
    return value or default


def discover_local_semantic_profile(
    backend_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> LocalSemanticProfile:
    """Resolve one complete local snapshot without downloading anything."""

    values = os.environ if environ is None else environ
    root = Path(backend_root).resolve()
    model_name = _configured_value(
        values,
        "MIRROW_MEMORY_V2_SEMANTIC_MODEL",
        DEFAULT_MODEL_NAME,
    )
    cache_root = Path(
        _configured_value(
            values,
            "MIRROW_MEMORY_V2_MODEL_CACHE",
            str(root / "model_cache"),
        )
    ).resolve()
    snapshots_root = cache_root / _model_cache_folder(model_name) / "snapshots"
    requested_revision = values.get(
        "MIRROW_MEMORY_V2_SEMANTIC_REVISION",
        "",
    ).strip()
    if requested_revision:
        candidates = (snapshots_root / requested_revision,)
    else:
        candidates = tuple(
            sorted(
                (path for path in snapshots_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )
        ) if snapshots_root.is_dir() else ()
    snapshot = next((path for path in candidates if _complete_snapshot(path)), None)
    if snapshot is None:
        raise FileNotFoundError("no complete local semantic model snapshot")
    instruction = _configured_value(
        values,
        "MIRROW_MEMORY_V2_SEMANTIC_QUERY_INSTRUCTION",
        DEFAULT_QUERY_INSTRUCTION,
    )
    vector_cache = Path(
        _configured_value(
            values,
            "MIRROW_MEMORY_V2_SEMANTIC_CACHE",
            str(root / ".tmp" / "memory_v2_semantic_bge_small_zh_v15.db"),
        )
    ).resolve()
    return LocalSemanticProfile(
        model_name=model_name,
        revision=snapshot.name,
        snapshot_path=snapshot,
        vector_cache_path=vector_cache,
        query_instruction=instruction,
    )


def open_local_semantic_index(
    profile: LocalSemanticProfile,
    *,
    allow_rebuild: bool,
) -> SQLiteSemanticCandidateIndex:
    """Open the pinned local model lazily; callers choose rebuild authority."""

    from vector_embedder import VectorEmbedder

    embedder = QueryInstructionEmbedder(
        VectorEmbedder(model_name=str(profile.snapshot_path)),
        profile.query_instruction,
    )
    return SQLiteSemanticCandidateIndex(
        profile.vector_cache_path,
        embedder=embedder,
        model_id=profile.model_id,
        allow_rebuild=allow_rebuild,
        source_layers=("event", "cognition", "period"),
    )


__all__ = [
    "DEFAULT_MODEL_NAME",
    "DEFAULT_QUERY_INSTRUCTION",
    "LocalSemanticProfile",
    "QUERY_INSTRUCTION_VERSION",
    "discover_local_semantic_profile",
    "open_local_semantic_index",
]
