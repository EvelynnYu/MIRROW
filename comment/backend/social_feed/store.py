"""Authoritative storage for the private AI/A-Ning social feed.

The feed is deliberately separate from conversation_messages and Elpis' group
chat "moments".  Opening the feed consumes only feed notifications; it never
creates chat history or a topic boundary.
"""

from __future__ import annotations

import json
import base64
import math
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import aiosqlite
except ModuleNotFoundError:  # pragma: no cover - only used in minimal test installs
    class _CompatCursor:
        def __init__(self, cursor: sqlite3.Cursor):
            self._cursor = cursor

        @property
        def rowcount(self) -> int:
            return self._cursor.rowcount

        async def fetchone(self):
            return self._cursor.fetchone()

        async def fetchall(self):
            return self._cursor.fetchall()

    class _CompatConnection:
        """Tiny aiosqlite-compatible fallback for environments without extras.

        MIRROW's normal installation uses aiosqlite.  Keeping the service
        importable in the repository's minimal test environment avoids making
        a feed test depend on the full production dependency set; feed queries
        are small and this fallback is deliberately synchronous.
        """

        def __init__(self, path: str | os.PathLike[str]):
            self._conn = sqlite3.connect(path, timeout=10)

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._conn.row_factory = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is not None:
                self._conn.rollback()
            self._conn.close()

        async def execute(self, sql: str, parameters=()):
            return _CompatCursor(self._conn.execute(sql, parameters))

        async def executescript(self, sql: str):
            self._conn.executescript(sql)

        async def commit(self):
            self._conn.commit()

    class _CompatAioSqlite:
        Row = sqlite3.Row
        Connection = _CompatConnection

        @staticmethod
        def connect(path: str | os.PathLike[str]):
            return _CompatConnection(path)

    aiosqlite = _CompatAioSqlite()


try:
    _BEIJING = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows/minimal installs may omit the tzdata wheel
    _BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
_AUTHORS = {"aning", "k"}
_VISIBILITIES = {"private", "public"}
_CURSOR_VERSION = 1


def _encode_feed_cursor(created_at: float, moment_id: str) -> str:
    """Encode the timeline boundary without exposing a client-facing schema."""
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "created_at": float(created_at), "id": str(moment_id)},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_feed_cursor(value: str | None) -> tuple[float, str] | None:
    if not value:
        return None
    try:
        encoded = str(value).strip()
        if len(encoded) > 512:
            raise ValueError("cursor_too_long")
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        created_at = float(data["created_at"])
        moment_id = str(data["id"]).strip()
        if int(data.get("v", 0)) != _CURSOR_VERSION or not moment_id or not math.isfinite(created_at):
            raise ValueError("invalid_cursor")
        return created_at, moment_id
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, json.JSONDecodeError) as exc:
        raise SocialFeedError("invalid_cursor") from exc


class SocialFeedError(ValueError):
    """A safe domain validation error."""


class SocialFeedStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = Path(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS social_moments (
                    id TEXT PRIMARY KEY,
                    author TEXT NOT NULL CHECK (author IN ('aning', 'k')),
                    content TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'private'
                        CHECK (visibility IN ('private', 'public')),
                    source_run_id TEXT,
                    source_activity_id TEXT,
                    created_at REAL NOT NULL,
                    forward_json TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_social_moments_created
                    ON social_moments(created_at DESC);

                CREATE TABLE IF NOT EXISTS social_comments (
                    id TEXT PRIMARY KEY,
                    moment_id TEXT NOT NULL REFERENCES social_moments(id) ON DELETE CASCADE,
                    author TEXT NOT NULL CHECK (author IN ('aning', 'k')),
                    content TEXT NOT NULL,
                    reply_to_id TEXT REFERENCES social_comments(id) ON DELETE SET NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_social_comments_moment
                    ON social_comments(moment_id, created_at);

                CREATE TABLE IF NOT EXISTS social_reactions (
                    moment_id TEXT NOT NULL REFERENCES social_moments(id) ON DELETE CASCADE,
                    author TEXT NOT NULL CHECK (author IN ('aning', 'k')),
                    reaction TEXT NOT NULL CHECK (reaction = 'like'),
                    created_at REAL NOT NULL,
                    PRIMARY KEY (moment_id, author)
                );

                CREATE TABLE IF NOT EXISTS social_notifications (
                    id TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL CHECK (recipient IN ('aning', 'k')),
                    actor TEXT NOT NULL CHECK (actor IN ('aning', 'k')),
                    kind TEXT NOT NULL CHECK (kind IN ('moment', 'comment', 'like')),
                    moment_id TEXT NOT NULL REFERENCES social_moments(id) ON DELETE CASCADE,
                    comment_id TEXT REFERENCES social_comments(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    announced_at REAL,
                    announce_source TEXT NOT NULL DEFAULT '',
                    announce_source_id TEXT NOT NULL DEFAULT '',
                    read_at REAL,
                    -- Reading means the referenced domain content was really
                    -- supplied to its recipient.  A main-chat reminder only
                    -- updates the separate announcement fields above.
                    read_source TEXT NOT NULL DEFAULT '',
                    read_source_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_social_notifications_recipient
                    ON social_notifications(recipient, read_at, created_at DESC);

                -- A topic can close more than once (retry, reconnect, or a
                -- delayed maintenance task), but the daily social decision
                -- is a single durable fact.  The row is intentionally kept
                -- after a post is deleted so a later retry cannot post twice
                -- for the same day.
                CREATE TABLE IF NOT EXISTS social_daily_summary_decisions (
                    target_date TEXT PRIMARY KEY,
                    action TEXT NOT NULL CHECK (action IN ('none', 'post')),
                    content TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'private'
                        CHECK (visibility IN ('private', 'public')),
                    reason TEXT NOT NULL DEFAULT '',
                    moment_id TEXT,
                    notification_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_social_daily_summary_decisions_date
                    ON social_daily_summary_decisions(target_date);

                -- A runtime node may finish the feed mutation and crash before
                -- its own evidence row is persisted.  This receipt is part of
                -- the feed transaction, so retrying the same run/activity/node
                -- returns the original result without creating a second post,
                -- comment, like, or notification.
                CREATE TABLE IF NOT EXISTS social_action_receipts (
                    source_key TEXT PRIMARY KEY,
                    action TEXT NOT NULL CHECK (
                        action IN ('post', 'comment', 'like', 'delete', 'set_visibility')
                    ),
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                """
            )
            await self._migrate_action_receipts(db)
            # Additive migration for databases created before main-chat
            # notification consumption was made reversible.  Existing reads
            # stay consumed (empty source means they are never restored).
            for column, definition in (
                ("announced_at", "REAL"),
                ("announce_source", "TEXT NOT NULL DEFAULT ''"),
                ("announce_source_id", "TEXT NOT NULL DEFAULT ''"),
                ("read_source", "TEXT NOT NULL DEFAULT ''"),
                ("read_source_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    await db.execute(
                        f"ALTER TABLE social_notifications ADD COLUMN {column} {definition}"
                    )
                except Exception:
                    pass
            try:
                await db.execute("ALTER TABLE social_moments ADD COLUMN forward_json TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            # Older builds treated a successful main-chat reminder as if AI
            # had read the referenced post/comment.  Preserve the reminder as
            # announced, but restore the domain item to genuinely unread.
            await db.execute(
                "UPDATE social_notifications SET "
                "announced_at=COALESCE(announced_at, read_at), "
                "announce_source=CASE WHEN announce_source='' THEN read_source ELSE announce_source END, "
                "announce_source_id=CASE WHEN announce_source_id='' THEN read_source_id ELSE announce_source_id END "
                "WHERE recipient='k' AND read_source='main_chat' AND read_at IS NOT NULL"
            )
            await db.execute(
                "UPDATE social_notifications SET read_at=NULL, read_source='', read_source_id='' "
                "WHERE recipient='k' AND read_source='main_chat'"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_notifications_read_source "
                "ON social_notifications(read_source, read_source_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_social_notifications_announcement "
                "ON social_notifications(recipient, announced_at, created_at DESC)"
            )
            from .mentions import initialize_private
            await initialize_private(db)
            await db.commit()
        self._initialized = True

    @staticmethod
    async def _migrate_action_receipts(db: aiosqlite.Connection) -> None:
        """Extend the action receipt CHECK without dropping old receipts.

        SQLite cannot alter a CHECK constraint in place.  The old table is
        renamed to a clearly non-authoritative backup inside the same
        transaction, then copied into the new table before initialization
        commits.  Existing post/comment/like receipts therefore survive a
        restart or a migration interruption; all future reads use the new
        table only.
        """
        row = await (await db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='social_action_receipts'"
        )).fetchone()
        table_sql = str(row[0] if row else "").lower()
        if not table_sql or "set_visibility" in table_sql:
            return

        backup_name = f"social_action_receipts_legacy_{uuid.uuid4().hex[:12]}"
        await db.execute(
            f"ALTER TABLE social_action_receipts RENAME TO {backup_name}"
        )
        await db.execute(
            """
            CREATE TABLE social_action_receipts (
                source_key TEXT PRIMARY KEY,
                action TEXT NOT NULL CHECK (
                    action IN ('post', 'comment', 'like', 'delete', 'set_visibility')
                ),
                result_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO social_action_receipts "
            "(source_key, action, result_json, created_at) "
            f"SELECT source_key, action, result_json, created_at FROM {backup_name}"
        )
        # The migration is transactional; after the copy the legacy table is
        # no longer authoritative and must not remain as a second evidence copy.
        await db.execute(f"DROP TABLE {backup_name}")

    @staticmethod
    def _author(value: str) -> str:
        author = str(value or "").strip().lower()
        if author not in _AUTHORS:
            raise SocialFeedError("invalid_author")
        return author

    @staticmethod
    def _content(value: str, *, limit: int) -> str:
        content = str(value or "").strip()
        if not content:
            raise SocialFeedError("content_required")
        if len(content) > limit:
            raise SocialFeedError("content_too_long")
        return content

    @staticmethod
    def _other(author: str) -> str:
        return "k" if author == "aning" else "aning"

    @staticmethod
    def _visibility(value: str) -> str:
        visibility = str(value or "").strip().lower()
        if visibility not in _VISIBILITIES:
            raise SocialFeedError("invalid_visibility")
        return visibility

    @staticmethod
    def _date_bounds(day: str | date | None) -> tuple[float, float, str]:
        if day is None:
            selected = datetime.now(_BEIJING).date()
        elif isinstance(day, date):
            selected = day
        else:
            try:
                selected = date.fromisoformat(str(day))
            except ValueError as exc:
                raise SocialFeedError("invalid_date") from exc
        start = datetime.combine(selected, dt_time.min, _BEIJING)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp(), selected.isoformat()

    async def create_moment(
        self,
        author: str,
        content: str,
        *,
        source_run_id: str = "",
        source_activity_id: str = "",
        source_key: str | None = None,
        visibility: str = "private",
        created_at: float | None = None,
        mention_actor_ids: list[str] | None = None,
        forward_ref=None,
    ) -> dict[str, Any]:
        await self.initialize()
        author = self._author(author)
        visibility = self._visibility(visibility)
        from .mentions import normalise, encode
        try:
            mentions = normalise(mention_actor_ids, private=True)
        except ValueError as exc:
            raise SocialFeedError(str(exc)) from None
        now = float(time.time() if created_at is None else created_at)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            replay = await self._read_action_receipt(db, source_key, "post")
            if replay is not None:
                return replay
            content = self._content(content, limit=1200)
            from .forwarding import encode_forward_ref
            try:
                forward_json = encode_forward_ref(forward_ref)
            except ValueError as exc:
                raise SocialFeedError(str(exc)) from None
            moment_id = f"sm_{uuid.uuid4().hex}"
            await db.execute(
                "INSERT INTO social_moments "
                "(id, author, content, visibility, source_run_id, source_activity_id, created_at, mentions_json, forward_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (moment_id, author, content, visibility, source_run_id or None, source_activity_id or None, now, encode(mentions), forward_json),
            )
            await self._insert_notification(db, self._other(author), author, "moment", moment_id, None, now)
            result = {
                "id": moment_id, "author": author, "content": content,
                "visibility": visibility,
                "created_at": now, "comments": [], "reactions": [],
                "mention_actor_ids": mentions,
            }
            if forward_json:
                result['forward_ref'] = forward_ref
            await self._write_action_receipt(db, source_key, "post", result, now)
            await db.commit()
        return result

    async def get_daily_summary_decision(
        self, target_date: str | date | None = None
    ) -> dict[str, Any] | None:
        """Return the durable topic-end decision for one Beijing calendar day.

        A missing row means that no decision has been committed yet.  This is
        deliberately a separate fact from the feed moments themselves: a
        deleted post must not make a later topic-end retry eligible to post a
        second time.
        """
        await self.initialize()
        _start, _end, day_text = self._date_bounds(target_date)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT target_date, action, content, visibility, reason, "
                "moment_id, notification_id, created_at "
                "FROM social_daily_summary_decisions WHERE target_date=?",
                (day_text,),
            )).fetchone()
        return dict(row) if row is not None else None

    async def commit_daily_summary_decision(
        self,
        target_date: str | date | None,
        action: str,
        content: str = "",
        visibility: str = "private",
        reason: str = "",
    ) -> dict[str, Any]:
        """Commit one daily ``none``/``post`` decision atomically.

        For ``post`` the decision row, AI's moment, and A-Ning's feed
        notification are inserted in one ``BEGIN IMMEDIATE`` transaction.  A
        repeated call for the same date returns the original row with
        ``idempotent_replay=True`` and never creates another side effect.
        """
        _start, _end, day_text = self._date_bounds(target_date)
        action = str(action or "").strip().lower()
        if action not in {"none", "post"}:
            raise SocialFeedError("invalid_daily_action")
        visibility = self._visibility(visibility)
        clean_reason = str(reason or "").strip()[:500]
        clean_content = self._content(content, limit=1200) if action == "post" else ""

        await self.initialize()
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")

            existing = await (await db.execute(
                "SELECT target_date, action, content, visibility, reason, "
                "moment_id, notification_id, created_at "
                "FROM social_daily_summary_decisions WHERE target_date=?",
                (day_text,),
            )).fetchone()
            if existing is not None:
                await db.commit()
                replay = dict(existing)
                replay["idempotent_replay"] = True
                return replay

            moment_id: str | None = None
            notification_id: str | None = None
            if action == "post":
                moment_id = f"sm_{uuid.uuid4().hex}"
                await db.execute(
                    "INSERT INTO social_moments "
                    "(id, author, content, visibility, source_run_id, source_activity_id, created_at) "
                    "VALUES (?, 'k', ?, ?, ?, ?, ?)",
                    (
                        moment_id,
                        clean_content,
                        visibility,
                        "topic_end_social_feed",
                        day_text,
                        now,
                    ),
                )
                notification_id = await self._insert_notification(
                    db, "aning", "k", "moment", moment_id, None, now
                )

            await db.execute(
                "INSERT INTO social_daily_summary_decisions "
                "(target_date, action, content, visibility, reason, moment_id, notification_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day_text,
                    action,
                    clean_content,
                    visibility,
                    clean_reason,
                    moment_id,
                    notification_id,
                    now,
                ),
            )
            await db.commit()

        return {
            "target_date": day_text,
            "action": action,
            "content": clean_content,
            "visibility": visibility,
            "reason": clean_reason,
            "moment_id": moment_id,
            "notification_id": notification_id,
            "created_at": now,
            "idempotent_replay": False,
        }

    async def delete_moment(
        self,
        moment_id: str,
        actor: str,
        *,
        source_key: str | None = None,
    ) -> dict[str, Any]:
        """Delete one author's post and its dependent feed records.

        ``ON DELETE CASCADE`` removes comments, reactions and notifications
        in the same transaction.  A source receipt is written for AI/runtime
        callers so retrying a completed delete is a harmless replay rather
        than a second mutation or a false ``moment_not_found`` failure.
        """
        await self.initialize()
        actor = self._author(actor)
        moment_id = str(moment_id or "").strip()
        if not moment_id:
            raise SocialFeedError("moment_id_required")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            replay = await self._read_action_receipt(db, source_key, "delete")
            if replay is not None:
                return replay
            row = await (await db.execute(
                "SELECT author FROM social_moments WHERE id=?", (moment_id,)
            )).fetchone()
            if row is None:
                raise SocialFeedError("moment_not_found")
            if str(row["author"]) != actor:
                raise SocialFeedError("forbidden")
            await db.execute("DELETE FROM social_moments WHERE id=?", (moment_id,))
            result = {
                "ok": True,
                "action": "delete",
                "moment_id": moment_id,
                "author": actor,
                "deleted": True,
            }
            await self._write_action_receipt(db, source_key, "delete", result, time.time())
            await db.commit()
        return result

    async def set_moment_visibility(
        self,
        moment_id: str,
        actor: str,
        visibility: str,
        *,
        allow_owner_override: bool = False,
        source_key: str | None = None,
    ) -> dict[str, Any]:
        """Change visibility under owner/explicit A-Ning override rules.

        AI can only change AI's own post.  The HTTP A-Ning boundary passes
        ``allow_owner_override=True`` to manage either author's post; the
        store still requires the actor to be A-Ning, so a model/runtime caller
        cannot self-grant this privilege.
        """
        await self.initialize()
        actor = self._author(actor)
        moment_id = str(moment_id or "").strip()
        if not moment_id:
            raise SocialFeedError("moment_id_required")
        visibility = self._visibility(visibility)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            replay = await self._read_action_receipt(db, source_key, "set_visibility")
            if replay is not None:
                return replay
            row = await (await db.execute(
                "SELECT author, visibility FROM social_moments WHERE id=?", (moment_id,)
            )).fetchone()
            if row is None:
                raise SocialFeedError("moment_not_found")
            owner = str(row["author"])
            if owner != actor and not (allow_owner_override and actor == "aning"):
                raise SocialFeedError("forbidden")
            previous = str(row["visibility"])
            changed = previous != visibility
            if changed:
                await db.execute(
                    "UPDATE social_moments SET visibility=? WHERE id=?",
                    (visibility, moment_id),
                )
            result = {
                "ok": True,
                "action": "set_visibility",
                "moment_id": moment_id,
                "author": owner,
                "visibility": visibility,
                "previous_visibility": previous,
                "changed": changed,
            }
            await self._write_action_receipt(
                db, source_key, "set_visibility", result, time.time()
            )
            await db.commit()
        return result

    async def add_comment(
        self,
        moment_id: str,
        author: str,
        content: str,
        *,
        reply_to_id: str | None = None,
        source_key: str | None = None,
        created_at: float | None = None,
        mention_actor_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        await self.initialize()
        author = self._author(author)
        from .mentions import normalise, encode
        try:
            mentions = normalise(mention_actor_ids, private=True)
        except ValueError as exc:
            raise SocialFeedError(str(exc)) from None
        now = float(time.time() if created_at is None else created_at)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            replay = await self._read_action_receipt(db, source_key, "comment")
            if replay is not None:
                return replay
            content = self._content(content, limit=600)
            comment_id = f"sc_{uuid.uuid4().hex}"
            moment = await (await db.execute(
                "SELECT author FROM social_moments WHERE id=?", (moment_id,)
            )).fetchone()
            if not moment:
                raise SocialFeedError("moment_not_found")
            reply_author = None
            if reply_to_id:
                reply = await (await db.execute(
                    "SELECT author FROM social_comments WHERE id=? AND moment_id=?",
                    (reply_to_id, moment_id),
                )).fetchone()
                if not reply:
                    raise SocialFeedError("reply_not_found")
                reply_author = str(reply["author"])
            await db.execute(
                "INSERT INTO social_comments "
                "(id, moment_id, author, content, reply_to_id, created_at, mentions_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (comment_id, moment_id, author, content, reply_to_id, now, encode(mentions)),
            )
            recipients = ({str(moment["author"]), reply_author} | set(mentions)) - {None, author}
            for recipient in recipients:
                await self._insert_notification(
                    db, str(recipient), author, "comment", moment_id, comment_id, now
                )
            result = {
                "id": comment_id, "moment_id": moment_id, "author": author,
                "content": content, "reply_to_id": reply_to_id, "created_at": now,
                "mention_actor_ids": mentions,
            }
            await self._write_action_receipt(db, source_key, "comment", result, now)
            await db.commit()
        return result

    async def delete_comment(self, moment_id: str, comment_id: str, actor: str) -> dict[str, Any]:
        await self.initialize()
        actor = self._author(actor)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT author FROM social_comments WHERE id=? AND moment_id=?",
                (comment_id, moment_id),
            )).fetchone()
            if row is None:
                raise SocialFeedError("comment_not_found")
            if actor != "aning" and row["author"] != actor:
                raise SocialFeedError("forbidden")
            await db.execute("DELETE FROM social_comments WHERE id=? AND moment_id=?", (comment_id, moment_id))
            await db.commit()
        return {"id": comment_id, "moment_id": moment_id, "status": "deleted"}

    async def toggle_like(self, moment_id: str, author: str) -> dict[str, Any]:
        await self.initialize()
        author = self._author(author)
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            # Serialize the read/flip pair.  Without an immediate transaction,
            # two UI requests can both observe the same state and one loses its
            # toggle (or raises a uniqueness error while trying to like).
            await db.execute("BEGIN IMMEDIATE")
            moment = await (await db.execute(
                "SELECT author FROM social_moments WHERE id=?", (moment_id,)
            )).fetchone()
            if not moment:
                raise SocialFeedError("moment_not_found")
            existing = await (await db.execute(
                "SELECT 1 FROM social_reactions WHERE moment_id=? AND author=?",
                (moment_id, author),
            )).fetchone()
            notification_id = None
            if existing:
                await db.execute(
                    "DELETE FROM social_reactions WHERE moment_id=? AND author=?",
                    (moment_id, author),
                )
                active = False
            else:
                await db.execute(
                    "INSERT INTO social_reactions (moment_id, author, reaction, created_at) "
                    "VALUES (?, ?, 'like', ?)",
                    (moment_id, author, now),
                )
                active = True
                recipient = str(moment["author"])
                if recipient != author:
                    notification_id = await self._insert_notification(
                        db, recipient, author, "like", moment_id, None, now
                    )
            result = {"ok": True, "active": active, "moment_id": moment_id, "author": author}
            if notification_id:
                result["notification_id"] = notification_id
            await db.commit()
        return result

    async def ensure_like(
        self, moment_id: str, author: str, *, source_key: str | None = None
    ) -> dict[str, Any]:
        """Idempotent autonomous like; unlike the UI toggle it never removes one."""
        await self.initialize()
        author = self._author(author)
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            replay = await self._read_action_receipt(db, source_key, "like")
            if replay is not None:
                return replay
            moment = await (await db.execute(
                "SELECT author FROM social_moments WHERE id=?", (moment_id,)
            )).fetchone()
            if not moment:
                raise SocialFeedError("moment_not_found")
            # INSERT OR IGNORE is the final database-level guard even though
            # BEGIN IMMEDIATE already serializes this connection's check.
            cur = await db.execute(
                "INSERT OR IGNORE INTO social_reactions "
                "(moment_id, author, reaction, created_at) VALUES (?, ?, 'like', ?)",
                (moment_id, author, now),
            )
            inserted = int(cur.rowcount or 0) == 1
            notification_id = None
            if inserted and str(moment["author"]) != author:
                notification_id = await self._insert_notification(
                    db, str(moment["author"]), author, "like", moment_id, None, now
                )
            result = {"ok": True, "active": True, "moment_id": moment_id,
                      "author": author, "existing": not inserted,
                      "inserted": inserted}
            if notification_id:
                result["notification_id"] = notification_id
            await self._write_action_receipt(db, source_key, "like", result, now)
            await db.commit()
        return result

    async def list_moments(
        self,
        *,
        day: str | date | None = None,
        limit: int = 50,
        visibility: str | None = None,
        cursor: str | None = None,
        anchor_day: str | date | None = None,
    ) -> dict[str, Any]:
        """Read the feed as a stable newest-first timeline.

        ``day`` remains the exact Beijing-day query used by the AI tool and
        older clients.  The UI timeline leaves ``day`` empty and uses an
        opaque cursor ordered by ``created_at DESC, id DESC``.  ``anchor_day``
        positions the first page at (or immediately before) a Beijing day
        without turning the page into a day-only view.
        """
        await self.initialize()
        day_bounds = self._date_bounds(day) if day is not None else None
        anchor_bounds = self._date_bounds(anchor_day) if anchor_day is not None else None
        limit = min(100, max(1, int(limit)))
        visibility_filter = self._visibility(visibility) if visibility is not None else None
        decoded_cursor = _decode_feed_cursor(cursor)
        clauses: list[str] = []
        params: list[Any] = []
        if day_bounds is not None:
            start, end, _day_text = day_bounds
            clauses.extend(["created_at>=?", "created_at<?"])
            params.extend([start, end])
        elif anchor_bounds is not None and decoded_cursor is None:
            # Start at the newest row at or before the selected day's end.  A
            # missing day therefore lands on the nearest older material while
            # preserving the continuous timeline below it.
            _anchor_start, anchor_end, _anchor_text = anchor_bounds
            clauses.append("created_at<?")
            params.append(anchor_end)
        if decoded_cursor is not None:
            boundary_created_at, boundary_id = decoded_cursor
            clauses.append("(created_at<? OR (created_at=? AND id<?))")
            params.extend([boundary_created_at, boundary_created_at, boundary_id])
        if visibility_filter is not None:
            clauses.append("visibility=?")
            params.append(visibility_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Every view uses the same stable cursor contract.  Fetching one
            # extra row reports whether another page exists without an
            # unstable count query, including explicit day-filtered views.
            query_limit = limit + 1
            rows = await (await db.execute(
                "SELECT id, author, content, visibility, created_at, mentions_json, forward_json FROM social_moments "
                f"{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, query_limit),
            )).fetchall()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            items = [dict(row) for row in page_rows]
            await self._attach_interactions(db, items)
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_feed_cursor(last["created_at"], last["id"])
        return {
            "date": day_bounds[2] if day_bounds is not None else "",
            "anchor_day": anchor_bounds[2] if anchor_bounds is not None else "",
            "items": items,
            "total": len(items),
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
        }

    async def get_moment(self, moment_id: str) -> dict[str, Any] | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT id, author, content, visibility, created_at, mentions_json, forward_json FROM social_moments WHERE id=?",
                (moment_id,),
            )).fetchone()
            if not row:
                return None
            items = [dict(row)]
            await self._attach_interactions(db, items)
        return items[0]

    async def unread_notifications(
        self, recipient: str, *, limit: int | None = 50
    ) -> list[dict[str, Any]]:
        await self.initialize()
        recipient = self._author(recipient)
        limit_sql = "" if limit is None else " LIMIT ?"
        params: tuple[Any, ...] = (
            (recipient,)
            if limit is None
            else (recipient, min(100, max(1, int(limit))))
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT n.id, n.actor, n.kind, n.moment_id, n.comment_id, n.created_at, "
                "m.content AS moment_content, c.content AS comment_content, "
                "CASE WHEN n.comment_id IS NULL THEN m.mentions_json ELSE c.mentions_json END AS mention_targets "
                "FROM social_notifications n "
                "JOIN social_moments m ON m.id=n.moment_id "
                "LEFT JOIN social_comments c ON c.id=n.comment_id "
                "WHERE n.recipient=? AND n.read_at IS NULL "
                f"ORDER BY n.created_at ASC{limit_sql}",
                params,
            )).fetchall()
        from .mentions import notice_projection
        return [notice_projection(row, recipient) for row in rows]

    async def unannounced_notifications(
        self, recipient: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return unread domain notices not yet surfaced as a chat reminder."""

        await self.initialize()
        recipient = self._author(recipient)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT n.id, n.actor, n.kind, n.moment_id, n.comment_id, n.created_at, "
                "m.content AS moment_content, c.content AS comment_content, "
                "CASE WHEN n.comment_id IS NULL THEN m.mentions_json ELSE c.mentions_json END AS mention_targets "
                "FROM social_notifications n "
                "JOIN social_moments m ON m.id=n.moment_id "
                "LEFT JOIN social_comments c ON c.id=n.comment_id "
                "WHERE n.recipient=? AND n.read_at IS NULL AND n.announced_at IS NULL "
                "ORDER BY n.created_at ASC LIMIT ?",
                (recipient, min(100, max(1, int(limit)))),
            )).fetchall()
        from .mentions import notice_projection
        return [notice_projection(row, recipient) for row in rows]

    async def mark_notifications_read(
        self,
        recipient: str,
        notification_ids: Iterable[str] | None = None,
        *,
        read_source: str = "ui",
        read_source_id: str = "",
    ) -> int:
        await self.initialize()
        recipient = self._author(recipient)
        read_source = str(read_source or "ui").strip().lower()[:40]
        read_source_id = str(read_source_id or "").strip()[:200]
        # The page only receives at most 100 unread rows.  Keep the explicit-ID
        # update bounded too, so a malformed client cannot create an enormous
        # SQLite placeholder list while still preserving idempotent marking.
        ids = list(dict.fromkeys(
            str(item) for item in (notification_ids or []) if str(item)
        ))[:100]
        if notification_ids is not None and not ids:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if ids:
                marks = ",".join("?" for _ in ids)
                cur = await db.execute(
                    f"UPDATE social_notifications SET read_at=?, read_source=?, read_source_id=? "
                    f"WHERE recipient=? AND read_at IS NULL AND id IN ({marks})",
                    (time.time(), read_source, read_source_id, recipient, *ids),
                )
            else:
                cur = await db.execute(
                    "UPDATE social_notifications SET read_at=?, read_source=?, read_source_id=? "
                    "WHERE recipient=? AND read_at IS NULL",
                    (time.time(), read_source, read_source_id, recipient),
                )
            await db.commit()
            return max(0, int(cur.rowcount or 0))

    def _ensure_sync_schema(self, db: sqlite3.Connection) -> None:
        """Create/migrate only the feed tables needed by sync consumption.

        Main-chat prompt commit is synchronous.  It must not call
        ``asyncio.run`` while already inside the request event loop, so this
        narrow method keeps the same store-owned transaction boundary.
        """
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_moments (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL CHECK (author IN ('aning', 'k')),
                content TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private'
                    CHECK (visibility IN ('private', 'public')),
                source_run_id TEXT,
                source_activity_id TEXT,
                created_at REAL NOT NULL,
                forward_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS social_comments (
                id TEXT PRIMARY KEY,
                moment_id TEXT NOT NULL REFERENCES social_moments(id) ON DELETE CASCADE,
                author TEXT NOT NULL CHECK (author IN ('aning', 'k')),
                content TEXT NOT NULL,
                reply_to_id TEXT REFERENCES social_comments(id) ON DELETE SET NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS social_notifications (
                id TEXT PRIMARY KEY,
                recipient TEXT NOT NULL CHECK (recipient IN ('aning', 'k')),
                actor TEXT NOT NULL CHECK (actor IN ('aning', 'k')),
                kind TEXT NOT NULL CHECK (kind IN ('moment', 'comment', 'like')),
                moment_id TEXT NOT NULL REFERENCES social_moments(id) ON DELETE CASCADE,
                comment_id TEXT REFERENCES social_comments(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                announced_at REAL,
                announce_source TEXT NOT NULL DEFAULT '',
                announce_source_id TEXT NOT NULL DEFAULT '',
                read_at REAL,
                read_source TEXT NOT NULL DEFAULT '',
                read_source_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        for column, definition in (
            ("announced_at", "REAL"),
            ("announce_source", "TEXT NOT NULL DEFAULT ''"),
            ("announce_source_id", "TEXT NOT NULL DEFAULT ''"),
            ("read_source", "TEXT NOT NULL DEFAULT ''"),
            ("read_source_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                db.execute(
                    f"ALTER TABLE social_notifications ADD COLUMN {column} {definition}"
                )
            except sqlite3.OperationalError:
                pass
        try:
            db.execute("ALTER TABLE social_moments ADD COLUMN forward_json TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        db.execute(
            "UPDATE social_notifications SET "
            "announced_at=COALESCE(announced_at, read_at), "
            "announce_source=CASE WHEN announce_source='' THEN read_source ELSE announce_source END, "
            "announce_source_id=CASE WHEN announce_source_id='' THEN read_source_id ELSE announce_source_id END "
            "WHERE recipient='k' AND read_source='main_chat' AND read_at IS NOT NULL"
        )
        db.execute(
            "UPDATE social_notifications SET read_at=NULL, read_source='', read_source_id='' "
            "WHERE recipient='k' AND read_source='main_chat'"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_notifications_read_source "
            "ON social_notifications(read_source, read_source_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_notifications_announcement "
            "ON social_notifications(recipient, announced_at, created_at DESC)"
        )

    def mark_notifications_announced_sync(
        self,
        recipient: str,
        notification_ids: Iterable[str],
        *,
        announce_source: str = "main_chat",
        announce_source_id: str = "",
    ) -> int:
        """Record a reminder without claiming that its domain content was read."""

        recipient = self._author(recipient)
        source = str(announce_source or "main_chat").strip().lower()[:40]
        source_id = str(announce_source_id or "").strip()[:200]
        if not source_id:
            raise SocialFeedError("announce_source_id_required")
        ids = list(dict.fromkeys(
            str(item).strip() for item in (notification_ids or []) if str(item).strip()
        ))[:100]
        if not ids:
            return 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            self._ensure_sync_schema(db)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE social_notifications SET announced_at=?, announce_source=?, announce_source_id=? "
                f"WHERE recipient=? AND read_at IS NULL AND announced_at IS NULL AND id IN ({marks})",
                (time.time(), source, source_id, recipient, *ids),
            )
            db.commit()
            return max(0, int(cur.rowcount or 0))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_notifications_read_sync(
        self,
        recipient: str,
        notification_ids: Iterable[str],
        *,
        read_source: str = "main_chat",
        read_source_id: str = "",
    ) -> int:
        """Synchronously claim explicit unread IDs in one IMMEDIATE txn.

        This is intentionally explicit-ID-only and bounded to 100 rows.  It is
        used by the main-chat prompt ledger after provider success/reply
        persistence; failures or empty claims leave notifications untouched.
        """
        recipient = self._author(recipient)
        source = str(read_source or "main_chat").strip().lower()[:40]
        source_id = str(read_source_id or "").strip()[:200]
        if not source_id:
            raise SocialFeedError("read_source_id_required")
        ids = list(dict.fromkeys(
            str(item).strip() for item in (notification_ids or []) if str(item).strip()
        ))[:100]
        if not ids:
            return 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3's context manager commits/rolls back but does not close the
        # connection.  Close explicitly so Windows callers can immediately
        # rotate/delete a temporary feed database after a synchronous claim.
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            self._ensure_sync_schema(db)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE social_notifications SET read_at=?, read_source=?, read_source_id=? "
                f"WHERE recipient=? AND read_at IS NULL AND id IN ({marks})",
                (time.time(), source, source_id, recipient, *ids),
            )
            db.commit()
            return max(0, int(cur.rowcount or 0))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def restore_main_chat_announcements(self, announce_source_id: str) -> int:
        """Restore only reminders announced by one revoked main-chat turn."""
        source_id = str(announce_source_id or "").strip()[:200]
        if not source_id:
            return 0
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE social_notifications SET announced_at=NULL, announce_source='', announce_source_id='' "
                "WHERE recipient='k' AND read_at IS NULL "
                "AND announce_source='main_chat' AND announce_source_id=?",
                (source_id,),
            )
            await db.commit()
            return max(0, int(cur.rowcount or 0))

    async def restore_main_chat_notifications(self, read_source_id: str) -> int:
        """Compatibility alias for the pre-announcement method name."""

        return await self.restore_main_chat_announcements(read_source_id)

    @staticmethod
    def _normalise_source_key(source_key: str | None) -> str:
        value = str(source_key or "").strip()
        if len(value) > 200:
            raise SocialFeedError("source_key_too_long")
        return value

    @classmethod
    async def _read_action_receipt(
        cls,
        db: aiosqlite.Connection,
        source_key: str | None,
        action: str,
    ) -> dict[str, Any] | None:
        key = cls._normalise_source_key(source_key)
        if not key:
            return None
        row = await (await db.execute(
            "SELECT action, result_json FROM social_action_receipts WHERE source_key=?", (key,)
        )).fetchone()
        if row is None:
            return None
        if str(row["action"]) != action:
            raise SocialFeedError("source_key_conflict")
        try:
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise SocialFeedError("invalid_action_receipt") from exc
        if not isinstance(result, dict):
            raise SocialFeedError("invalid_action_receipt")
        result["idempotent_replay"] = True
        return result

    @classmethod
    async def _write_action_receipt(
        cls,
        db: aiosqlite.Connection,
        source_key: str | None,
        action: str,
        result: dict[str, Any],
        created_at: float,
    ) -> None:
        key = cls._normalise_source_key(source_key)
        if not key:
            return
        stored = dict(result)
        stored.pop("idempotent_replay", None)
        await db.execute(
            "INSERT INTO social_action_receipts (source_key, action, result_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key, action, json.dumps(stored, ensure_ascii=False, sort_keys=True), created_at),
        )

    async def _attach_interactions(self, db: aiosqlite.Connection, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        ids = [item["id"] for item in items]
        marks = ",".join("?" for _ in ids)
        comments = await (await db.execute(
            f"SELECT id, moment_id, author, content, reply_to_id, created_at, mentions_json "
            f"FROM social_comments WHERE moment_id IN ({marks}) ORDER BY created_at ASC",
            ids,
        )).fetchall()
        reactions = await (await db.execute(
            f"SELECT moment_id, author, reaction, created_at "
            f"FROM social_reactions WHERE moment_id IN ({marks}) ORDER BY created_at ASC",
            ids,
        )).fetchall()
        comment_map: dict[str, list[dict[str, Any]]] = {item_id: [] for item_id in ids}
        reaction_map: dict[str, list[dict[str, Any]]] = {item_id: [] for item_id in ids}
        from .mentions import project
        for row in comments:
            comment_map[str(row["moment_id"])].append(project(dict(row)))
        for row in reactions:
            reaction_map[str(row["moment_id"])].append(dict(row))
        for item in items:
            project(item)
            item["comments"] = comment_map[item["id"]]
            item["reactions"] = reaction_map[item["id"]]

    @staticmethod
    async def _insert_notification(
        db: aiosqlite.Connection,
        recipient: str,
        actor: str,
        kind: str,
        moment_id: str,
        comment_id: str | None,
        created_at: float,
    ) -> str:
        notification_id = f"sn_{uuid.uuid4().hex}"
        await db.execute(
            "INSERT INTO social_notifications "
            "(id, recipient, actor, kind, moment_id, comment_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (notification_id, recipient, actor, kind, moment_id, comment_id, created_at),
        )
        return notification_id


_STORE = None


def get_social_feed_store():
    global _STORE
    if _STORE is None:
        from .hybrid_store import HybridSocialFeedStore
        from .public_wall import PublicWall
        root = Path(__file__).resolve().parents[1]
        _STORE = HybridSocialFeedStore(
            SocialFeedStore(root / "events" / "social_feed.db"),
            PublicWall(root / "events" / "social_public.db"),
        )
    return _STORE
