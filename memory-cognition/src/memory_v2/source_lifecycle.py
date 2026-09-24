"""Reconcile Memory V2 read models after authoritative message mutation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from .context_shadow import (
    quarantine_context_shadow_sources,
    refresh_context_shadow_for_source_mutation,
)
from .conversation_source import ConversationSource
from .store import MemoryV2Store


_REASON_CODES = {
    "edit": "source_message_edited",
    "delete": "source_message_deleted",
    "revoke": "source_message_revoked",
    "clear": "source_session_cleared",
}


def configured_memory_v2_paths() -> tuple[Path, ...]:
    """Resolve configured production databases without creating new files."""

    raw = (
        os.environ.get("MIRROW_MEMORY_V2_DBS", "").strip()
        or os.environ.get("MIRROW_MEMORY_V2_SHADOW_DBS", "").strip()
    )
    return tuple(
        dict.fromkeys(
            Path(value).resolve()
            for value in raw.split(os.pathsep)
            if value.strip()
        )
    )


def _authority_path() -> Path:
    backend_root = Path(__file__).resolve().parents[1]
    return Path(
        os.environ.get(
            "MIRROW_MEMORY_V2_AUTHORITY_DB",
            str(backend_root / "events" / "event_chronicle.db"),
        )
    ).resolve()


def source_mutation_revision(
    mutation: str,
    message_ids: Sequence[str],
    *,
    revision_material: str = "",
) -> str:
    """Return a content-free stable identity for one authoritative mutation."""

    normalized = sorted(
        {
            str(message_id).strip()
            for message_id in message_ids
            if str(message_id).strip()
        }
    )
    payload = "\0".join(
        (
            str(mutation).strip().casefold(),
            *normalized,
            hashlib.sha256(str(revision_material).encode("utf-8")).hexdigest()
            if revision_material
            else "",
        )
    )
    return "message_mutation:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reconcile_memory_v2_source_mutation(
    message_ids: Sequence[str],
    *,
    mutation: str,
    revision_material: str = "",
) -> dict[str, object]:
    """Invalidate cited events and advance the online recall read model.

    The caller invokes this only after the conversation authority mutation has
    succeeded.  Any failure leaves the source IDs in a process-local recall
    quarantine so an already materialized index cannot surface ghost content.
    Pending rebuild days are represented durably by events whose latest status
    is ``invalid_source``; no second mutable repair queue is introduced.
    """

    normalized = tuple(
        dict.fromkeys(
            str(message_id).strip()
            for message_id in message_ids
            if str(message_id).strip()
        )
    )
    if not normalized:
        return {
            "status": "no_sources",
            "source_count": 0,
            "invalidated_event_count": 0,
            "preserved_event_count": 0,
            "pending_active_dates": [],
            "refresh_status": "not_requested",
        }
    mutation = str(mutation or "").strip().casefold()
    if mutation not in _REASON_CODES:
        raise ValueError("unsupported Memory V2 source mutation")
    memory_paths = configured_memory_v2_paths()
    if not memory_paths:
        return {
            "status": "not_configured",
            "source_count": len(normalized),
            "invalidated_event_count": 0,
            "preserved_event_count": 0,
            "pending_active_dates": [],
            "refresh_status": "not_requested",
        }

    revision = source_mutation_revision(
        mutation,
        normalized,
        revision_material=revision_material,
    )
    quarantine_context_shadow_sources(normalized)
    event_ids: set[str] = set()
    preserved_event_ids: set[str] = set()
    active_dates: set[str] = set()
    try:
        authority_path = _authority_path()
        try:
            surviving_message_ids = (
                ConversationSource(authority_path).list_message_ids()
                if authority_path.is_file()
                else ()
            )
        except Exception:
            # Duplicate protection is optional.  If the read-only authority
            # scan fails, fall back to conservative event invalidation.
            surviving_message_ids = ()
        for path in memory_paths:
            if not path.is_file():
                raise FileNotFoundError("configured Memory V2 database is missing")
            result = MemoryV2Store(path, initialise=False).invalidate_events_by_source_message_ids(
                normalized,
                reason_code=_REASON_CODES[mutation],
                source_revision=revision,
                surviving_message_ids=surviving_message_ids,
            )
            event_ids.update(str(item) for item in result["event_ids"])
            preserved_event_ids.update(
                str(item) for item in result["preserved_event_ids"]
            )
            active_dates.update(str(item) for item in result["active_dates"])
    except Exception as exc:
        return {
            "status": "error",
            "source_count": len(normalized),
            "invalidated_event_count": len(event_ids),
            "preserved_event_count": len(preserved_event_ids),
            "pending_active_dates": sorted(active_dates),
            "refresh_status": "quarantined",
            "error_type": type(exc).__name__,
        }

    try:
        refresh = refresh_context_shadow_for_source_mutation(
            (*memory_paths, _authority_path()),
            normalized,
        )
    except Exception as exc:
        return {
            "status": "reconciled_refresh_error",
            "source_count": len(normalized),
            "invalidated_event_count": len(event_ids),
            "preserved_event_count": len(preserved_event_ids),
            "pending_active_dates": sorted(active_dates),
            "refresh_status": "quarantined",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "reconciled",
        "source_count": len(normalized),
        "invalidated_event_count": len(event_ids),
        "preserved_event_count": len(preserved_event_ids),
        "pending_active_dates": sorted(active_dates),
        "refresh_status": str(refresh.get("status") or "unknown"),
    }


__all__ = [
    "configured_memory_v2_paths",
    "reconcile_memory_v2_source_mutation",
    "source_mutation_revision",
]
