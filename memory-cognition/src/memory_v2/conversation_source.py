"""Read-only access to the authoritative ``conversation_messages`` table."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .models import BatchSourceRef, EncodingBatch, SourceRef, digest_batch_sources
from .source_projection import SOURCE_PROJECTION_START_ACTIVE_DATE, memory_source_content


DEFAULT_MEMORY_ROLES = ("user", "assistant", "notification", "system")
AMBIENT_OBSERVATION_EVENT = "ambient_listening_observation"
_REQUIRED_COLUMNS = {
    "id",
    "active_date",
    "calendar_date",
    "session_id",
    "timestamp",
    "role",
    "content",
    "message_id",
    "is_wander",
    "is_sentinel",
    "is_reminder",
    "event_type",
}

SOURCE_KINDS = frozenset({"chat", "wander", "sentinel", "reminder"})
MAX_NEIGHBOR_MESSAGES = 4
_CANONICAL_SESSION_ID_RE = re.compile(r"^session_[0-9]{13}_[0-9a-f]{8}$")


def is_canonical_session_id(session_id: str) -> bool:
    """Return whether an ID belongs to SessionManager's persisted chat namespace."""

    return bool(_CANONICAL_SESSION_ID_RE.fullmatch(str(session_id or "")))


def _source_kind(
    *, role: str, is_wander: bool, is_sentinel: bool, is_reminder: bool
) -> str:
    marked = [
        name
        for name, enabled in (
            ("wander", is_wander),
            ("sentinel", is_sentinel),
            ("reminder", is_reminder),
        )
        if enabled
    ]
    if len(marked) > 1:
        raise ConversationSourceError("conversation source has conflicting proactive flags")
    if marked and role != "assistant":
        raise ConversationSourceError("only assistant sources may carry proactive flags")
    return marked[0] if marked else "chat"


class ConversationSourceError(RuntimeError):
    """The raw conversation authority cannot satisfy the V2 source contract."""


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConversationMessage:
    """One authoritative row, including content only while encoding is in memory."""

    row_id: int
    active_date: str
    calendar_date: str
    session_id: str
    timestamp: str
    role: str
    content: str
    message_id: str
    is_wander: bool = False
    is_sentinel: bool = False
    is_reminder: bool = False
    event_type: str = ""

    @property
    def source_kind(self) -> str:
        return _source_kind(
            role=self.role,
            is_wander=self.is_wander,
            is_sentinel=self.is_sentinel,
            is_reminder=self.is_reminder,
        )

    def as_source_ref(
        self,
        *,
        span_start: int | None = None,
        span_end: int | None = None,
    ) -> SourceRef:
        span_digest = ""
        if span_start is not None and span_end is not None:
            span_digest = digest_text(self.content[span_start:span_end])
        return SourceRef(
            message_row_id=self.row_id,
            message_id=self.message_id,
            session_id=self.session_id,
            source_ts=self.timestamp,
            source_role=self.role,
            source_kind=self.source_kind,
            source_event_type=self.event_type,
            span_start=span_start,
            span_end=span_end,
            span_digest=span_digest,
        )

    def as_batch_source_ref(self) -> BatchSourceRef:
        return BatchSourceRef(
            message_row_id=self.row_id,
            message_id=self.message_id,
            session_id=self.session_id,
            active_date=self.active_date,
            calendar_date=self.calendar_date,
            source_ts=self.timestamp,
            source_role=self.role,
            content_digest=digest_text(self.content),
            source_kind=self.source_kind,
            source_event_type=self.event_type,
        )


def digest_messages(messages: Sequence[ConversationMessage]) -> str:
    """Hash identities and bodies without copying raw text into a receipt."""

    return digest_batch_sources(
        tuple(message.as_batch_source_ref() for message in messages)
    )


class ConversationSource:
    """A strictly read-only adapter over the raw SQLite authority."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        canonical_sessions_only: bool = False,
    ):
        path = Path(db_path).resolve()
        if not path.is_file():
            raise ConversationSourceError(f"conversation database does not exist: {path}")
        self.db_path = path
        self.canonical_sessions_only = bool(canonical_sessions_only)
        self._verify_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _verify_schema(self) -> None:
        try:
            with self._connect() as connection:
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(conversation_messages)"
                    ).fetchall()
                }
        except sqlite3.Error as exc:
            raise ConversationSourceError(
                f"cannot inspect conversation authority: {exc}"
            ) from exc
        missing = _REQUIRED_COLUMNS - columns
        if missing:
            raise ConversationSourceError(
                "conversation_messages is missing required columns: "
                + ", ".join(sorted(missing))
            )
        self._has_tool_calls = "tool_calls" in columns
        self._has_long_text_card = "long_text_card" in columns

    @property
    def _fields(self) -> str:
        receipt_column = "tool_calls" if self._has_tool_calls else "NULL AS tool_calls"
        card_column = "long_text_card" if self._has_long_text_card else "NULL AS long_text_card"
        return (
            "id, active_date, calendar_date, session_id, timestamp, role, "
            "content, message_id, is_wander, is_sentinel, is_reminder, event_type, "
            + receipt_column + ", " + card_column
        )

    @property
    def _nonempty_condition(self) -> str:
        conditions = ["trim(content)<>''"]
        if self._has_tool_calls:
            conditions.append("(tool_calls IS NOT NULL AND trim(tool_calls) NOT IN ('', '[]'))")
        if self._has_long_text_card:
            conditions.append("(long_text_card IS NOT NULL AND trim(long_text_card) NOT IN ('', '{}'))")
        return "(" + " OR ".join(conditions) + ")"

    @staticmethod
    def _normalise_roles(roles: Sequence[str]) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(str(role).strip() for role in roles if str(role).strip()))
        if not result:
            raise ValueError("at least one source role is required")
        return result

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> ConversationMessage:
        values = dict(row)
        projected = memory_source_content(values)
        return ConversationMessage(
            row_id=int(row["id"]),
            active_date=str(row["active_date"] or ""),
            calendar_date=str(row["calendar_date"] or ""),
            session_id=str(row["session_id"] or ""),
            timestamp=str(row["timestamp"] or ""),
            role=str(row["role"] or ""),
            content=projected if projected is not None else "",
            message_id=str(row["message_id"] or ""),
            is_wander=bool(row["is_wander"]),
            is_sentinel=bool(row["is_sentinel"]),
            is_reminder=bool(row["is_reminder"]),
            event_type=str(row["event_type"] or ""),
        )

    def _accept_message(self, message: ConversationMessage) -> bool:
        return bool(message.content.strip()) and (
            not self.canonical_sessions_only or is_canonical_session_id(message.session_id)
        )

    def _accepted_rows(self, rows: Sequence[sqlite3.Row]) -> list[ConversationMessage]:
        messages = [self._row_to_message(row) for row in rows]
        return [message for message in messages if self._accept_message(message)]

    def read_active_date(
        self,
        active_date: str,
        *,
        roles: Sequence[str] = DEFAULT_MEMORY_ROLES,
    ) -> list[ConversationMessage]:
        """Read by stored active date; never infer it from the timestamp."""

        clean_roles = self._normalise_roles(roles)
        placeholders = ", ".join("?" for _ in clean_roles)
        query = (
            f"SELECT {self._fields} "
            "FROM conversation_messages "
            f"WHERE active_date=? AND role IN ({placeholders}) "
            f"AND {self._nonempty_condition} ORDER BY timestamp, id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, (active_date, *clean_roles)).fetchall()
        return self._accepted_rows(rows)

    def read_all(
        self,
        *,
        roles: Sequence[str] = DEFAULT_MEMORY_ROLES,
    ) -> list[ConversationMessage]:
        """Read the complete authority in stored chronological partitions.

        This is intended for bounded offline index construction.  It does not
        infer active dates from timestamps and never mutates the source DB.
        """

        clean_roles = self._normalise_roles(roles)
        placeholders = ", ".join("?" for _ in clean_roles)
        query = (
            f"SELECT {self._fields} "
            "FROM conversation_messages "
            f"WHERE role IN ({placeholders}) AND {self._nonempty_condition} "
            "ORDER BY active_date, session_id, timestamp, id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, clean_roles).fetchall()
        return self._accepted_rows(rows)

    def read_recent_dialogue_before(
        self,
        message_id: str,
        *,
        max_user_turns: int = 2,
        max_messages: int = 6,
    ) -> list[ConversationMessage]:
        """Read a bounded suffix of ordinary dialogue before one message.

        The stored active date and session are taken from the anchor row.  No
        wall-clock or timestamp-derived day inference is performed here.
        Proactive assistant output is excluded because it is not reliable
        antecedent material for pronouns in Human's current utterance.
        """

        clean_id = str(message_id or "").strip()
        if not clean_id:
            return []
        if not 1 <= int(max_user_turns) <= 5:
            raise ValueError("max_user_turns must be within 1..5")
        if not 1 <= int(max_messages) <= 12:
            raise ValueError("max_messages must be within 1..12")
        columns = self._fields
        with self._connect() as connection:
            anchor = connection.execute(
                f"SELECT {columns} FROM conversation_messages WHERE message_id = ?",
                (clean_id,),
            ).fetchone()
            if anchor is None:
                return []
            if self.canonical_sessions_only and not is_canonical_session_id(
                str(anchor["session_id"] or "")
            ):
                return []
            rows = connection.execute(
                f"SELECT {columns} FROM conversation_messages "
                "WHERE session_id = ? AND active_date = ? "
                f"AND role IN ('user', 'assistant') AND {self._nonempty_condition} "
                "AND is_wander = 0 AND is_sentinel = 0 AND is_reminder = 0 "
                "AND (julianday(timestamp) < julianday(?) OR "
                "(julianday(timestamp) = julianday(?) AND id < ?)) "
                "ORDER BY julianday(timestamp) DESC, id DESC LIMIT 24",
                (
                    str(anchor["session_id"] or ""),
                    str(anchor["active_date"] or ""),
                    str(anchor["timestamp"] or ""),
                    str(anchor["timestamp"] or ""),
                    int(anchor["id"]),
                ),
            ).fetchall()
        messages = list(reversed(self._accepted_rows(rows)))
        suffix: list[ConversationMessage] = []
        user_turns = 0
        for message in reversed(messages):
            if message.role == "user":
                if user_turns >= int(max_user_turns):
                    break
                user_turns += 1
            suffix.append(message)
            if len(suffix) >= int(max_messages):
                break
        return list(reversed(suffix))

    def list_active_dates(
        self,
        *,
        before_active_date: str | None = None,
        roles: Sequence[str] = DEFAULT_MEMORY_ROLES,
    ) -> tuple[str, ...]:
        """List non-empty stored active days with bounded notice validation.

        Canonical-session filtering remains identical to the row readers.  The
        optional cutoff is strict: a day equal to the cutoff is still active
        and therefore absent from the result.
        """

        clean_roles = self._normalise_roles(roles)
        placeholders = ", ".join("?" for _ in clean_roles)
        where = [f"role IN ({placeholders})", self._nonempty_condition]
        params: list[str] = list(clean_roles)
        if before_active_date is not None:
            where.append("active_date < ?")
            params.append(str(before_active_date))
        base_query = (
            "SELECT DISTINCT active_date, session_id, role, event_type "
            "FROM conversation_messages "
            f"WHERE {' AND '.join(where)} AND role!='notification' "
            "AND (role!='system' OR (event_type='group_chat_summary' "
            "AND active_date>=? AND content NOT LIKE '%----------群聊摘要----------%')) "
            "ORDER BY active_date, session_id"
        )
        with self._connect() as connection:
            base_rows = connection.execute(
                base_query, (*params, SOURCE_PROJECTION_START_ACTIVE_DATE)
            ).fetchall()
            notice_rows = []
            if "notification" in clean_roles:
                notice_where = ["role='notification'", "active_date>=?", self._nonempty_condition]
                notice_params: list[str] = [SOURCE_PROJECTION_START_ACTIVE_DATE]
                if before_active_date is not None:
                    notice_where.append("active_date<?")
                    notice_params.append(str(before_active_date))
                notice_rows = connection.execute(
                    f"SELECT {self._fields} FROM conversation_messages "
                    f"WHERE {' AND '.join(notice_where)}",
                    notice_params,
                ).fetchall()
            ambient_rows = connection.execute(
                "SELECT DISTINCT active_date, session_id, role, event_type "
                "FROM conversation_messages WHERE role='notification' "
                "AND event_type=? AND trim(content)<>''"
                + (" AND active_date<?" if before_active_date is not None else ""),
                (AMBIENT_OBSERVATION_EVENT, str(before_active_date))
                if before_active_date is not None else (AMBIENT_OBSERVATION_EVENT,),
            ).fetchall() if "notification" in clean_roles else []
        accepted = {
            str(row["active_date"] or "")
            for row in (*base_rows, *ambient_rows)
            if str(row["active_date"] or "")
            and (not self.canonical_sessions_only
                 or is_canonical_session_id(str(row["session_id"] or "")))
        }
        accepted.update(message.active_date for message in self._accepted_rows(notice_rows)
                        if message.active_date)
        return tuple(sorted(accepted))

    def list_message_ids(
        self,
        *,
        roles: Sequence[str] = DEFAULT_MEMORY_ROLES,
    ) -> tuple[str, ...]:
        """List live source identities without returning private message bodies."""

        clean_roles = self._normalise_roles(roles)
        placeholders = ", ".join("?" for _ in clean_roles)
        query = (
            f"SELECT {self._fields} FROM conversation_messages "
            f"WHERE role IN ({placeholders}) AND {self._nonempty_condition} "
            "AND trim(message_id)<>'' ORDER BY id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, clean_roles).fetchall()
        return tuple(dict.fromkeys(message.message_id.strip()
                                   for message in self._accepted_rows(rows)
                                   if message.message_id.strip()))

    def read_batch(
        self,
        batch_sources: Sequence[BatchSourceRef],
    ) -> list[ConversationMessage]:
        if not batch_sources:
            return []
        return self.read_message_ids(
            tuple(source.message_id for source in batch_sources)
        )

    def read_message_ids(
        self,
        message_ids: Sequence[str],
    ) -> list[ConversationMessage]:
        """Read exact stable IDs in caller order without exposing row ranges."""

        message_ids = tuple(str(message_id) for message_id in message_ids)
        if not message_ids:
            return []
        if any(not message_id for message_id in message_ids):
            raise ConversationSourceError("message IDs must not be empty")
        if len(set(message_ids)) != len(message_ids):
            raise ConversationSourceError("message IDs must be unique")
        placeholders = ", ".join("?" for _ in message_ids)
        query = (
            f"SELECT {self._fields} "
            "FROM conversation_messages "
            f"WHERE message_id IN ({placeholders})"
        )
        with self._connect() as connection:
            rows = connection.execute(query, message_ids).fetchall()
        by_id = {
            message.message_id: message
            for message in (self._row_to_message(row) for row in rows)
            if self._accept_message(message)
        }
        return [by_id[message_id] for message_id in message_ids if message_id in by_id]

    def read_message_neighborhood(
        self,
        message_id: str,
        *,
        before: int = 1,
        after: int = 1,
        roles: Sequence[str] = DEFAULT_MEMORY_ROLES,
    ) -> list[ConversationMessage]:
        """Read a small chronological window around one exact source message.

        The stored active date and session form the boundary.  Row IDs are used
        only to make equal timestamps deterministic, never as chronology.
        """

        clean_id = str(message_id or "").strip()
        if not clean_id:
            raise ConversationSourceError("message ID must not be empty")
        if (
            isinstance(before, bool)
            or isinstance(after, bool)
            or not isinstance(before, int)
            or not isinstance(after, int)
            or not 0 <= before <= MAX_NEIGHBOR_MESSAGES
            or not 0 <= after <= MAX_NEIGHBOR_MESSAGES
        ):
            raise ValueError(
                f"before and after must be integers within 0..{MAX_NEIGHBOR_MESSAGES}"
            )
        clean_roles = self._normalise_roles(roles)
        role_placeholders = ", ".join("?" for _ in clean_roles)
        fields = self._fields + " "
        with self._connect() as connection:
            anchor_rows = connection.execute(
                f"SELECT {fields}FROM conversation_messages WHERE message_id=? LIMIT 2",
                (clean_id,),
            ).fetchall()
            if not anchor_rows:
                return []
            if len(anchor_rows) > 1:
                raise ConversationSourceError(
                    "message ID is not unique in conversation authority"
                )
            anchor = self._row_to_message(anchor_rows[0])
            if not self._accept_message(anchor):
                return []
            common = (
                anchor.session_id,
                anchor.active_date,
                *clean_roles,
                anchor.timestamp,
                anchor.timestamp,
                anchor.row_id,
            )
            previous_rows = connection.execute(
                f"SELECT {fields}FROM conversation_messages "
                "WHERE session_id=? AND active_date=? "
                f"AND role IN ({role_placeholders}) AND {self._nonempty_condition} "
                "AND (timestamp < ? OR (timestamp = ? AND id < ?)) "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*common, before),
            ).fetchall()
            next_rows = connection.execute(
                f"SELECT {fields}FROM conversation_messages "
                "WHERE session_id=? AND active_date=? "
                f"AND role IN ({role_placeholders}) AND {self._nonempty_condition} "
                "AND (timestamp > ? OR (timestamp = ? AND id > ?)) "
                "ORDER BY timestamp, id LIMIT ?",
                (*common, after),
            ).fetchall()
        previous = [message for row in reversed(previous_rows)
                    if self._accept_message(message := self._row_to_message(row))]
        following = [message for row in next_rows
                     if self._accept_message(message := self._row_to_message(row))]
        return [*previous, anchor, *following]

    def validate_batch(
        self,
        batch: EncodingBatch,
        *,
        batch_sources: Sequence[BatchSourceRef],
    ) -> bool:
        if not batch_sources or not self.validate_batch_sources(batch_sources):
            return False
        messages = self.read_batch(batch_sources)
        return (
            len(messages) == batch.source_count
            and messages[0].row_id == batch.from_message_row_id
            and messages[-1].row_id == batch.to_message_row_id
            and messages[0].message_id == batch.from_message_id
            and messages[-1].message_id == batch.to_message_id
            and digest_messages(messages) == batch.source_digest
        )

    def validate_batch_sources(self, sources: Sequence[BatchSourceRef]) -> bool:
        if not sources:
            return False
        messages = self.read_batch(sources)
        if len(messages) != len(sources):
            return False
        return all(
            message.as_batch_source_ref() == source
            for message, source in zip(messages, sources)
        )

    def validate_refs(self, sources: Sequence[SourceRef]) -> bool:
        """Verify exact row identities and optional quote spans against raw text."""

        if not sources:
            return False
        message_ids = tuple(dict.fromkeys(source.message_id for source in sources))
        if any(not message_id for message_id in message_ids):
            return False
        by_message_id = {message.message_id: message
                         for message in self.read_message_ids(message_ids)}
        if len(by_message_id) != len(message_ids):
            return False
        for source in sources:
            message = by_message_id.get(source.message_id)
            if message is None:
                return False
            if (
                message.row_id != source.message_row_id
                or message.session_id != source.session_id
                or message.timestamp != source.source_ts
                or message.role != source.source_role
                or message.source_kind != source.source_kind
                or message.event_type != source.source_event_type
            ):
                return False
            if source.span_start is None and source.span_end is None:
                if source.span_digest:
                    return False
                continue
            if source.span_start is None or source.span_end is None:
                return False
            if not 0 <= source.span_start < source.span_end <= len(message.content):
                return False
            selected = message.content[source.span_start : source.span_end]
            if digest_text(selected) != source.span_digest:
                return False
        return True
