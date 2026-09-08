"""Bounded historical conversation reads used by memory and bookmark roaming.

Scene rows are indexes only.  This module deliberately obtains scene material
through ``scene_manager.get_scene_messages`` and obtains random-day material
through ``EventChronicleDB.get_messages_by_date``; neither path treats a scene
summary or a bookmark preview as the original conversation.
"""

from __future__ import annotations

from datetime import datetime
import inspect
import random
from typing import Any, Callable, Iterable, Optional


REAL_ROLES = frozenset({"user", "assistant"})
EXCLUDED_FLAGS = ("is_wander", "is_sentinel", "is_reminder")
DEFAULT_MAX_MESSAGES = 12
DEFAULT_MAX_CHARS = 2400
DEFAULT_DATE_LIMIT = 90


async def load_iceberg_memory_buckets() -> Optional[list[dict]]:
    """Load the archive-inclusive OB_Rev snapshot used by memory recall."""
    try:
        from ombre_brain_client import get_ob_client

        client = get_ob_client()
        initialized = client.ensure_initialized()
        if inspect.isawaitable(initialized):
            await initialized
        list_all = client.bucket_mgr.list_all
        try:
            buckets = list_all(include_archive=True)
        except TypeError:
            buckets = list_all()
        if inspect.isawaitable(buckets):
            buckets = await buckets
        return list(buckets or [])
    except Exception:
        return None


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("message_id") or message.get("id") or "").strip()


def is_real_conversation_message(message: Any) -> bool:
    """Whether a persisted row is eligible as original conversation material."""
    if not isinstance(message, dict):
        return False
    if str(message.get("role") or "") not in REAL_ROLES:
        return False
    if any(message.get(flag) for flag in EXCLUDED_FLAGS):
        return False
    return bool(str(message.get("content") or "").strip())


def _call_with_optional_chronicle(func: Callable, value: Any, chronicle: Any) -> Any:
    """Call a scene/date adapter while keeping tiny test doubles compatible."""
    try:
        return func(value, chronicle=chronicle)
    except TypeError:
        return func(value)


class HistoricalConversationReader:
    """Read one bounded snapshot from a scene anchor or historical day.

    ``chronicle`` and the scene functions are injectable so tests can use an
    isolated temporary database/stub.  No global chronicle is touched until a
    caller explicitly constructs this reader without dependencies.
    """

    def __init__(
        self,
        chronicle: Any = None,
        *,
        scene_picker: Optional[Callable[..., Optional[dict]]] = None,
        scene_message_loader: Optional[Callable[..., list[dict]]] = None,
        memory_buckets: Optional[Iterable[dict]] = None,
        now: Optional[Callable[[], datetime]] = None,
        rng: Any = None,
        min_age_days: int = 3,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
        date_limit: int = DEFAULT_DATE_LIMIT,
    ):
        self.chronicle = chronicle
        self.scene_picker = scene_picker
        self.scene_message_loader = scene_message_loader
        self.memory_buckets = None if memory_buckets is None else list(memory_buckets)
        self.memory_buckets_unavailable = False
        self.now = now or datetime.now
        self.rng = rng or random
        self.min_age_days = max(0, int(min_age_days))
        self.max_messages = max(1, int(max_messages))
        self.max_chars = max(200, int(max_chars))
        self.date_limit = max(1, int(date_limit))

    def _picker(self) -> Callable[..., Optional[dict]]:
        if self.scene_picker is not None:
            return self.scene_picker
        from scene_manager import pick_iceberg_scene
        return pick_iceberg_scene

    def _scene_loader(self) -> Callable[..., list[dict]]:
        if self.scene_message_loader is not None:
            return self.scene_message_loader
        from scene_manager import get_scene_messages
        return get_scene_messages

    def _pick_scene(self, exclude_scene_ids: Iterable[str] = ()) -> Optional[dict]:
        if self.memory_buckets_unavailable:
            return None
        picker = self._picker()
        kwargs = {
            "min_age_days": self.min_age_days,
            "chronicle": self.chronicle,
            "exclude_scene_ids": list(exclude_scene_ids),
        }
        if self.memory_buckets is not None:
            kwargs["memory_buckets"] = self.memory_buckets
        try:
            return picker(**kwargs)
        except TypeError:
            # Narrow compatibility for old injected helpers.  The production
            # picker accepts all of the keyword arguments above.
            try:
                return picker(
                    min_age_days=self.min_age_days,
                    chronicle=self.chronicle,
                    **({"memory_buckets": self.memory_buckets} if self.memory_buckets is not None else {}),
                )
            except TypeError:
                return picker(self.min_age_days)

    def _read_scene(self, scene: dict) -> list[dict]:
        loader = self._scene_loader()
        rows = _call_with_optional_chronicle(loader, scene, self.chronicle)
        return self.bound_snapshot(rows or [], source_date=str(scene.get("active_date") or ""))

    def bound_snapshot(self, rows: Iterable[dict], *, source_date: str = "") -> list[dict]:
        """Normalize and cap original rows for a prompt-local snapshot."""
        result: list[dict] = []
        used_chars = 0
        for row in rows or []:
            if not is_real_conversation_message(row):
                continue
            if len(result) >= self.max_messages or used_chars >= self.max_chars:
                break
            content = str(row.get("content") or "").strip()
            remaining = self.max_chars - used_chars
            # Keep the cap deterministic and leave room for the message ID and
            # role metadata in the structured prompt.
            content = content[: max(1, remaining)]
            timestamp = str(row.get("timestamp") or "").strip()
            active_date = str(
                row.get("active_date") or row.get("calendar_date") or source_date or timestamp[:10]
            )[:10]
            result.append({
                "message_id": _message_id(row),
                "role": str(row.get("role") or ""),
                "timestamp": timestamp,
                "date": active_date,
                "active_date": active_date,
                "session_id": str(row.get("session_id") or ""),
                "content": content,
            })
            used_chars += len(content)
        return result

    @staticmethod
    def candidate_messages(snapshot: Iterable[dict], bookmarked_ids: Iterable[str] = ()) -> list[dict]:
        bookmarked = {str(item).strip() for item in (bookmarked_ids or []) if str(item).strip()}
        return [
            row for row in (snapshot or [])
            if row.get("message_id") and row.get("message_id") not in bookmarked
        ]

    def read_unbookmarked_anchor(
        self,
        bookmarked_ids: Iterable[str] = (),
        *,
        max_attempts: int = 4,
    ) -> Optional[dict[str, Any]]:
        """Pick an iceberg scene and return it only if it has an unbookmarked row."""
        excluded_scenes: set[str] = set()
        for _ in range(max(1, int(max_attempts))):
            scene = self._pick_scene(excluded_scenes)
            if not scene:
                return None
            scene_id = str(scene.get("id") or "")
            if scene_id:
                excluded_scenes.add(scene_id)
            snapshot = self._read_scene(scene)
            candidates = self.candidate_messages(snapshot, bookmarked_ids)
            if not candidates:
                continue
            return {
                "source_mode": "unbookmarked_anchor",
                "scene_id": scene_id,
                "active_date": str(scene.get("active_date") or "")[:10],
                "session_id": str(scene.get("session_id") or ""),
                "snapshot": snapshot,
                "candidates": candidates,
            }
        return None

    def _available_dates(self, today: str) -> list[str]:
        chronicle = self.chronicle
        if chronicle is None:
            return []
        try:
            list_dates = getattr(chronicle, "list_active_dates", None)
            if callable(list_dates):
                dates = list_dates(before_date=today, limit=self.date_limit)
                return [str(item)[:10] for item in (dates or []) if str(item)[:10] < today]
        except TypeError:
            try:
                dates = chronicle.list_active_dates(today, self.date_limit)
                return [str(item)[:10] for item in (dates or []) if str(item)[:10] < today]
            except Exception:
                pass
        except Exception:
            pass

        # Existing chronicle versions expose the same source through the
        # nth-previous helper.  This remains bounded and does not scan every
        # tick; the caller invokes it only when selecting random_day.
        dates: list[str] = []
        getter = getattr(chronicle, "get_nth_previous_active_date", None)
        if callable(getter):
            for index in range(1, self.date_limit + 1):
                try:
                    value = getter(index, before_date=today)
                except TypeError:
                    value = getter(index, today)
                if not value:
                    break
                value = str(value)[:10]
                if value < today and value not in dates:
                    dates.append(value)
        return dates

    def read_random_day(
        self,
        bookmarked_ids: Iterable[str] = (),
        *,
        today: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Read one random historical active_date, excluding today."""
        current_date = str(today or self.now().strftime("%Y-%m-%d"))[:10]
        dates = self._available_dates(current_date)
        if not dates:
            return None
        # Shuffle a copy so an empty/invalid day does not make the mode look
        # unavailable when another eligible date exists.
        dates = list(dates)
        try:
            self.rng.shuffle(dates)
        except Exception:
            dates.sort()
        get_by_date = getattr(self.chronicle, "get_messages_by_date", None)
        if not callable(get_by_date):
            return None
        for active_date in dates:
            try:
                rows = get_by_date(active_date) or []
            except Exception:
                continue
            snapshot = self.bound_snapshot(rows, source_date=active_date)
            if not snapshot:
                continue
            candidates = self.candidate_messages(snapshot, bookmarked_ids)
            return {
                "source_mode": "random_day",
                "active_date": active_date,
                "scene_id": "",
                "session_id": str(snapshot[0].get("session_id") or ""),
                "snapshot": snapshot,
                "candidates": candidates,
            }
        return None
