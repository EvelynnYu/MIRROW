"""Validated, idempotent bookmark actions for autonomous Wander nodes.

The model receives candidate numbers, not a free-form authority to mutate a
bookmark by arbitrary ID.  This adapter resolves that number against the
prompt-local snapshot and re-checks the authoritative chronicle immediately
before writing/deleting metadata.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any, Iterable, Optional

from .historical_reader import is_real_conversation_message


class BookmarkActionAdapter:
    """Apply at most one ``none/add/remove`` action against bookmark metadata."""

    def __init__(self, chronicle: Any = None, ob_archiver: Any = None):
        self.chronicle = chronicle
        self.ob_archiver = ob_archiver

    def _chronicle(self) -> Any:
        if self.chronicle is None:
            from wander_manager.host_hooks import get_chronicle as get_global_chronicle
            self.chronicle = get_global_chronicle()
        return self.chronicle

    @staticmethod
    def _action(decision: Any) -> dict[str, Any]:
        if not isinstance(decision, dict):
            return {"type": "none"}
        action = decision.get("bookmark_action")
        return action if isinstance(action, dict) else decision

    @staticmethod
    def _candidate(snapshot: dict[str, Any], action: dict[str, Any]) -> Optional[dict[str, Any]]:
        candidates = snapshot.get("candidates") or snapshot.get("action_candidates") or []
        if not isinstance(candidates, list):
            return None
        index = action.get("target_index", action.get("candidate_index"))
        if index is not None:
            try:
                idx = int(index) - 1
            except (TypeError, ValueError):
                return None
            if 0 <= idx < len(candidates) and isinstance(candidates[idx], dict):
                return candidates[idx]
            return None
        # Compatibility with an older prompt contract.  The ID is still
        # accepted only when it belongs to the displayed candidate set.
        requested_id = str(action.get("message_id") or "").strip()
        if requested_id:
            for item in candidates:
                if str(item.get("message_id") or "").strip() == requested_id:
                    return item
        return None

    @staticmethod
    def _source_bucket_id(source_key: str, message_id: str) -> str:
        digest = hashlib.sha256(f"{source_key}\x00{message_id}".encode("utf-8")).hexdigest()[:24]
        return f"wander_bm_{digest}"

    @staticmethod
    def _compact_error(value: Any) -> str:
        return str(value or "unknown_error").replace("\n", " ")[:200]

    def _bookmarks_for_message(self, message_id: str) -> list[dict[str, Any]]:
        chronicle = self._chronicle()
        try:
            rows, _ = chronicle.list_bookmarks(limit=500)
            return [
                row for row in (rows or [])
                if isinstance(row, dict) and str(row.get("original_msg_id") or "") == message_id
            ]
        except Exception:
            # A tiny fake may only implement the existing single-row lookup.
            getter = getattr(chronicle, "get_bookmark_meta_by_msg_id", None)
            row = getter(message_id) if callable(getter) else None
            return [row] if isinstance(row, dict) else []

    def apply(
        self,
        decision: Any,
        snapshot: dict[str, Any],
        *,
        source_key: str,
    ) -> dict[str, Any]:
        """Validate and apply one action, returning compact auditable evidence."""
        action = self._action(decision)
        action_type = str(action.get("type") or action.get("action") or "none").strip().lower()
        evidence: dict[str, Any] = {
            "requested": action_type or "none",
            "status": "none",
            "reason": str(action.get("reason") or "")[:240],
        }
        if action_type in {"", "none", "no_action", "skip"}:
            evidence["requested"] = "none"
            evidence["reason"] = evidence["reason"] or "model_selected_no_action"
            return evidence
        if action_type not in {"add", "remove"}:
            evidence.update(status="rejected", reason="unsupported_action")
            return evidence
        if not source_key:
            evidence.update(status="rejected", reason="missing_stable_source_key")
            return evidence

        candidate = self._candidate(snapshot, action)
        if not candidate:
            evidence.update(status="rejected", reason="target_not_in_snapshot")
            return evidence
        message_id = str(candidate.get("message_id") or "").strip()
        evidence["message_id"] = message_id
        if not message_id:
            evidence.update(status="rejected", reason="target_has_no_reliable_message_id")
            return evidence

        chronicle = self._chronicle()
        try:
            current = chronicle.get_message_by_msg_id(message_id)
            if not is_real_conversation_message(current):
                evidence.update(status="rejected", reason="target_not_current_real_message")
                return evidence
            current_rows = self._bookmarks_for_message(message_id)
            current_by_bucket = {
                str(row.get("bucket_id") or ""): row for row in current_rows
                if row.get("bucket_id")
            }

            if action_type == "add":
                # Both the snapshot and current state must say this is new.
                # Any owner already holding the message wins over stale model
                # output, preventing duplicate AI rows and user-row takeover.
                if candidate.get("collected_by") or candidate.get("bookmark_id"):
                    evidence.update(status="already_bookmarked", reason="snapshot_currently_bookmarked")
                    return evidence
                if current_rows:
                    evidence.update(status="already_bookmarked", reason="current_bookmark_exists")
                    return evidence
                bucket_id = self._source_bucket_id(source_key, message_id)
                existing_bucket = getattr(chronicle, "get_bookmark_meta_by_bucket", None)
                if callable(existing_bucket) and existing_bucket(bucket_id):
                    evidence.update(status="already_applied", bucket_id=bucket_id)
                    return evidence
                add = getattr(chronicle, "add_bookmark", None)
                if not callable(add):
                    evidence.update(status="failed", reason="bookmark_writer_unavailable")
                    return evidence
                bookmark_id = add(
                    bucket_id=bucket_id,
                    original_msg_id=message_id,
                    session_id=str(current.get("session_id") or candidate.get("session_id") or ""),
                    original_timestamp=str(current.get("timestamp") or candidate.get("timestamp") or ""),
                    collected_by="k",
                    content=str(current.get("content") or candidate.get("content") or "")[:5000],
                    role=str(current.get("role") or candidate.get("role") or "assistant"),
                )
                evidence.update(status="applied", bucket_id=bucket_id, bookmark_id=str(bookmark_id or ""))
                return evidence

            # remove: only the displayed AI-owned row is eligible.  A stale
            # snapshot or a user-owned row is an explicit rejection, never a
            # best-effort delete.
            displayed_owner = str(candidate.get("collected_by") or "")
            displayed_bucket = str(candidate.get("bucket_id") or candidate.get("bookmark_id") or "")
            if displayed_owner != "k" or not displayed_bucket:
                evidence.update(status="rejected", reason="only_k_bookmark_can_be_removed")
                return evidence
            current_row = current_by_bucket.get(displayed_bucket)
            if not current_row:
                evidence.update(status="rejected", reason="bookmark_no_longer_current")
                return evidence
            if str(current_row.get("collected_by") or "") != "k":
                evidence.update(status="rejected", reason="user_bookmark_protected")
                return evidence
            delete = getattr(chronicle, "delete_bookmark_meta", None)
            if not callable(delete) or not delete(displayed_bucket):
                evidence.update(status="failed", reason="bookmark_delete_failed", bucket_id=displayed_bucket)
                return evidence
            evidence.update(status="applied", bucket_id=displayed_bucket)
            return evidence
        except Exception as exc:
            evidence.update(status="failed", reason=self._compact_error(exc))
            return evidence

    async def apply_async(
        self,
        decision: Any,
        snapshot: dict[str, Any],
        *,
        source_key: str,
    ) -> dict[str, Any]:
        """Apply metadata synchronously, then archive an OB_Rev bucket if needed.

        ``bookmarks`` is the local metadata authority used by existing routes;
        OB_Rev archiving is an asynchronous companion and never turns a
        successful metadata deletion into a fabricated success when it fails.
        """
        evidence = self.apply(decision, snapshot, source_key=source_key)
        if evidence.get("status") != "applied" or evidence.get("requested") != "remove":
            return evidence
        archiver = self.ob_archiver
        if archiver is None:
            return evidence
        try:
            archive = getattr(archiver, "archive", None)
            if callable(archive):
                value = archive(evidence.get("bucket_id", ""))
            else:
                update = getattr(archiver, "update", None)
                if not callable(update):
                    return evidence
                value = update(evidence.get("bucket_id", ""), status="archived")
            if inspect.isawaitable(value):
                await value
        except Exception:
            # SQLite metadata remains truthful; expose the secondary archive
            # failure to the caller/audit instead of hiding it.
            evidence["ob_archive_error"] = "archive_failed"
        return evidence
