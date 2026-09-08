"""Local wish-board domain service.

The board keeps a current wish snapshot in ``wishes``, a flat conversation in
``wish_comments`` and an append-only history in ``wish_events``.  The existing
``wish_observations`` table is retained for v3 audit compatibility, while new
self-reflection writes use stable wish IDs and the structured action contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

from mirrow_core.time_utils import now_iso

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "events", "wishes.db")
WISH_STATUSES = ("open", "in_progress", "impossible_pending", "impossible_kept", "fulfilled", "deleted")
_STATUS_ALIASES = {"pending": "open"}
_ACTION_TYPES = {"none", "create", "reaffirm", "retain_impossible", "delete_impossible"}


class WishMigrationRequired(RuntimeError):
    """Raised when a write needs the explicit wish-board schema migration."""


def canonical_status(status: str) -> str:
    value = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(value, value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def normalise_feature(value: str) -> str:
    """Normalize only obvious decorations for exact matching; no fuzzy match."""
    text = str(value or "").strip().casefold()
    text = re.sub(r"[（(](?:新想法|之前提过但还没有|重复|再次)[）)]", "", text)
    text = re.sub(r"[\s\u3000]+", "", text)
    return re.sub(r"[，。！？、；：:,.!?;\-—_]+", "", text)


def normalise_reaffirm_basis(value: str) -> str:
    """Normalize only stable formatting differences for objective evidence.

    This is intentionally not a semantic or sentiment matcher.  It removes
    whitespace/common punctuation and makes ``愿望[16]``/``愿望16`` compare
    equally, while leaving the factual words themselves untouched.
    """
    text = str(value or "").strip().casefold()
    text = re.sub(r"愿望\s*[\[\(（【]?\s*(\d+)\s*[\]\)）】]?", r"愿望\1", text)
    text = re.sub(r"[\s\u3000]+", "", text)
    return re.sub(r"[，。！？、；：:,.!?;…·\-—_]+", "", text)


def _age_seconds(written_at: str, *, now: Optional[str] = None) -> int:
    """Return a conservative non-negative age for a stored ISO timestamp."""
    try:
        current = datetime.fromisoformat(str(now or now_iso()).replace("Z", "+00:00"))
        written = datetime.fromisoformat(str(written_at or "").replace("Z", "+00:00"))
        if current.tzinfo is not None and written.tzinfo is None:
            written = written.replace(tzinfo=current.tzinfo)
        elif current.tzinfo is None and written.tzinfo is not None:
            current = current.replace(tzinfo=written.tzinfo)
        return max(0, int((current - written).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return 0


class WishStore:
    """Synchronous SQLite service for the single local MIRROW instance."""

    def __init__(self, db_path: str = _DEFAULT_DB, *, migrate: bool = False):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._schema_state = "unknown"
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db(migrate=migrate)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
        ).fetchone() is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}

    @classmethod
    def _is_current_schema(cls, conn: sqlite3.Connection) -> bool:
        required_columns = {
            "id", "feature", "reason", "status", "times_wished", "first_wished_at",
            "last_wished_at", "user_comment", "comment_updated_at", "fulfilled_at",
            "fulfilled_note", "updated_at", "deleted_at", "merged_into_id",
        }
        return (
            required_columns.issubset(cls._table_columns(conn, "wishes"))
            and cls._table_exists(conn, "wish_comments")
            and cls._table_exists(conn, "wish_events")
            and cls._table_exists(conn, "wish_observations")
            and cls._has_current_role_schema(conn)
        )

    @staticmethod
    def _has_current_role_schema(conn: sqlite3.Connection) -> bool:
        for table in ("wish_comments", "wish_events"):
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not row or "'user'" not in str(row[0] or ""):
                return False
        return True

    @staticmethod
    def _has_legacy_role_schema(conn: sqlite3.Connection) -> bool:
        """Whether an existing role table needs a supplied role mapping."""
        for table in ("wish_comments", "wish_events"):
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row and "'user'" not in str(row[0] or ""):
                return True
        return False

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
                CREATE TABLE IF NOT EXISTS wishes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'open',
                    times_wished INTEGER DEFAULT 1,
                    first_wished_at TEXT DEFAULT '',
                    last_wished_at TEXT DEFAULT '',
                    user_comment TEXT DEFAULT '',
                    comment_updated_at TEXT DEFAULT '',
                    fulfilled_at TEXT DEFAULT '',
                    fulfilled_note TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    deleted_at TEXT DEFAULT '',
                    merged_into_id INTEGER
                )
            """)
        conn.execute("""
                CREATE TABLE IF NOT EXISTS wish_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wish_id INTEGER NOT NULL REFERENCES wishes(id),
                    author TEXT NOT NULL CHECK(author IN ('k','user','system')),
                    content TEXT NOT NULL,
                    reply_to_comment_id INTEGER,
                    source_key TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
        conn.execute("""
                CREATE TABLE IF NOT EXISTS wish_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wish_id INTEGER REFERENCES wishes(id),
                    actor TEXT NOT NULL CHECK(actor IN ('k','user','system','migration')),
                    event_type TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    source_key TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_comments_wish ON wish_comments(wish_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_events_wish ON wish_events(wish_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_events_type ON wish_events(event_type, created_at)")
        conn.execute("""
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
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_observations_node ON wish_observations(run_id, activity_id, node_id)")

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Apply the destructive-boundary migration only after explicit opt-in."""
        self._create_schema(conn)
        existing = self._table_columns(conn, "wishes")
        for col, kind in (("updated_at", "TEXT DEFAULT ''"), ("deleted_at", "TEXT DEFAULT ''"), ("merged_into_id", "INTEGER")):
            if col not in existing:
                conn.execute(f"ALTER TABLE wishes ADD COLUMN {col} {kind}")
        # ``pending`` is an old spelling.  This is intentionally only in the
        # explicit migration path; normal construction maps it at read time.
        conn.execute("UPDATE wishes SET status='open' WHERE status='pending'")
        conn.execute("UPDATE wishes SET updated_at=COALESCE(NULLIF(updated_at,''), last_wished_at, first_wished_at) WHERE updated_at IS NULL OR updated_at=''")

    @staticmethod
    def _migrate_legacy_user_role(conn: sqlite3.Connection) -> None:
        """Migrate a host-supplied former user key without bundling it here.

        This must only run through the explicit ``migrate=True`` construction
        path.  It leaves every unrelated role untouched.
        """
        legacy = os.getenv("MIRROW_LEGACY_USER_ROLE", "").strip()
        if not legacy or legacy == "user":
            return
        if legacy in {"k", "system", "migration"}:
            raise WishMigrationRequired("旧用户角色映射不能使用保留系统角色")
        for table, role_column, ddl, columns in (
            ("wish_comments", "author", """
                CREATE TABLE wish_comments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wish_id INTEGER NOT NULL REFERENCES wishes(id),
                    author TEXT NOT NULL CHECK(author IN ('k','user','system')),
                    content TEXT NOT NULL, reply_to_comment_id INTEGER,
                    source_key TEXT UNIQUE, created_at TEXT NOT NULL
                )""", "id,wish_id,author,content,reply_to_comment_id,source_key,created_at"),
            ("wish_events", "actor", """
                CREATE TABLE wish_events_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wish_id INTEGER REFERENCES wishes(id),
                    actor TEXT NOT NULL CHECK(actor IN ('k','user','system','migration')),
                    event_type TEXT NOT NULL, content TEXT DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}', source_key TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )""", "id,wish_id,actor,event_type,content,payload_json,source_key,created_at"),
        ):
            if not WishStore._table_exists(conn, table):
                continue
            schema = str(conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] or "")
            if "'user'" in schema:
                conn.execute(f"UPDATE {table} SET {role_column}='user' WHERE {role_column}=?", (legacy,))
                continue
            conn.execute(ddl)
            selected = columns.replace(
                role_column, f"CASE WHEN {role_column}=? THEN 'user' ELSE {role_column} END"
            )
            conn.execute(
                f"INSERT INTO {table}_new ({columns}) SELECT {selected} FROM {table}", (legacy,)
            )
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_comments_wish ON wish_comments(wish_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_events_wish ON wish_events(wish_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wish_events_type ON wish_events(event_type, created_at)")

    def _init_db(self, *, migrate: bool = False) -> None:
        existed = os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0
        with self._lock, self._conn() as conn:
            has_wishes = self._table_exists(conn, "wishes")
            has_any_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone() is not None
            if not has_any_tables or (not existed and not has_wishes):
                self._create_schema(conn)
                conn.commit()
                self._schema_state = "current"
                return
            if migrate and not self._is_current_schema(conn):
                if self._has_legacy_role_schema(conn) and not os.getenv("MIRROW_LEGACY_USER_ROLE", "").strip():
                    raise WishMigrationRequired("旧角色 schema 需要先由宿主提供 MIRROW_LEGACY_USER_ROLE 再显式迁移")
                conn.execute("BEGIN")
                self._migrate_schema(conn)
            if migrate:
                self._migrate_legacy_user_role(conn)
                if not self._is_current_schema(conn):
                    raise WishMigrationRequired("许愿板 schema 迁移未完成")
                conn.commit()
                self._schema_state = "current"
                return
            self._schema_state = "current" if self._is_current_schema(conn) else "legacy_readonly"

    def migrate(self) -> bool:
        """Explicitly migrate an existing legacy database in one transaction."""
        with self._lock, self._conn() as conn:
            if self._is_current_schema(conn):
                self._schema_state = "current"
                return False
            if self._has_legacy_role_schema(conn) and not os.getenv("MIRROW_LEGACY_USER_ROLE", "").strip():
                raise WishMigrationRequired("旧角色 schema 需要先由宿主提供 MIRROW_LEGACY_USER_ROLE 再显式迁移")
            conn.execute("BEGIN")
            self._migrate_schema(conn)
            self._migrate_legacy_user_role(conn)
            if not self._is_current_schema(conn):
                raise WishMigrationRequired("许愿板 schema 迁移未完成")
            conn.commit()
            self._schema_state = "current"
            return True

    def _ensure_writable(self) -> None:
        if self._schema_state != "current":
            raise WishMigrationRequired(
                "许愿板数据库仍是旧 schema；当前仅提供只读兼容，请先显式执行 WishStore.migrate()"
            )

    @staticmethod
    def _dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        return {key: row[key] for key in row.keys()} if row else None

    def _normalise_read_wish(self, wish: Optional[dict]) -> Optional[dict]:
        """Keep legacy rows readable without changing their stored status."""
        if wish is None:
            return None
        for key, default in (
            ("updated_at", ""), ("deleted_at", ""), ("merged_into_id", None),
        ):
            wish.setdefault(key, default)
        wish["status"] = canonical_status(wish.get("status", "open"))
        return wish

    def _comments(self, conn: sqlite3.Connection, wish_id: int, limit: Optional[int] = 2) -> list[dict]:
        if not self._table_exists(conn, "wish_comments"):
            return []
        if limit is None:
            rows = conn.execute("SELECT * FROM wish_comments WHERE wish_id=? ORDER BY created_at, id", (wish_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM wish_comments WHERE wish_id=? ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at, id",
                (wish_id, max(0, int(limit))),
            ).fetchall()
        return [self._dict(row) for row in rows]

    def _pending_reply_candidate(self, conn: sqlite3.Connection, wish_id: int) -> Optional[dict]:
        """Return the newest User comment as a convenience, not a gate.

        Historical User comments remain replyable.  The model receives their
        real timestamps and any direct AI-reply timestamps and decides whether
        an older comment merits a supplement.
        """
        if not self._table_exists(conn, "wish_comments"):
            return None
        row = conn.execute(
            """
            SELECT c.*
            FROM wish_comments c
            WHERE c.wish_id=?
              AND c.author='user'
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT 1
            """,
            (int(wish_id),),
        ).fetchone()
        if row is None:
            return None
        written_at = str(row["created_at"] or "")
        age = _age_seconds(written_at)
        return {
            "comment_id": int(row["id"]),
            "wish_id": int(row["wish_id"]),
            "content": str(row["content"] or ""),
            "written_at": written_at,
            "age": age,
            "age_seconds": age,
            "replyable": True,
        }

    @staticmethod
    def _mark_comment_history(comments: list[dict], candidate: Optional[dict]) -> list[dict]:
        candidate_id = candidate.get("comment_id") if candidate else None
        direct_replies: dict[int, list[dict]] = {}
        for comment in comments:
            if str(comment.get("author") or "") != "k":
                continue
            try:
                target_id = int(comment.get("reply_to_comment_id"))
            except (TypeError, ValueError):
                continue
            direct_replies.setdefault(target_id, []).append({
                "comment_id": int(comment.get("id") or 0),
                "written_at": str(comment.get("created_at") or ""),
            })
        for comment in comments:
            is_candidate = candidate_id is not None and int(comment.get("id") or 0) == int(candidate_id)
            comment["reply_candidate"] = bool(is_candidate)
            comment["replyable"] = str(comment.get("author") or "") == "user"
            comment["k_reply_history"] = direct_replies.get(int(comment.get("id") or 0), [])
        return comments

    def _decorate(self, conn: sqlite3.Connection, wish: dict) -> dict:
        latest = None
        if self._table_exists(conn, "wish_comments"):
            latest = conn.execute(
                "SELECT content, created_at FROM wish_comments WHERE wish_id=? AND author='user' ORDER BY created_at DESC, id DESC LIMIT 1",
                (wish["id"],),
            ).fetchone()
        if latest:
            wish["user_comment"] = latest["content"]
            wish["comment_updated_at"] = latest["created_at"]
        self._normalise_read_wish(wish)
        candidate = self._pending_reply_candidate(conn, int(wish["id"]))
        wish["pending_reply_candidate"] = candidate
        wish["latest_comments"] = self._mark_comment_history(self._comments(conn, wish["id"], 2), candidate)
        wish["all_comments"] = self._mark_comment_history(self._comments(conn, wish["id"], None), candidate)
        wish["reaffirm_basis_history"] = [
            {
                "basis": str(row["content"] or ""),
                "written_at": str(row["created_at"] or ""),
            }
            for row in conn.execute(
                "SELECT content, created_at FROM wish_events "
                "WHERE wish_id=? AND event_type='reaffirmed' "
                "ORDER BY created_at DESC, id DESC LIMIT 5",
                (int(wish["id"]),),
            ).fetchall()
        ] if self._table_exists(conn, "wish_events") else []
        return wish

    def list_all(self, *, include_threads: bool = False) -> List[dict]:
        order = ("CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'impossible_pending' THEN 2 "
                 "WHEN 'impossible_kept' THEN 3 WHEN 'fulfilled' THEN 4 WHEN 'deleted' THEN 5 ELSE 6 END, "
                 "times_wished DESC, id ASC")
        with self._lock, self._conn() as conn:
            result = [self._normalise_read_wish(self._dict(row)) for row in conn.execute(f"SELECT * FROM wishes ORDER BY {order}").fetchall()]
            for wish in result:
                if include_threads:
                    self._decorate(conn, wish)
            return result

    def get_by_status(self, status: str, *, include_threads: bool = False) -> List[dict]:
        status = canonical_status(status)
        with self._lock, self._conn() as conn:
            if status == "open":
                rows = conn.execute("SELECT * FROM wishes WHERE status IN ('open','pending') ORDER BY times_wished DESC, id ASC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM wishes WHERE status=? ORDER BY times_wished DESC, id ASC", (status,)).fetchall()
            result = [self._normalise_read_wish(self._dict(row)) for row in rows]
            for wish in result:
                if include_threads:
                    self._decorate(conn, wish)
            return result

    def get_by_id(self, wish_id: int, *, include_threads: bool = False) -> Optional[dict]:
        with self._lock, self._conn() as conn:
            wish = self._normalise_read_wish(self._dict(conn.execute("SELECT * FROM wishes WHERE id=?", (int(wish_id),)).fetchone()))
            if wish:
                if include_threads:
                    self._decorate(conn, wish)
            return wish

    def get_board(self) -> dict:
        with self._lock, self._conn() as conn:
            wishes = []
            for row in conn.execute("SELECT * FROM wishes ORDER BY last_wished_at DESC, id DESC").fetchall():
                wish = self._decorate(conn, self._dict(row))
                wish["history"] = [self._dict(item) for item in conn.execute(
                    "SELECT * FROM wish_events WHERE wish_id=? ORDER BY created_at, id", (wish["id"],)
                ).fetchall()] if self._table_exists(conn, "wish_events") else []
                wishes.append(wish)
            return {"wishes": wishes, "statuses": list(WISH_STATUSES), "updated_at": now_iso()}

    def get_context_for_reflection(self) -> str:
        board = self.get_board()
        if not board["wishes"]:
            return "（许愿板目前没有记录。）"
        labels = {
            "open": "尚未标记", "in_progress": "用户正在努力实现",
            "impossible_pending": "用户标记为天方夜谭，等待你的决定",
            "impossible_kept": "你保留的天方夜谭愿望", "fulfilled": "已实现历史", "deleted": "删除记录（墓碑）",
        }
        snapshot_at = board.get("updated_at") or now_iso()
        lines = [
            f"以下信息来自持久化许愿板，快照生成于 {snapshot_at}。",
            "愿望状态反映快照生成时的记录；评论行标注留言作者与写入时间。每条愿望的数字是稳定 wish_id：",
        ]
        for status, label in labels.items():
            selected = [w for w in board["wishes"] if w.get("status") == status]
            if not selected:
                continue
            lines.append(f"\n【{label}】")
            for wish in selected:
                line = f"  [{wish['id']}] {wish.get('feature','')}（许愿{int(wish.get('times_wished') or 0)}次）"
                if wish.get("reason"):
                    line += f"；最近理由：{wish['reason']}"
                lines.append(line)
                for comment in wish.get("all_comments") or []:
                    author = {"user": "用户", "k": "AI", "system": "系统"}.get(
                        str(comment.get("author") or ""), str(comment.get("author") or "未知")
                    )
                    marker = "【最新用户留言】" if comment.get("reply_candidate") else (
                        "【可补充回复的历史留言】" if comment.get("replyable") else "【AI 的历史回复】"
                    )
                    reply_times = "、".join(
                        str(item.get("written_at") or "未记录时间")
                        for item in comment.get("k_reply_history") or []
                    ) or "无直接回复记录"
                    lines.append(
                        f"      历史留言 {marker} "
                        f"[comment_id={comment.get('id')}, author={author}, written_at={comment.get('created_at') or '未记录'}]："
                        f"{comment.get('content')}；AI直接回复时间={reply_times}"
                    )
                candidate = wish.get("pending_reply_candidate")
                if candidate:
                    lines.append(
                        "      待 AI 回复候选："
                        f"comment_id={candidate.get('comment_id')}，written_at={candidate.get('written_at') or '未记录时间'}，"
                        f"age={candidate.get('age', 0)}秒；它只是最新留言，历史用户留言也可按真实时间自由选择补充回复。"
                    )
                else:
                    lines.append("      最新用户留言：无。")
                basis_history = wish.get("reaffirm_basis_history") or []
                if basis_history:
                    lines.append("      最近 reaffirm 依据（仅客观历史，规范化后完全相同不重复计数）：")
                    for item in basis_history:
                        lines.append(
                            f"        [{item.get('written_at') or '未记录时间'}] {item.get('basis') or '（空）'}"
                        )
        return "\n".join(lines)

    def _find_exact(self, conn: sqlite3.Connection, feature: str, *, include_deleted: bool = False) -> Optional[sqlite3.Row]:
        key = normalise_feature(feature)
        if not key:
            return None
        query = "SELECT * FROM wishes ORDER BY id" if include_deleted else "SELECT * FROM wishes WHERE status != 'deleted' ORDER BY id"
        return next((r for r in conn.execute(query).fetchall()
                     if normalise_feature(r["feature"]) == key), None)

    def _event(self, conn: sqlite3.Connection, *, wish_id: Optional[int], actor: str, event_type: str,
               content: str = "", payload: Optional[dict] = None, source_key: Optional[str] = None,
               created_at: Optional[str] = None) -> dict:
        created = created_at or now_iso()
        try:
            cur = conn.execute(
                "INSERT INTO wish_events (wish_id,actor,event_type,content,payload_json,source_key,created_at) VALUES (?,?,?,?,?,?,?)",
                (wish_id, actor, event_type, content or "", _json(payload or {}), source_key, created),
            )
            return {"id": cur.lastrowid, "wish_id": wish_id, "actor": actor, "event_type": event_type,
                    "content": content or "", "payload": payload or {}, "source_key": source_key,
                    "created_at": created, "mutation": True}
        except sqlite3.IntegrityError:
            if source_key:
                row = conn.execute("SELECT * FROM wish_events WHERE source_key=?", (source_key,)).fetchone()
                if row:
                    item = self._dict(row)
                    try:
                        item["payload"] = json.loads(item.get("payload_json") or "{}")
                    except Exception:
                        item["payload"] = {}
                    item.update({"mutation": False, "idempotent_replay": True})
                    return item
            raise

    def _comment(self, conn: sqlite3.Connection, *, wish_id: int, author: str, content: str,
                 reply_to_comment_id: Optional[int] = None, source_key: Optional[str] = None,
                 created_at: Optional[str] = None) -> dict:
        content = str(content or "").strip()
        if not content:
            raise ValueError("comment content is required")
        if author not in {"k", "user", "system"}:
            raise ValueError("author must be k, user or system")
        created = created_at or now_iso()
        try:
            cur = conn.execute(
                "INSERT INTO wish_comments (wish_id,author,content,reply_to_comment_id,source_key,created_at) VALUES (?,?,?,?,?,?)",
                (wish_id, author, content, reply_to_comment_id, source_key, created),
            )
            return {"id": cur.lastrowid, "wish_id": wish_id, "author": author, "content": content,
                    "reply_to_comment_id": reply_to_comment_id, "source_key": source_key,
                    "created_at": created, "mutation": True}
        except sqlite3.IntegrityError:
            if source_key:
                row = conn.execute("SELECT * FROM wish_comments WHERE source_key=?", (source_key,)).fetchone()
                if row:
                    item = self._dict(row)
                    item.update({"mutation": False, "idempotent_replay": True})
                    return item
            raise

    def add_or_merge(self, feature: str, reason: str) -> Optional[dict]:
        """Legacy helper; exact-normalized matching only (no fuzzy guessing)."""
        self._ensure_writable()
        feature, reason = str(feature or "").strip(), str(reason or "").strip()
        if not feature:
            return None
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = now_iso()
            row = self._find_exact(conn, feature, include_deleted=True)
            row_status = canonical_status(row["status"]) if row is not None else ""
            if row is not None and row_status in {"open", "in_progress", "impossible_kept"}:
                before, wish_id, outcome = int(row["times_wished"] or 1), int(row["id"]), "merged"
                after = before + 1
                conn.execute("UPDATE wishes SET reason=COALESCE(NULLIF(?,''),reason), times_wished=?, last_wished_at=?, updated_at=? WHERE id=?", (reason, after, now, now, wish_id))
            elif row is not None:
                # Fulfilled, impossible-pending and deleted rows are history,
                # not invitations to create a second identity.  The legacy
                # caller can display this result without mutating the board.
                result = self._dict(row)
                result.update({"status": row_status, "_mutation": False, "_outcome": "existing_historical"})
                conn.commit()
                return result
            else:
                cur = conn.execute("INSERT INTO wishes (feature,reason,status,times_wished,first_wished_at,last_wished_at,updated_at) VALUES (?,?,?,?,?,?,?)", (feature, reason, "open", 1, now, now, now))
                wish_id, before, after, outcome = int(cur.lastrowid), 0, 1, "created"
            self._event(conn, wish_id=wish_id, actor="k", event_type="reaffirmed" if outcome == "merged" else "created", content=reason, payload={"feature": feature, "legacy": True}, source_key=f"legacy_add:{uuid4().hex}", created_at=now)
            conn.commit()
            result = self._dict(conn.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone())
            result["_mutation"] = True
            return result

    def set_status(self, wish_id: int, status: str, *, actor: str = "user", note: str = "", source_key: Optional[str] = None) -> dict:
        self._ensure_writable()
        status = canonical_status(status)
        if status not in WISH_STATUSES:
            raise ValueError(f"invalid wish status: {status}")
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM wishes WHERE id=?", (int(wish_id),)).fetchone()
            if row is None:
                return {"mutated": False, "wish": None, "event": None, "error": "not_found"}
            old, now, note = canonical_status(row["status"]), now_iso(), str(note or "").strip()
            if old == status and not note:
                wish = self._dict(row); wish["status"] = old
                conn.commit()
                return {"mutated": False, "wish": wish, "event": None}
            assignments, values = "status=?, updated_at=?", [status, now]
            if status == "fulfilled":
                assignments += ", fulfilled_at=?, fulfilled_note=?"; values.extend([now, note])
            elif old == "fulfilled":
                assignments += ", fulfilled_at='', fulfilled_note=''"
            if status == "deleted":
                assignments += ", deleted_at=?"; values.append(now)
            values.append(int(wish_id))
            conn.execute(f"UPDATE wishes SET {assignments} WHERE id=?", values)
            event = self._event(conn, wish_id=int(wish_id), actor=actor, event_type="deleted" if status == "deleted" else "status_changed", content=note, payload={"from": old, "to": status, "note": note}, source_key=source_key, created_at=now)
            wish = self._dict(conn.execute("SELECT * FROM wishes WHERE id=?", (int(wish_id),)).fetchone()); wish["status"] = status
            conn.commit()
            return {"mutated": True, "wish": wish, "event": event}

    def add_comment(self, wish_id: int, content: str, *, author: str = "user", reply_to_comment_id: Optional[int] = None, source_key: Optional[str] = None) -> dict:
        self._ensure_writable()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM wishes WHERE id=?", (int(wish_id),)).fetchone() is None:
                return {"mutated": False, "comment": None, "error": "not_found"}
            comment = self._comment(conn, wish_id=int(wish_id), author=author, content=content, reply_to_comment_id=reply_to_comment_id, source_key=source_key)
            event = self._event(conn, wish_id=int(wish_id), actor=author, event_type="comment", content=comment["content"], payload={"comment_id": comment["id"], "reply_to_comment_id": reply_to_comment_id}, source_key=f"comment_event:{comment.get('source_key') or uuid4().hex}", created_at=comment["created_at"])
            conn.execute("UPDATE wishes SET updated_at=?, user_comment=CASE WHEN ?='user' THEN ? ELSE user_comment END, comment_updated_at=CASE WHEN ?='user' THEN ? ELSE comment_updated_at END WHERE id=?", (comment["created_at"], author, comment["content"], author, comment["created_at"], int(wish_id)))
            conn.commit()
            return {"mutated": bool(comment.get("mutation")), "comment": comment, "event": event}

    @staticmethod
    def _sync_latest_user_comment(conn: sqlite3.Connection, wish_id: int, updated_at: str) -> None:
        latest = conn.execute(
            "SELECT content, created_at FROM wish_comments WHERE wish_id=? AND author='user' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(wish_id),),
        ).fetchone()
        content = str(latest["content"] or "") if latest is not None else ""
        comment_at = str(latest["created_at"] or "") if latest is not None else ""
        conn.execute(
            "UPDATE wishes SET user_comment=?, comment_updated_at=?, updated_at=? WHERE id=?",
            (content, comment_at, updated_at, int(wish_id)),
        )

    def edit_comment(self, comment_id: int, content: str, *, actor: str = "user") -> dict:
        """Edit one actor-owned comment while preserving an audit event."""
        self._ensure_writable()
        content = str(content or "").strip()
        if not content:
            raise ValueError("comment content is required")
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM wish_comments WHERE id=?", (int(comment_id),)).fetchone()
            if row is None:
                return {"mutated": False, "comment": None, "error": "not_found"}
            if str(row["author"] or "") != actor:
                return {"mutated": False, "comment": None, "error": "not_owner"}
            previous = str(row["content"] or "")
            if previous == content:
                item = self._dict(row)
                conn.commit()
                return {"mutated": False, "comment": item, "event": None}
            now = now_iso()
            conn.execute("UPDATE wish_comments SET content=? WHERE id=?", (content, int(comment_id)))
            event = self._event(
                conn,
                wish_id=int(row["wish_id"]),
                actor=actor,
                event_type="comment_edited",
                content=content,
                payload={"comment_id": int(comment_id), "previous_content": previous},
                created_at=now,
            )
            self._sync_latest_user_comment(conn, int(row["wish_id"]), now)
            item = self._dict(conn.execute("SELECT * FROM wish_comments WHERE id=?", (int(comment_id),)).fetchone())
            conn.commit()
            return {"mutated": True, "comment": item, "event": event}

    def delete_comment(self, comment_id: int, *, actor: str = "user") -> dict:
        """Delete one actor-owned comment and retain a deletion record."""
        self._ensure_writable()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM wish_comments WHERE id=?", (int(comment_id),)).fetchone()
            if row is None:
                return {"mutated": False, "deleted": None, "error": "not_found"}
            if str(row["author"] or "") != actor:
                return {"mutated": False, "deleted": None, "error": "not_owner"}
            now = now_iso()
            deleted = self._dict(row)
            conn.execute("DELETE FROM wish_comments WHERE id=?", (int(comment_id),))
            event = self._event(
                conn,
                wish_id=int(row["wish_id"]),
                actor=actor,
                event_type="comment_deleted",
                content=str(row["content"] or ""),
                payload={"comment_id": int(comment_id), "reply_to_comment_id": row["reply_to_comment_id"]},
                created_at=now,
            )
            self._sync_latest_user_comment(conn, int(row["wish_id"]), now)
            conn.commit()
            return {"mutated": True, "deleted": deleted, "event": event}

    def set_comment(self, wish_id: int, comment: str) -> bool:
        if not str(comment or "").strip():
            return self.get_by_id(wish_id) is not None
        return bool(self.add_comment(wish_id, comment, author="user").get("comment"))

    def delete(self, wish_id: int) -> bool:
        result = self.set_status(wish_id, "deleted", actor="system", note="legacy delete")
        return bool(result.get("mutated") or result.get("wish"))

    def _validate_comment_reply(self, conn: sqlite3.Connection, reply: dict) -> tuple[Optional[dict], Optional[str]]:
        """Validate the structured AI reply before any transaction mutation."""
        try:
            wish_id = int(reply.get("wish_id"))
        except (TypeError, ValueError):
            return None, "comment_reply_wish_not_found"
        raw_target = reply.get("reply_to_comment_id")
        try:
            target_id = int(raw_target) if raw_target is not None else 0
        except (TypeError, ValueError):
            target_id = 0
        if target_id <= 0:
            return None, "comment_reply_requires_candidate"
        target = conn.execute(
            "SELECT * FROM wish_comments WHERE id=?", (target_id,)
        ).fetchone()
        if target is None:
            return None, "comment_reply_target_not_found"
        if int(target["wish_id"]) != wish_id:
            return None, "comment_reply_target_wrong_wish"
        if str(target["author"] or "") != "user":
            return None, "comment_reply_target_not_user"
        # Any real User comment on this wish remains an eligible target.  A
        # later AI comment or an earlier direct reply is context, not a lock.
        return self._dict(target), None

    @staticmethod
    def _reaffirm_basis(action: dict) -> str:
        return str(action.get("basis") or action.get("reason") or "").strip()[:500]

    @staticmethod
    def _latest_reaffirm(conn: sqlite3.Connection, wish_id: int) -> Optional[sqlite3.Row]:
        if not WishStore._table_exists(conn, "wish_events"):
            return None
        return conn.execute(
            "SELECT content, created_at FROM wish_events "
            "WHERE wish_id=? AND event_type='reaffirmed' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(wish_id),),
        ).fetchone()

    def commit_reflection_action(self, *, run_id: str, activity_id: str, node_id: str, action: Optional[dict] = None, comment_reply: Optional[dict] = None) -> list[dict]:
        self._ensure_writable()
        ids = [str(value or "").strip() for value in (run_id, activity_id, node_id)]
        if not all(ids):
            raise ValueError("run_id, activity_id and node_id are required")
        action = action if isinstance(action, dict) else {}
        action_type = str(action.get("type") or "none").strip().lower()
        if action_type not in _ACTION_TYPES:
            action_type = "none"
        action_key = hashlib.sha256(f"reflection-action:{run_id}:{activity_id}:{node_id}".encode()).hexdigest()
        comment_key = hashlib.sha256(f"reflection-comment:{run_id}:{activity_id}:{node_id}".encode()).hexdigest()
        results: list[dict] = []
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM wish_events WHERE source_key=?", (action_key,)).fetchone()
            reply = comment_reply if isinstance(comment_reply, dict) else {}
            content = str(reply.get("content") or "").strip()
            existing_comment = conn.execute(
                "SELECT * FROM wish_comments WHERE source_key=?", (comment_key,)
            ).fetchone() if content else None
            if content and existing_comment is None:
                _, reply_error = self._validate_comment_reply(conn, reply)
                if reply_error:
                    try:
                        reply_wish_id = int(reply.get("wish_id"))
                    except (TypeError, ValueError):
                        reply_wish_id = None
                    return [{
                        "wish_id": reply_wish_id,
                        "outcome": "comment_reply_rejected",
                        "error": reply_error,
                        "mutation": False,
                        "idempotent_replay": False,
                    }]
            if existing is None and action_type != "none":
                raw_id = action.get("wish_id")
                try: wish_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError): wish_id = None
                title, reason, now = str(action.get("title") or action.get("feature") or "").strip()[:120], str(action.get("reason") or "").strip()[:500], now_iso()
                if action_type == "create":
                    if not title: raise ValueError("create action requires title")
                    # A deleted wish is a tombstone, not permission to create a
                    # second row with the same identity.  Reopening/deleting a
                    # tombstone is an explicit future action; ``create`` never
                    # silently bypasses that history.
                    duplicate = self._find_exact(conn, title, include_deleted=True)
                    if duplicate is not None:
                        duplicate_status = canonical_status(duplicate["status"])
                        outcome = "ignored_deleted_tombstone" if duplicate_status == "deleted" else "ignored_exact_duplicate"
                        results.append({"wish_id": duplicate["id"], "title": str(duplicate["feature"] or ""), "event_type": "duplicate_candidate", "outcome": outcome, "status": duplicate_status, "mutation": False, "idempotent_replay": False})
                    else:
                        cur = conn.execute("INSERT INTO wishes (feature,reason,status,times_wished,first_wished_at,last_wished_at,updated_at) VALUES (?,?,?,?,?,?,?)", (title, reason, "open", 1, now, now, now))
                        wish_id = int(cur.lastrowid)
                        event = self._event(conn, wish_id=wish_id, actor="k", event_type="created", content=reason, payload={"title": title, "reason": reason}, source_key=action_key, created_at=now)
                        event.update({"outcome": "created", "wish_id": wish_id, "title": title}); results.append(event)
                elif action_type == "reaffirm":
                    if wish_id is None: raise ValueError("reaffirm action requires wish_id")
                    row = conn.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
                    if row is None: results.append({"wish_id": wish_id, "outcome": "not_found", "mutation": False})
                    elif canonical_status(row["status"]) in {"fulfilled", "deleted", "impossible_pending"}:
                        results.append({"wish_id": wish_id, "title": str(row["feature"] or ""), "outcome": "ignored_status", "status": canonical_status(row["status"]), "mutation": False})
                    else:
                        basis = self._reaffirm_basis(action)
                        if not basis:
                            results.append({
                                "wish_id": wish_id,
                                "title": str(row["feature"] or ""),
                                "outcome": "missing_basis",
                                "mutation": False,
                            })
                        else:
                            latest = self._latest_reaffirm(conn, wish_id)
                            previous_basis = str(latest["content"] or "") if latest is not None else ""
                            if latest is not None and normalise_reaffirm_basis(previous_basis) == normalise_reaffirm_basis(basis):
                                results.append({
                                    "wish_id": wish_id,
                                    "title": str(row["feature"] or ""),
                                    "outcome": "duplicate_basis",
                                    "duplicate_basis": basis,
                                    "previous_basis": previous_basis,
                                    "previous_basis_at": str(latest["created_at"] or ""),
                                    "mutation": False,
                                    "idempotent_replay": False,
                                })
                            else:
                                before = int(row["times_wished"] or 1); after = before + 1
                                conn.execute("UPDATE wishes SET times_wished=?, reason=COALESCE(NULLIF(?,''),reason), last_wished_at=?, updated_at=? WHERE id=?", (after, basis, now, now, wish_id))
                                event = self._event(conn, wish_id=wish_id, actor="k", event_type="reaffirmed", content=basis, payload={"from_count": before, "to_count": after, "reason": basis, "basis": basis}, source_key=action_key, created_at=now)
                                event.update({"outcome": "reaffirmed", "wish_id": wish_id, "title": str(row["feature"] or ""), "previous_count": before, "new_count": after, "basis": basis}); results.append(event)
                else:
                    if wish_id is None: raise ValueError(f"{action_type} action requires wish_id")
                    row = conn.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone(); target = "impossible_kept" if action_type == "retain_impossible" else "deleted"
                    if row is None: results.append({"wish_id": wish_id, "outcome": "not_found", "mutation": False})
                    elif canonical_status(row["status"]) != "impossible_pending": results.append({"wish_id": wish_id, "title": str(row["feature"] or ""), "outcome": "ignored_status", "status": canonical_status(row["status"]), "mutation": False})
                    else:
                        conn.execute("UPDATE wishes SET status=?, updated_at=?, deleted_at=? WHERE id=?", (target, now, now if target == "deleted" else "", wish_id))
                        event = self._event(conn, wish_id=wish_id, actor="k", event_type="deleted" if target == "deleted" else "retained", content=reason, payload={"from": "impossible_pending", "to": target, "reason": reason}, source_key=action_key, created_at=now)
                        event.update({"outcome": target, "wish_id": wish_id, "title": str(row["feature"] or ""), "from_status": "impossible_pending", "to_status": target}); results.append(event)
            elif existing is not None:
                replay = self._dict(existing)
                try: replay["payload"] = json.loads(replay.get("payload_json") or "{}")
                except Exception: replay["payload"] = {}
                replay.update({"mutation": False, "idempotent_replay": True}); results.append(replay)
            if content:
                if existing_comment is not None:
                    replay = self._dict(existing_comment)
                    replay.update({"mutation": False, "idempotent_replay": True, "outcome": "commented", "wish_id": int(existing_comment["wish_id"])})
                    results.append(replay)
                else:
                    reply_wish_id = int(reply["wish_id"])
                    reply_to = int(reply["reply_to_comment_id"])
                    wish_row = conn.execute("SELECT feature FROM wishes WHERE id=?", (reply_wish_id,)).fetchone()
                    comment = self._comment(conn, wish_id=reply_wish_id, author="k", content=content, reply_to_comment_id=reply_to, source_key=comment_key)
                    event = self._event(conn, wish_id=reply_wish_id, actor="k", event_type="comment", content=content, payload={"comment_id": comment["id"], "reply_to_comment_id": reply_to}, source_key=f"{comment_key}:event", created_at=comment["created_at"])
                    event.update({"outcome": "commented", "wish_id": reply_wish_id, "title": str(wish_row["feature"] or "") if wish_row else "", "comment": comment}); results.append(event)
            conn.commit()
        return results

    # Legacy v3 audit path. It remains available to old nodes, but only exact
    # matching is used and no ``fulfilled`` output can mutate the board.
    def commit_reflection_wishes(self, *, run_id: str, activity_id: str, node_id: str, wishes: List[Dict]) -> List[dict]:
        # The pre-structured contract is an audit-only compatibility path.  It
        # must never create or increment a board row during a rolling upgrade.
        # A legacy read-only database cannot even receive the audit row, so
        # return an in-memory skipped result rather than mutating its schema.
        if not isinstance(wishes, list): raise ValueError("wishes must be a list")
        if self._schema_state != "current":
            return [
                {"wish_index": index, "outcome": "legacy_skipped", "mutation": False,
                 "idempotent_replay": False}
                for index, _item in enumerate(wishes)
            ]
        results = []
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, item in enumerate(wishes):
                if not isinstance(item, dict): raise ValueError(f"wish[{index}] must be an object")
                feature = str(item.get("feature") or "").strip()
                if not feature: raise ValueError(f"wish[{index}] feature is required")
                reason, novelty = str(item.get("reason") or "").strip(), str(item.get("novelty") or "new").strip().lower()
                if novelty not in {"new", "reaffirmed", "fulfilled"}: novelty = "new"
                source_key = hashlib.sha256("\0".join((str(run_id), str(activity_id), str(node_id), str(index))).encode()).hexdigest()
                wish_hash = hashlib.sha256(feature.casefold().encode()).hexdigest()
                old = conn.execute("SELECT * FROM wish_observations WHERE source_key=?", (source_key,)).fetchone()
                if old is not None:
                    replay = self._dict(old); replay["idempotent_replay"] = True; results.append(replay); continue
                committed = now_iso()
                # Legacy arrays are no longer allowed to mutate the board.
                # Keep one audit row per item so retries remain idempotent.
                matched = self._find_exact(conn, feature, include_deleted=True)
                matched_id = int(matched["id"]) if matched is not None else None
                outcome = "skipped_fulfilled" if novelty == "fulfilled" else "legacy_skipped"
                previous_count = int(matched["times_wished"] or 0) if matched is not None else 0
                if novelty == "fulfilled":
                    cur = conn.execute("INSERT INTO wish_observations (source_key,run_id,activity_id,node_id,wish_index,wish_hash,novelty,wish_id,outcome,previous_count,new_count,committed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (source_key, run_id, activity_id, node_id, index, wish_hash, novelty, matched_id, outcome, previous_count, previous_count, committed))
                else:
                    cur = conn.execute("INSERT INTO wish_observations (source_key,run_id,activity_id,node_id,wish_index,wish_hash,novelty,wish_id,outcome,previous_count,new_count,committed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (source_key, run_id, activity_id, node_id, index, wish_hash, novelty, matched_id, outcome, previous_count, previous_count, committed))
                row = conn.execute("SELECT * FROM wish_observations WHERE id=?", (cur.lastrowid,)).fetchone(); item_result = self._dict(row); item_result["idempotent_replay"] = False; results.append(item_result)
            conn.commit()
        return results

    def get_reflection_wish_observations(self, node_id: str) -> List[dict]:
        with self._lock, self._conn() as conn:
            return [self._dict(row) for row in conn.execute("SELECT * FROM wish_observations WHERE node_id=? ORDER BY wish_index", (node_id,)).fetchall()]

    def mark_fulfilled(self, feature: str, note: str = "") -> bool:
        self._ensure_writable()
        with self._lock, self._conn() as conn:
            row = self._find_exact(conn, str(feature or "").strip())
        if row is None: return False
        result = self.set_status(int(row["id"]), "fulfilled", actor="user", note=note)
        return bool(result.get("mutated") or result.get("wish"))


_store: Optional[WishStore] = None
_store_lock = threading.Lock()


def get_wish_store() -> WishStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = WishStore()
    return _store
