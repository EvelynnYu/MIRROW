"""Process-local idempotency guard for one accepted private-chat turn.

The authoritative identity is ``session_id + user_message_id``.  Text is only
used as a short-lived compatibility fallback for legacy clients that do not
send a message ID.  No message body is retained in the guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import threading
import time
from typing import Callable, Optional


@dataclass
class _TurnRecord:
    session_id: str
    updated_at: float
    completed: bool
    stable_identity: bool


class ChatTurnGuard:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        stable_ttl_seconds: float = 600.0,
        legacy_ttl_seconds: float = 60.0,
    ) -> None:
        self._clock = clock
        self._stable_ttl = stable_ttl_seconds
        self._legacy_ttl = legacy_ttl_seconds
        self._records: dict[str, _TurnRecord] = {}
        # Bounded, body-free terminal receipts survive replay-cache expiry.
        self._terminal: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._mutations: set[str] = set()

    def _running_locked(self, session_id: str) -> bool:
        return any(record.session_id == session_id and not record.completed
                   for record in self._records.values())

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            return self._running_locked(session_id)

    def begin_mutation(self, session_id: str) -> bool:
        """Reserve the gap before replacement acceptance; no generation owns it."""
        with self._lock:
            if session_id in self._mutations or self._running_locked(session_id):
                return False
            self._mutations.add(session_id)
            return True

    def end_mutation(self, session_id: str) -> None:
        with self._lock:
            self._mutations.discard(session_id)

    def is_mutating(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._mutations

    @staticmethod
    def _key(
        session_id: str,
        user_message_id: str,
        content: str,
        fallback_anchor: str = "",
    ) -> tuple[str, bool]:
        session = str(session_id or "_")
        message_id = str(user_message_id or "").strip()
        if message_id:
            return f"chat:{session}:message:{message_id}", True
        material = f"{content or ''}\n{fallback_anchor or ''}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"chat:{session}:legacy:{digest}", False

    def _prune(self, now: float) -> None:
        expired = []
        for key, record in self._records.items():
            if record.completed:
                ttl = self._stable_ttl if record.stable_identity else self._legacy_ttl
            else:
                # Long tool rounds remain owned until their task really ends.
                continue
            if now - record.updated_at >= ttl:
                expired.append(key)
        for key in expired:
            self._records.pop(key, None)

    def claim(
        self,
        *,
        session_id: str,
        user_message_id: str,
        content: str,
        fallback_anchor: str = "",
    ) -> Optional[str]:
        """Return an opaque claim token, or ``None`` for a replay."""
        key, stable = self._key(
            session_id, user_message_id, content, fallback_anchor,
        )
        now = self._clock()
        with self._lock:
            self._prune(now)
            if session_id in self._mutations:
                return None
            if key in self._records:
                return None
            self._terminal.pop(key, None)
            self._records[key] = _TurnRecord(
                session_id=session_id,
                updated_at=now, completed=False, stable_identity=stable,
            )
        return key

    def status(self, session_id: str, user_message_id: str) -> str:
        """Read generation lifecycle only; unknown is never completion evidence."""
        if not str(user_message_id or "").strip():
            return "unknown"
        key, _ = self._key(session_id, user_message_id, "")
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                return "completed" if record.completed else "running"
            return self._terminal.get(key, "unknown")

    def _remember_terminal(self, token: str, status: str) -> None:
        self._terminal[token] = status
        self._terminal.move_to_end(token)
        while len(self._terminal) > 256:
            self._terminal.popitem(last=False)

    def complete(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            record = self._records.get(token)
            if record is not None:
                record.completed = True
                record.updated_at = self._clock()
                if record.stable_identity:
                    self._remember_terminal(token, "completed")

    def fail(self, token: Optional[str]) -> None:
        """Release a turn that failed before producing an authoritative reply."""
        if not token:
            return
        with self._lock:
            record = self._records.get(token)
            if record is not None and not record.completed:
                self._records.pop(token, None)
                if record.stable_identity:
                    self._remember_terminal(token, "failed")


_GUARD = ChatTurnGuard()


def get_chat_turn_guard() -> ChatTurnGuard:
    return _GUARD
