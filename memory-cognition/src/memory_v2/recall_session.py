"""Small process-local cache for repeated recall inside one chat turn."""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence


_NORMALIZE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", str(text or "").casefold())


def _bigrams(text: str) -> set[str]:
    return {text[index : index + 2] for index in range(max(0, len(text) - 1))}


def _near_match(left: str, right: str) -> bool:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 6 and shorter in longer and len(shorter) / len(longer) >= 0.65:
        return True
    left_pairs, right_pairs = _bigrams(a), _bigrams(b)
    union = left_pairs | right_pairs
    return bool(union) and len(left_pairs & right_pairs) / len(union) >= 0.82


@dataclass(frozen=True)
class RecallSessionEntry:
    query_text: str
    execution: Any
    limit: int
    source_detail_requested: bool
    origin: str


class RecallSessionCache:
    def __init__(self, *, max_turns: int = 256, max_entries_per_turn: int = 8):
        self.max_turns = int(max_turns)
        self.max_entries_per_turn = int(max_entries_per_turn)
        self._lock = threading.Lock()
        self._turns: OrderedDict[str, list[RecallSessionEntry]] = OrderedDict()

    @staticmethod
    def _keys(values: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))

    def lookup(
        self,
        turn_keys: Sequence[str],
        *,
        query_text: str,
        limit: int,
        source_detail_requested: bool,
    ) -> RecallSessionEntry | None:
        keys = self._keys(turn_keys)
        with self._lock:
            for key in keys:
                entries = self._turns.get(key, ())
                for entry in reversed(entries):
                    if entry.limit < int(limit):
                        continue
                    if entry.source_detail_requested != bool(source_detail_requested):
                        continue
                    if _near_match(entry.query_text, query_text):
                        self._turns.move_to_end(key)
                        return entry
        return None

    def store(
        self,
        turn_keys: Sequence[str],
        *,
        query_text: str,
        execution: Any,
        limit: int,
        source_detail_requested: bool,
        origin: str,
    ) -> None:
        entry = RecallSessionEntry(
            query_text=str(query_text or ""),
            execution=execution,
            limit=int(limit),
            source_detail_requested=bool(source_detail_requested),
            origin=str(origin or "recall"),
        )
        keys = self._keys(turn_keys)
        if not keys:
            return
        with self._lock:
            for key in keys:
                entries = self._turns.setdefault(key, [])
                entries.append(entry)
                del entries[: max(0, len(entries) - self.max_entries_per_turn)]
                self._turns.move_to_end(key)
            while len(self._turns) > self.max_turns:
                self._turns.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()


_cache = RecallSessionCache()


def get_recall_session_cache() -> RecallSessionCache:
    return _cache


__all__ = [
    "RecallSessionCache",
    "RecallSessionEntry",
    "get_recall_session_cache",
]
