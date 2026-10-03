"""Small, independent SQLite journal for shared music sessions.

It deliberately contains no conversation or account cookies.  Provider data is
refreshed on demand; this database only remembers MIRROW's own bindings and
observed playback facts.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    from datetime import timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")


class MusicStore:
    def __init__(self, path: str | Path | None = None):
        root = Path(__file__).resolve().parents[1]
        self.path = Path(path) if path else root / "data" / "music_system.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS playlist_bindings (
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL,
                    wander_default INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS playback_sessions (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, mode TEXT NOT NULL,
                    quiet INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                    song_json TEXT NOT NULL, queue_json TEXT NOT NULL,
                    queue_index INTEGER NOT NULL DEFAULT 0, subject TEXT NOT NULL,
                    observed_at TEXT NOT NULL, position_ms INTEGER,
                    error TEXT, pause_since TEXT,
                    pause_timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    playlist_id TEXT,
                    follow_external INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS playback_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, event_type TEXT NOT NULL,
                    device TEXT NOT NULL, song_id TEXT, source TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS music_materials (
                    song_id TEXT PRIMARY KEY, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS music_daily (
                    day TEXT PRIMARY KEY, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_song_catalog (
                    song_id TEXT PRIMARY KEY, data_json TEXT NOT NULL,
                    share_count INTEGER NOT NULL DEFAULT 0,
                    first_shared_at TEXT NOT NULL, last_shared_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS music_share_events (
                    source_id TEXT PRIMARY KEY, song_id TEXT NOT NULL,
                    shared_by TEXT NOT NULL, share_number INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS music_library_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL, event_type TEXT NOT NULL,
                    playlist_id TEXT NOT NULL, subject TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_music_playback_session
                    ON playback_events(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_music_playback_day
                    ON playback_events(occurred_at, event_type);
                CREATE INDEX IF NOT EXISTS idx_music_library_day
                    ON music_library_events(occurred_at, event_type);
                CREATE INDEX IF NOT EXISTS idx_music_shared_recent
                    ON shared_song_catalog(last_shared_at DESC);
            """)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(playback_sessions)").fetchall()
            }
            if "pause_since" not in columns:
                conn.execute("ALTER TABLE playback_sessions ADD COLUMN pause_since TEXT")
            if "pause_timeout_seconds" not in columns:
                conn.execute(
                    "ALTER TABLE playback_sessions ADD COLUMN pause_timeout_seconds "
                    "INTEGER NOT NULL DEFAULT 1800"
                )
            if "playlist_id" not in columns:
                conn.execute("ALTER TABLE playback_sessions ADD COLUMN playlist_id TEXT")
            if "follow_external" not in columns:
                conn.execute("ALTER TABLE playback_sessions ADD COLUMN follow_external INTEGER NOT NULL DEFAULT 0")
            binding_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(playlist_bindings)").fetchall()
            }
            if "wander_default" not in binding_columns:
                conn.execute(
                    "ALTER TABLE playlist_bindings ADD COLUMN wander_default "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # A process restart must never claim it is still controlling audio.
            conn.execute("UPDATE playback_sessions SET status='unknown', observed_at=?, pause_since=NULL "
                         "WHERE status IN ('starting','playing','paused')", (now_iso(),))

    def save_session(self, session: dict[str, Any]) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE playback_sessions SET status='superseded' WHERE id<>? AND status NOT IN ('ended','external','superseded')", (session["id"],))
            conn.execute("""INSERT INTO playback_sessions
              (id,device,mode,quiet,status,song_json,queue_json,queue_index,subject,observed_at,position_ms,error,pause_since,pause_timeout_seconds,playlist_id,follow_external)
              VALUES (:id,:device,:mode,:quiet,:status,:song_json,:queue_json,:queue_index,:subject,:observed_at,:position_ms,:error,:pause_since,:pause_timeout_seconds,:playlist_id,:follow_external)
              ON CONFLICT(id) DO UPDATE SET device=excluded.device,mode=excluded.mode,quiet=excluded.quiet,
              status=excluded.status,song_json=excluded.song_json,queue_json=excluded.queue_json,
              queue_index=excluded.queue_index,subject=excluded.subject,observed_at=excluded.observed_at,
              position_ms=excluded.position_ms,error=excluded.error,pause_since=excluded.pause_since,
              pause_timeout_seconds=excluded.pause_timeout_seconds,playlist_id=excluded.playlist_id,
              follow_external=excluded.follow_external""", {
                **session, "quiet": int(bool(session.get("quiet"))),
                "follow_external": int(bool(session.get("follow_external"))),
                "song_json": json.dumps(session.get("song") or {}, ensure_ascii=False),
                "queue_json": json.dumps(session.get("queue") or [], ensure_ascii=False),
                "pause_since": session.get("pause_since"),
                "pause_timeout_seconds": int(session.get("pause_timeout_seconds", 1800)),
                "playlist_id": str(session.get("playlist_id") or "") or None,
            })

    def active_session(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM playback_sessions WHERE status NOT IN ('ended','external','failed','superseded') ORDER BY observed_at DESC, rowid DESC LIMIT 1").fetchone()
        return self._session_row(row) if row else None

    def session(self, session_id: str) -> dict[str, Any] | None:
        """Return one durable playback session, including terminal sessions."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM playback_sessions WHERE id=?", (str(session_id),)
            ).fetchone()
        return self._session_row(row) if row else None

    def _session_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["quiet"] = bool(result["quiet"])
        result["follow_external"] = bool(result.get("follow_external"))
        result["song"] = json.loads(result.pop("song_json"))
        result["queue"] = json.loads(result.pop("queue_json"))
        return result

    def bind_playlist(self, playlist_id: str, subject: str, name: str = "",
                      wander_default: bool | None = None) -> dict[str, Any]:
        if subject not in {"owner", "k", "shared"}:
            raise ValueError("Invalid music shelf")
        playlist_id = str(playlist_id)
        with self._connection() as conn:
            previous = conn.execute(
                "SELECT wander_default FROM playlist_bindings WHERE id=?", (playlist_id,)
            ).fetchone()
            selected = bool(previous["wander_default"]) if previous and wander_default is None else bool(wander_default)
            selected = selected and subject == "k"
            if selected:
                conn.execute("UPDATE playlist_bindings SET wander_default=0 WHERE id<>?", (playlist_id,))
            conn.execute("""INSERT INTO playlist_bindings(id,subject,name,imported_at,wander_default)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                subject=excluded.subject,name=excluded.name,wander_default=excluded.wander_default""",
                (playlist_id, subject, name, now_iso(), int(selected)))
        return {"id": playlist_id, "subject": subject, "name": name,
                "wander_default": selected}

    def bindings(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id,subject,name,imported_at,wander_default FROM playlist_bindings "
                "ORDER BY imported_at DESC"
            ).fetchall()
        return [{**dict(row), "wander_default": bool(row["wander_default"])} for row in rows]

    def bind_playlists(self, items: list[dict[str, Any]]) -> None:
        if any(item["subject"] not in {"owner", "k", "shared"} for item in items):
            raise ValueError("Invalid music shelf")
        with self._connection() as conn:
            for p in items:
                pid = str(p["id"])
                previous = conn.execute(
                    "SELECT wander_default FROM playlist_bindings WHERE id=?", (pid,)
                ).fetchone()
                selected = bool(previous["wander_default"]) and p["subject"] == "k" if previous else False
                conn.execute("""INSERT INTO playlist_bindings(id,subject,name,imported_at,wander_default)
                    VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    subject=excluded.subject,name=excluded.name,wander_default=excluded.wander_default""",
                    (pid, p["subject"], p["name"], now_iso(), int(selected)))

    def unbind_playlist(self, playlist_id: str) -> bool:
        """Forget only MIRROW's shelf binding; never delete the NetEase playlist."""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM playlist_bindings WHERE id=?", (str(playlist_id),))
        return cursor.rowcount > 0

    def set_wander_default(self, playlist_id: str, selected: bool = True) -> dict[str, Any]:
        playlist_id = str(playlist_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id,subject,name FROM playlist_bindings WHERE id=?", (playlist_id,)
            ).fetchone()
            if not row:
                raise ValueError("Playlist is not bound")
            if selected and row["subject"] != "k":
                raise ValueError("Wander default must belong to K")
            if selected:
                conn.execute("UPDATE playlist_bindings SET wander_default=0")
            conn.execute(
                "UPDATE playlist_bindings SET wander_default=? WHERE id=?",
                (int(bool(selected)), playlist_id),
            )
        return {**dict(row), "wander_default": bool(selected)}

    def library_event(self, event_type: str, playlist_id: str, subject: str | None = None,
                      details: dict[str, Any] | None = None) -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO music_library_events(occurred_at,event_type,playlist_id,subject,details_json) "
                "VALUES(?,?,?,?,?)",
                (now_iso(), str(event_type), str(playlist_id), subject,
                 json.dumps(details or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def library_events_after(self, event_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM music_library_events WHERE id>? ORDER BY id LIMIT ?",
                (max(0, int(event_id)), max(1, min(int(limit), 100))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def latest_library_event_id(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id),0) FROM music_library_events").fetchone()
        return int(row[0])

    def library_events_for_day(self, day: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM music_library_events WHERE substr(occurred_at,1,10)=? ORDER BY id",
                (str(day),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def event(self, session_id: str, event_type: str, device: str, song_id: str | None,
              source: str, details: dict[str, Any] | None = None) -> None:
        with self._connection() as conn:
            conn.execute("INSERT INTO playback_events(session_id,occurred_at,event_type,device,song_id,source,details_json) VALUES(?,?,?,?,?,?,?)",
                (session_id, now_iso(), event_type, device, song_id, source,
                 json.dumps(details or {}, ensure_ascii=False)))

    def events_for_day(self, day: str) -> list[dict[str, Any]]:
        """Return local-date events for daily music cognition, without interpretation."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM playback_events WHERE substr(occurred_at,1,10)=? ORDER BY occurred_at", (day,)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            result.append(event)
        return result

    def events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return the ordered device/action evidence for one playback session."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM playback_events WHERE session_id=? ORDER BY id",
                (str(session_id),),
            ).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            result.append(event)
        return result

    def material(self, song_id):
        with self._connection() as conn:
            row = conn.execute("SELECT data_json FROM music_materials WHERE song_id=?", (str(song_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def save_material(self, song_id, data):
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO music_materials VALUES(?,?)", (str(song_id), json.dumps(data, ensure_ascii=False)))

    def daily(self, day):
        with self._connection() as conn:
            row = conn.execute("SELECT data_json FROM music_daily WHERE day=?", (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_daily(self, day, data):
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO music_daily VALUES(?,?)", (day, json.dumps(data, ensure_ascii=False)))

    def record_song_share(self, song: dict[str, Any], source_id: str,
                          shared_by: str) -> dict[str, Any]:
        """Cache one real card delivery and return its stable occurrence number."""
        sid = str(song.get("id") or "")
        if not sid or not source_id or shared_by not in {"owner", "k"}:
            raise ValueError("Invalid song share receipt")
        stamp = now_iso()
        cached = {key: song.get(key) for key in (
            "id", "name", "artist", "album", "cover", "title_aliases", "duration", "link"
        )}
        with self._connection() as conn:
            previous = conn.execute(
                "SELECT song_id,share_number FROM music_share_events WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if previous and str(previous["song_id"]) == sid:
                number = int(previous["share_number"])
                current = conn.execute(
                    "SELECT share_count FROM shared_song_catalog WHERE song_id=?", (sid,)
                ).fetchone()
                count = int(current["share_count"]) if current else number
                if current:
                    conn.execute(
                        "UPDATE shared_song_catalog SET data_json=? WHERE song_id=?",
                        (json.dumps(cached, ensure_ascii=False), sid),
                    )
                return {**song, "share_number": number, "share_count": count,
                        "shared_by": shared_by, "cached_share": True}
            if previous:
                old_sid = str(previous["song_id"])
                conn.execute(
                    "UPDATE shared_song_catalog SET share_count=MAX(0,share_count-1) WHERE song_id=?",
                    (old_sid,),
                )
                conn.execute("DELETE FROM music_share_events WHERE source_id=?", (source_id,))
            current = conn.execute(
                "SELECT share_count,first_shared_at FROM shared_song_catalog WHERE song_id=?", (sid,)
            ).fetchone()
            number = (int(current["share_count"]) if current else 0) + 1
            first = str(current["first_shared_at"]) if current else stamp
            conn.execute(
                """INSERT INTO shared_song_catalog(song_id,data_json,share_count,first_shared_at,last_shared_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(song_id) DO UPDATE SET
                   data_json=excluded.data_json,share_count=excluded.share_count,
                   last_shared_at=excluded.last_shared_at""",
                (sid, json.dumps(cached, ensure_ascii=False), number, first, stamp),
            )
            conn.execute(
                "INSERT INTO music_share_events(source_id,song_id,shared_by,share_number,occurred_at) VALUES(?,?,?,?,?)",
                (source_id, sid, shared_by, number, stamp),
            )
        return {**cached, "share_number": number, "share_count": number,
                "shared_by": shared_by, "cached_share": True}

    def shared_song(self, song_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT data_json,share_count,last_shared_at FROM shared_song_catalog WHERE song_id=?",
                (str(song_id),),
            ).fetchone()
        if not row:
            return None
        return {**json.loads(row["data_json"]), "share_count": int(row["share_count"]),
                "last_shared_at": row["last_shared_at"], "cached_share": True}

    def search_shared_songs(self, query: str = "", limit: int = 30) -> list[dict[str, Any]]:
        needle = str(query or "").strip().casefold()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT data_json,share_count,last_shared_at FROM shared_song_catalog "
                "WHERE share_count>0 ORDER BY last_shared_at DESC",
            ).fetchall()
        songs = []
        for row in rows:
            song = json.loads(row["data_json"])
            haystack = f"{song.get('name', '')} {song.get('artist', '')}".casefold()
            if needle and needle not in haystack:
                continue
            songs.append({**song, "share_count": int(row["share_count"]),
                          "last_shared_at": row["last_shared_at"], "cached_share": True})
            if len(songs) >= max(1, min(int(limit), 100)):
                break
        return songs

    def activity_days(self):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT day FROM (
                    SELECT DISTINCT substr(occurred_at,1,10) AS day
                    FROM playback_events WHERE event_type='heard'
                    UNION
                    SELECT DISTINCT substr(occurred_at,1,10) AS day
                    FROM music_library_events
                ) ORDER BY day
            """).fetchall()
        return [row[0] for row in rows]

    def last_episode(self, session_id):
        with self._connection() as conn:
            row = conn.execute("SELECT details_json FROM playback_events WHERE session_id=? AND event_type='heard' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return json.loads(row[0]).get("episode") if row else None
