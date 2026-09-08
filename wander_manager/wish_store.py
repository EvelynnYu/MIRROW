"""许愿 SQLite 存储 — AI 在自省事件中产生的愿望 + 用户的反馈 comment。

替代旧的 data/wish_history.json（已备份并清空）。存储在 events/wishes.db，
与 ledger.db / tasks.db 一致。

表结构 wishes：
    id, feature, reason, status, times_wished,
    first_wished_at, last_wished_at,
    user_comment, comment_updated_at,      ← 用户的反馈（本次新增）
    fulfilled_at, fulfilled_note

去重：rapidfuzz ratio + token_set_ratio 取 max ≥ 75 的同功能不同措辞自动合并（对齐 self_book，中文 partial_ratio 偏低不可用）。
"""

import os
import sqlite3
import threading
import logging
import hashlib
from contextlib import contextmanager
from typing import Iterator, List, Dict, Optional

from mirrow_core.time_utils import now_iso

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "events", "wishes.db"
)


class WishStore:
    """许愿 SQLite CRUD（同步，单用户本地，线程锁保护）。"""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wishes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    times_wished INTEGER DEFAULT 1,
                    first_wished_at TEXT DEFAULT '',
                    last_wished_at TEXT DEFAULT '',
                    user_comment TEXT DEFAULT '',
                    comment_updated_at TEXT DEFAULT '',
                    fulfilled_at TEXT DEFAULT '',
                    fulfilled_note TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wish_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    wish_index INTEGER NOT NULL,
                    wish_hash TEXT NOT NULL,
                    novelty TEXT NOT NULL DEFAULT 'new',
                    wish_id INTEGER REFERENCES wishes(id),
                    outcome TEXT NOT NULL,
                    previous_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    committed_at TEXT NOT NULL,
                    UNIQUE(run_id, activity_id, node_id, wish_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wish_observations_node "
                "ON wish_observations(run_id, activity_id, node_id)"
            )
            conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in row.keys()}

    def list_all(self) -> List[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wishes ORDER BY "
                "CASE status WHEN 'pending' THEN 0 ELSE 1 END, times_wished DESC, id ASC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_by_status(self, status: str) -> List[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wishes WHERE status=? ORDER BY times_wished DESC, id ASC",
                (status,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_by_id(self, wish_id: int) -> Optional[dict]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    @staticmethod
    def _find_pending_match(rows: list[sqlite3.Row], feature: str) -> Optional[sqlite3.Row]:
        """Return the same fuzzy match used by the historical add path."""
        matched = None
        if len(feature) >= 4:
            try:
                from rapidfuzz import fuzz as rfuzz

                best = 0
                for row in rows:
                    existing = row["feature"] or ""
                    if not existing:
                        continue
                    score = max(
                        rfuzz.ratio(feature, existing),
                        rfuzz.token_set_ratio(feature, existing),
                        rfuzz.ratio(existing, feature),
                        rfuzz.token_set_ratio(existing, feature),
                    )
                    if score > best:
                        best, matched = score, row
                if best < 75:
                    matched = None
            except ImportError:
                matched = next(
                    (row for row in rows if (row["feature"] or "").strip() == feature),
                    None,
                )
        else:
            matched = next(
                (row for row in rows if (row["feature"] or "").strip() == feature),
                None,
            )
        return matched

    def _add_or_merge_in_connection(
        self,
        conn: sqlite3.Connection,
        feature: str,
        reason: str,
        now: str,
    ) -> tuple[sqlite3.Row, str, int, int]:
        rows = conn.execute("SELECT * FROM wishes WHERE status='pending'").fetchall()
        matched = self._find_pending_match(rows, feature)
        if matched is not None:
            previous_count = matched["times_wished"] or 1
            new_count = previous_count + 1
            conn.execute(
                "UPDATE wishes SET reason=?, times_wished=?, last_wished_at=? WHERE id=?",
                (reason or matched["reason"], new_count, now, matched["id"]),
            )
            row = conn.execute("SELECT * FROM wishes WHERE id=?", (matched["id"],)).fetchone()
            return row, "merged", previous_count, new_count

        cursor = conn.execute(
            "INSERT INTO wishes (feature, reason, status, times_wished, first_wished_at, last_wished_at) "
            "VALUES (?,?,?,?,?,?)",
            (feature, reason, "pending", 1, now, now),
        )
        row = conn.execute("SELECT * FROM wishes WHERE id=?", (cursor.lastrowid,)).fetchone()
        return row, "created", 0, 1

    def add_or_merge(self, feature: str, reason: str) -> Optional[dict]:
        """新增一条许愿；rapidfuzz≥75 命中已有 pending 愿望则合并。"""
        if not feature or not feature.strip():
            return None
        feature = feature.strip()
        reason = (reason or "").strip()
        now = now_iso()

        with self._lock, self._conn() as conn:
            row, outcome, _previous_count, new_count = self._add_or_merge_in_connection(
                conn, feature, reason, now
            )
            conn.commit()
            logger.info("许愿%s: id=%s, count=%s", outcome, row["id"], new_count)
            return self._row_to_dict(row)

    def commit_reflection_wishes(
        self,
        *,
        run_id: str,
        activity_id: str,
        node_id: str,
        wishes: List[Dict],
    ) -> List[dict]:
        """Atomically commit one reflection node's wishes exactly once.

        Idempotency lives in the same SQLite transaction as ``wishes``. A
        runtime DB audit alone cannot provide this guarantee across crashes.
        ``fulfilled`` observations are recorded but never create or increment a
        wish.
        """
        identities = [str(value or "").strip() for value in (run_id, activity_id, node_id)]
        if not all(identities):
            raise ValueError("run_id, activity_id and node_id are required")
        if not isinstance(wishes, list):
            raise ValueError("wishes must be a list")

        normalized = []
        for index, item in enumerate(wishes):
            if not isinstance(item, dict):
                raise ValueError(f"wish[{index}] must be an object")
            feature = str(item.get("feature") or "").strip()
            if not feature:
                raise ValueError(f"wish[{index}] feature is required")
            reason = str(item.get("reason") or "").strip()
            novelty = str(item.get("novelty") or "new").strip().lower()
            if novelty not in {"new", "reaffirmed", "fulfilled"}:
                novelty = "new"
            source_text = "\0".join((*identities, str(index)))
            source_key = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            wish_hash = hashlib.sha256(feature.casefold().encode("utf-8")).hexdigest()
            normalized.append((index, feature, reason, novelty, source_key, wish_hash))

        results = []
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, feature, reason, novelty, source_key, wish_hash in normalized:
                existing = conn.execute(
                    "SELECT * FROM wish_observations WHERE source_key=?", (source_key,)
                ).fetchone()
                if existing is not None:
                    result = self._row_to_dict(existing)
                    result["idempotent_replay"] = True
                    results.append(result)
                    continue

                committed_at = now_iso()
                if novelty == "fulfilled":
                    cursor = conn.execute(
                        """INSERT INTO wish_observations
                           (source_key, run_id, activity_id, node_id, wish_index, wish_hash,
                            novelty, wish_id, outcome, previous_count, new_count, committed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'skipped_fulfilled', 0, 0, ?)""",
                        (source_key, run_id, activity_id, node_id, index, wish_hash, novelty, committed_at),
                    )
                else:
                    wish, outcome, previous_count, new_count = self._add_or_merge_in_connection(
                        conn, feature, reason, committed_at
                    )
                    cursor = conn.execute(
                        """INSERT INTO wish_observations
                           (source_key, run_id, activity_id, node_id, wish_index, wish_hash,
                            novelty, wish_id, outcome, previous_count, new_count, committed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source_key, run_id, activity_id, node_id, index, wish_hash,
                            novelty, wish["id"], outcome, previous_count, new_count, committed_at,
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM wish_observations WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                result = self._row_to_dict(row)
                result["idempotent_replay"] = False
                results.append(result)
            conn.commit()
        logger.info("自省愿望提交完成: node_id=%s, observations=%s", node_id, len(results))
        return results

    def get_reflection_wish_observations(self, node_id: str) -> List[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wish_observations WHERE node_id=? ORDER BY wish_index", (node_id,)
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def set_comment(self, wish_id: int, comment: str) -> bool:
        """写入用户对某愿望的反馈 comment。"""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE wishes SET user_comment=?, comment_updated_at=? WHERE id=?",
                ((comment or "").strip(), now_iso(), wish_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, wish_id: int) -> bool:
        """旧接口已封死；愿望删除必须保留历史墓碑。"""
        raise RuntimeError("wish deletion is soft-only; use WishStore.set_status(..., 'deleted')")

    def mark_fulfilled(self, feature: str, note: str = "") -> bool:
        feature = (feature or "").strip()
        if not feature:
            return False
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM wishes WHERE status='pending'").fetchall()
            target = None
            for r in rows:
                if (r["feature"] or "").strip() == feature:
                    target = r
                    break
            if target is None and len(feature) >= 4:
                try:
                    from rapidfuzz import fuzz as rfuzz
                    best = 0
                    for r in rows:
                        ex = r["feature"] or ""
                        if not ex:
                            continue
                        score = max(rfuzz.ratio(feature, ex),
                                    rfuzz.token_set_ratio(feature, ex),
                                    rfuzz.ratio(ex, feature),
                                    rfuzz.token_set_ratio(ex, feature))
                        if score > best:
                            best, target = score, r
                    if best < 75:
                        target = None
                except ImportError:
                    pass
            if target is None:
                logger.warning(f"未找到待实现的愿望: 「{feature}」")
                return False
            conn.execute(
                "UPDATE wishes SET status='fulfilled', fulfilled_at=?, fulfilled_note=? WHERE id=?",
                (now_iso(), note or "", target["id"]),
            )
            conn.commit()
            logger.info(f"愿望已标记为已实现: 「{target['feature']}」")
            return True


# 全局单例
_store: Optional[WishStore] = None
_store_lock = threading.Lock()


def get_wish_store() -> WishStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = WishStore()
    return _store


# The board implementation lives in its own module so the compatibility file
# above remains import-safe for older callers.  Re-export it as the canonical
# class/singleton for all current code paths.
from .wish_board_service import WishStore as WishBoardStore  # noqa: E402

WishStore = WishBoardStore
_store = None


def get_wish_store() -> WishBoardStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = WishBoardStore()
    return _store
