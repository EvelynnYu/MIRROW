from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from .errors import MusicNotFound, PlaybackUnavailable, SessionConflict
from .provider import NetEaseProvider
from .store import MusicStore, now_iso

Adapter = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]] | dict[str, Any]]
_service: "MusicService | None" = None
DEFAULT_PAUSE_TIMEOUT_SECONDS = 30 * 60
MAX_PAUSE_TIMEOUT_SECONDS = 24 * 60 * 60
START_CONFIRMATION_GRACE_SECONDS = 30


class MusicService:
    def __init__(self, store: MusicStore | None = None, provider: NetEaseProvider | None = None,
                 adapter: Adapter | None = None):
        self.store = store or MusicStore()
        self.provider = provider or NetEaseProvider()
        self.adapter = adapter
        self._lock = asyncio.Lock()
        self._end_receipts = set()
        self._heard_episodes = set()
        self._episode = uuid.uuid4().hex
        self._external_candidate: tuple[str, str] | None = None
        self._session: dict[str, Any] | None = self.store.active_session()
        self._allow_advance = self._session is None
        self._source = "explicit"
        if self._session:
            episode = self.store.last_episode(self._session["id"])
            if episode:
                self._episode = episode
                self._heard_episodes.add(episode)

    def configure(self, adapter: Adapter | None = None) -> None:
        self.adapter = adapter

    async def _adapter(self, action: str, device: str, song: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.adapter:
            raise PlaybackUnavailable("播放设备尚未连接")
        try:
            result = self.adapter(action, device, song)
            result = await result if inspect.isawaitable(result) else result
            if not isinstance(result, dict) or result.get("success") is False:
                raise PlaybackUnavailable("设备没有确认音乐操作")
            if action in {"pause", "resume"} and result.get("playing") is not (action == "resume"):
                raise PlaybackUnavailable("网易云尚未确认暂停或继续，请检查设备")
            return result
        except Exception as error:
            if self._session:
                self._session["status"] = "unknown"
                self._session["pause_since"] = None
                self._session["error"] = "设备操作没有得到确认，请检查网易云与设备连接"
                self._save(self._session)
            if isinstance(error, PlaybackUnavailable): raise
            raise PlaybackUnavailable("播放设备暂不可用，请检查连接与网易云权限") from None

    def status(self) -> dict[str, Any]:
        session = self._public_session(self._session) if self._session else None
        if session and session["status"] in {"playing", "paused"}:
            from datetime import datetime
            try:
                if (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(session["observed_at"])).total_seconds() > 15:
                    session["status"] = "unknown"
                    session["error"] = "设备回执已过期，等待重新确认"
            except (ValueError, TypeError):
                session["status"] = "unknown"
        return {"session": session, "capabilities": {
            "library": True, "account": True, "playback": self.adapter is not None,
        }, "playlists": self.store.bindings()}

    async def search_songs(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Prefer songs already shared in MIRROW, then merge fresh provider hits."""
        cached = self.store.search_shared_songs(query, limit)
        try:
            remote = await self.provider.search(query, limit=limit)
        except MusicNotFound:
            if cached:
                return cached
            raise
        by_id = {str(song.get("id") or ""): song for song in cached}
        result = list(cached)
        for song in remote:
            sid = str(song.get("id") or "")
            if sid in by_id:
                index = next(i for i, item in enumerate(result) if str(item.get("id") or "") == sid)
                result[index] = {**song, **{
                    key: by_id[sid][key]
                    for key in ("share_count", "last_shared_at", "cached_share")
                    if key in by_id[sid]
                }}
            else:
                result.append(song)
        return result[:max(1, min(int(limit), 30))]

    async def start_song(self, song: dict[str, Any], device: str, mode: str, queue: list[dict[str, Any]] | None = None,
                         subject: str = "shared", pause_timeout_seconds: int | None = None,
                         playlist_id: str | None = None) -> dict[str, Any]:
        if device not in {"mobile", "computer"} or mode not in {"single", "list", "loop", "one"}:
            raise MusicNotFound("播放参数无效")
        queue = list(queue or [song])
        if not queue:
            raise MusicNotFound("播放队列为空")
        async with self._lock:
            if self._session and self._session["device"] != device and self._allow_advance:
                raise SessionConflict("另一台设备有未结束的共同播放，请先结束那次会话")
            carry_follow = bool(self._session and self._session["device"] == device
                                and self._session.get("follow_external"))
            metadata = await self._adapter("play", device, song)
            self._episode = uuid.uuid4().hex
            self._external_candidate = None
            self._source = "explicit"
            self._allow_advance = True
            session = {"id": uuid.uuid4().hex, "device": device, "mode": mode, "quiet": False,
                       "follow_external": carry_follow,
                       "status": "playing" if self._confirmed(song, metadata) else "starting", "song": song,
                       "queue": queue, "queue_index": 0, "subject": subject, "observed_at": now_iso(),
                       "playlist_id": str(playlist_id or "") or None,
                       "position_ms": metadata.get("position_ms"), "error": None,
                       "commanded_at": now_iso(),
                       "command_dispatched": bool(metadata.get("dispatched")),
                       "pause_since": None,
                       "pause_timeout_seconds": self._pause_timeout(pause_timeout_seconds)}
            if session["status"] == "starting" and metadata.get("dispatched"):
                session["error"] = (
                    "点播已送达；请为 MIRROW 开启通知访问以同步播放状态"
                    if metadata.get("notification_access") is False
                    else "点播已送达，正在等待网易云确认播放状态"
                )
            self._session = session
            self.store.save_session(session)
            self.store.event(session["id"], "commanded", device, song.get("id"), "explicit", {"mode": mode, "subject": subject, "song": song})
            self._record_heard(session, metadata, "explicit")
            return self._public_session(session)

    async def control(self, action: str, session_id: str) -> dict[str, Any] | None:
        if action not in {"pause", "resume", "next", "end"}:
            raise MusicNotFound("不支持的播放控制")
        async with self._lock:
            session = self._require_session(session_id)
            if action in {"resume", "next"}:
                self._allow_advance = True
            if action == "end":
                # A manually selected NetEase song is observed together, not
                # owned by MIRROW's playback queue. Ending companionship must
                # not unexpectedly stop 使用者's independent player.
                if session["song"].get("origin") != "manual_in_shared_session":
                    await self._adapter("pause", session["device"], session["song"])
                session["status"] = "ended"
                session["pause_since"] = None
                self.store.event(session_id, "ended", session["device"], session["song"].get("id"), "explicit")
                self._save(session)
                self._session = None
                self._external_candidate = None
                return None
            if session["status"] == "waiting_next" or session["song"].get("origin") == "manual_in_shared_session":
                raise MusicNotFound("这首歌由网易云手动选择；请在网易云控制播放，或从 MIRROW 另选一首")
            if action == "next":
                if session["mode"] == "single" or len(session["queue"]) < 2:
                    raise MusicNotFound("当前没有下一首队列歌曲，请另选歌曲或歌单")
                if (session["queue_index"] + 1 >= len(session["queue"])
                        and session["mode"] != "loop"):
                    raise MusicNotFound("已经是列表最后一首")
                return await self._advance(session, source="explicit")
            metadata = await self._adapter(action, session["device"], session["song"])
            session["status"] = ("paused" if metadata.get("playing") is False else "unknown") if action == "pause" else ("playing" if metadata.get("playing") is True else "starting")
            session["pause_since"] = now_iso() if session["status"] == "paused" else None
            session["position_ms"] = metadata.get("position_ms", session.get("position_ms"))
            self.store.event(session_id, action, session["device"], session["song"].get("id"), "explicit")
            self._save(session)
            return self._public_session(session)

    async def update_session(self, session_id: str, quiet: bool | None = None,
                             mode: str | None = None,
                             pause_timeout_seconds: int | None = None,
                             follow_external: bool | None = None) -> dict[str, Any]:
        async with self._lock:
            session = self._require_session(session_id)
            if quiet is not None:
                session["quiet"] = bool(quiet)
            if mode is not None:
                if mode not in {"single", "list", "loop", "one"}:
                    raise MusicNotFound("循环方式无效")
                if session["status"] == "waiting_next":
                    raise MusicNotFound("上一首已经停下；请先从 MIRROW 播放歌曲或歌单，再设置播放方式")
                session["mode"] = mode
            if pause_timeout_seconds is not None:
                session["pause_timeout_seconds"] = self._pause_timeout(pause_timeout_seconds)
                if session["pause_timeout_seconds"] == 0:
                    session["pause_since"] = None
            if follow_external is not None:
                session["follow_external"] = bool(follow_external)
                if not follow_external and (
                    session["status"] == "waiting_next"
                    or session["song"].get("origin") == "manual_in_shared_session"
                ):
                    session["status"] = "ended"
                    session["pause_since"] = None
                    self.store.event(session_id, "follow_ended", session["device"],
                                     session["song"].get("id"), "explicit")
                    self._save(session)
                    self._session = None
                    self._external_candidate = None
                    return self._public_session(session)
            self._save(session)
            return self._public_session(session)

    async def observe(self, device: str, metadata: dict[str, Any], session_id: str | None = None) -> dict[str, Any] | None:
        """Accept trusted device state.  Natural progression needs explicit evidence."""
        async with self._lock:
            session = self._session
            if not session or session["device"] != device or (session_id and session["id"] != session_id):
                return self._public_session(session) if session else None
            repeated_end = metadata.get("end_token") in self._end_receipts
            if repeated_end:
                # A retained receipt describes the old boundary, not the current
                # MediaSession. Ignore stale stopped packets, but still observe
                # live playback (including a manually selected next song).
                if metadata.get("playing") is not True and not self._matches(
                    session["song"], str(metadata.get("title") or ""), str(metadata.get("artist") or "")
                ):
                    return self._public_session(session)
            # The Android fence can confirm the old track's natural boundary after
            # NetEase has already preloaded the next title.  Consume that signed,
            # session-scoped receipt before interpreting the current metadata as a
            # manual takeover; the receipt remains idempotent through ``end_token``.
            if not repeated_end and (metadata.get("end_of_track") is True or metadata.get("confirmed") == "end_of_track"):
                if not self._allow_advance:
                    session["status"] = "unknown"
                    session["pause_since"] = None
                    session["error"] = "重启后的队列等待明确继续，不会自动续播"
                    self._save(session)
                    return self._public_session(session)
                receipt = metadata.get("end_token")
                if receipt and receipt in self._end_receipts:
                    return self._public_session(session)
                if receipt:
                    self._end_receipts.add(receipt)
                return await self._advance(session, source="automatic")
            if not metadata.get("available", False):
                self._external_candidate = None
                # A native deep link can be acknowledged before an OEM publishes the
                # MediaSession.  Preserve that truthful in-between state briefly instead
                # of turning a delivered command into an immediate failure.
                session["status"] = (
                    "starting"
                    if session.get("status") == "starting" and session.get("command_dispatched")
                    and self._within_start_grace(session)
                    else "unknown"
                )
                session["pause_since"] = None
                self._save(session)
                return self._public_session(session)
            title, artist = str(metadata.get("title") or ""), str(metadata.get("artist") or "")
            if title and not self._matches(session["song"], title, artist):
                # MediaSession may continue publishing the previous song for a
                # moment after a successful deep-link dispatch. An unconfirmed
                # new command must not be cancelled by that stale observation.
                # Keep it pending; never count the old title as heard.
                if session["status"] == "starting" and self._within_start_grace(session):
                    self._external_candidate = None
                    session["error"] = "点播已送达，等待网易云确认新歌；当前设备仍显示上一首"
                    self._save(session)
                    return self._public_session(session)
                if session.get("follow_external"):
                    if metadata.get("playing") is True and artist.strip():
                        return self._observe_manual_candidate(session, metadata, title, artist)
                    self._external_candidate = None
                    if session["status"] != "waiting_next":
                        session["status"] = "unknown"
                        session["pause_since"] = None
                        session["error"] = "网易云显示另一首歌，等待实际播放确认"
                        self._save(session)
                    return self._public_session(session)
                session["status"] = "external"
                session["pause_since"] = None
                session["error"] = "网易云已切换到其他歌曲，MIRROW 已退出本次共同播放"
                self.store.event(session["id"], "external_takeover", device, session["song"].get("id"), "observed", {"title": title, "artist": artist})
                self._save(session)
                self._session = None
                return None
            if session["status"] == "waiting_next":
                if title and artist.strip() and metadata.get("playing") is True:
                    return self._observe_manual_candidate(session, metadata, title, artist)
                self._external_candidate = None
                self._save(session)
                return self._public_session(session)
            self._external_candidate = None
            if not self._allow_advance:
                session["status"] = "unknown"
                session["pause_since"] = None
                session["position_ms"] = metadata.get("position_ms")
                session["error"] = "服务重启后等待明确继续，未恢复播放托管"
                self._save(session)
                return self._public_session(session)
            if not title or not isinstance(metadata.get("playing"), bool):
                session["status"] = "unknown"
                session["pause_since"] = None
                self._save(session)
                return self._public_session(session)
            self._record_heard(session, metadata, self._source)
            session["status"] = "playing" if metadata.get("playing") else "paused"
            # A confirmed MediaSession observation supersedes any earlier
            # dispatch/pending warning.  Keeping that warning attached makes
            # the card look failed even though NetEase is audibly playing.
            session["error"] = None
            if session["status"] == "playing":
                session["pause_since"] = None
            elif not session.get("pause_since"):
                session["pause_since"] = now_iso()
            session["position_ms"] = metadata.get("position_ms")
            self._save(session)
            return self._public_session(session)

    def _observe_manual_candidate(self, session: dict[str, Any], metadata: dict[str, Any],
                                  title: str, artist: str) -> dict[str, Any]:
        """Follow only a stable playing title inside an explicit continuous session."""
        identity = (title.strip().casefold(), artist.strip().casefold())
        if self._external_candidate != identity:
            self._external_candidate = identity
            if session["status"] != "waiting_next":
                session["status"] = "unknown"
                session["pause_since"] = None
                session["error"] = "发现网易云新歌，等待第二次播放确认"
                self._save(session)
            return self._public_session(session)
        self._external_candidate = None
        try:
            duration = max(0, int(metadata.get("duration_ms") or 0))
        except (TypeError, ValueError):
            duration = 0
        song = {
            "id": "", "name": title.strip(), "artist": artist.strip(),
            "duration": duration,
            "origin": "manual_in_shared_session",
        }
        previous_id = str(session["song"].get("id") or "")
        session.update(song=song, queue=[song], queue_index=0, playlist_id=None,
                       mode="single", status="playing", position_ms=metadata.get("position_ms"),
                       pause_since=None, error=None)
        self._episode = uuid.uuid4().hex
        self._source = "manual_shared"
        self.store.event(session["id"], "external_followed", session["device"],
                         previous_id, "observed", {"title": song["name"], "artist": song["artist"]})
        self._record_heard(session, metadata, "manual_shared")
        self._save(session)
        return self._public_session(session)

    async def expire_paused(self, session_id: str | None = None,
                            now: datetime | str | None = None) -> bool:
        """End a session only after a continuously confirmed pause interval.

        Observation loss changes the session to ``unknown`` and clears the
        interval, so a stale receipt can never manufacture an automatic end.
        This closes MIRROW's listening state only; the remote app is already
        paused and is not commanded again.
        """
        async with self._lock:
            session = self._session
            if not session or (session_id and session["id"] != session_id):
                return False
            timeout = int(session.get("pause_timeout_seconds") or 0)
            paused_at = session.get("pause_since")
            if session.get("status") not in {"paused", "waiting_next"} or timeout <= 0 or not paused_at:
                return False
            current = datetime.fromisoformat(now) if isinstance(now, str) else (now or datetime.fromisoformat(now_iso()))
            try:
                elapsed = (current - datetime.fromisoformat(paused_at)).total_seconds()
            except (TypeError, ValueError):
                session["pause_since"] = now_iso()
                self._save(session)
                return False
            if elapsed < timeout:
                return False
            session["status"] = "ended"
            session["pause_since"] = None
            session["error"] = None
            self.store.event(
                session["id"], "paused_timeout", session["device"],
                session["song"].get("id"), "observed", {"timeout_seconds": timeout},
            )
            self._save(session)
            self._session = None
            return True

    def playback_receipt(self, session_id: str, song_id: str | None = None) -> dict[str, Any]:
        """Project durable playback evidence for Wander recovery.

        A duration estimate is deliberately absent.  Completion is true only
        when the music authority has recorded a device-backed track boundary.
        """
        session = self.store.session(session_id)
        if not session:
            return {"state": "missing", "session_id": str(session_id), "song_id": str(song_id or "")}
        expected_song_id = str(song_id or "")
        events = self.store.events_for_session(session_id)
        for event in reversed(events):
            if (event["event_type"] not in {"finished", "track_finished"}
                    or event.get("source") != "automatic"):
                continue
            if expected_song_id and str(event.get("song_id") or "") != expected_song_id:
                continue
            return {
                "state": "finished", "session_id": str(session_id),
                "song_id": str(event.get("song_id") or expected_song_id),
                "occurred_at": event.get("occurred_at"), "event_type": event["event_type"],
            }
        state = str(session.get("status") or "unknown")
        terminal_event = next((event for event in reversed(events) if event["event_type"] in {
            "paused_timeout", "ended", "external_takeover"
        }), None)
        if terminal_event:
            state = {
                "paused_timeout": "paused_timeout",
                "external_takeover": "external",
                "ended": "ended",
            }[terminal_event["event_type"]]
        current_song_id = str((session.get("song") or {}).get("id") or "")
        if expected_song_id and current_song_id and current_song_id != expected_song_id and state not in {
            "ended", "external", "failed", "superseded", "paused_timeout"
        }:
            state = "song_mismatch"
        return {
            "state": state, "session_id": str(session_id), "song_id": expected_song_id,
            "observed_at": session.get("observed_at"), "position_ms": session.get("position_ms"),
        }

    async def _advance(self, session: dict[str, Any], source: str) -> dict[str, Any] | None:
        queue = session["queue"]
        index = session["queue_index"]
        if session["mode"] == "single":
            # A natural-end receipt means the device fence has already paused.
            # Explicit next is rejected by ``control`` before reaching here.
            if source != "automatic":
                await self._adapter("pause", session["device"], session["song"])
            session["status"] = "waiting_next" if source == "automatic" and session.get("follow_external") else "ended"
            session["pause_since"] = now_iso() if session["status"] == "waiting_next" else None
            self.store.event(
                session["id"], "finished" if source == "automatic" else "ended",
                session["device"], session["song"].get("id"), source,
            )
            self._save(session)
            if session["status"] == "waiting_next":
                return self._public_session(session)
            self._session = None
            return None
        if session["mode"] == "one" and source == "automatic":
            next_index = index
        elif index + 1 < len(queue):
            next_index = index + 1
        elif session["mode"] == "loop":
            next_index = 0
        else:
            if source != "automatic":
                raise MusicNotFound("已经是列表最后一首")
            session["status"] = "ended"
            session["pause_since"] = None
            self.store.event(session["id"], "finished", session["device"], session["song"].get("id"), source)
            self._save(session); self._session = None
            return None
        self.store.event(
            session["id"], "track_finished" if source == "automatic" else "skipped",
            session["device"], session["song"].get("id"), source,
        )
        next_song = queue[next_index]
        metadata = await self._adapter("play", session["device"], next_song)
        self._episode = uuid.uuid4().hex
        self._source = source
        session.update(song=next_song, queue_index=next_index, status="playing" if self._confirmed(next_song, metadata) else "starting",
                       position_ms=metadata.get("position_ms"), error=None, pause_since=None,
                       commanded_at=now_iso(), command_dispatched=bool(metadata.get("dispatched")))
        self.store.event(session["id"], "advanced", session["device"], next_song.get("id"), source,
                         {"automatic_loop": source == "automatic" and next_index <= index})
        self._record_heard(session, metadata, source)
        self._save(session)
        return self._public_session(session)

    def _record_heard(self, session, metadata, source):
        if metadata.get("playing") is not True or not metadata.get("available") or not metadata.get("title"):
            return
        if not self._matches(session["song"], metadata["title"], str(metadata.get("artist") or "")):
            return
        if self._episode in self._heard_episodes: return
        self._heard_episodes.add(self._episode)
        self.store.event(session["id"], "heard", session["device"], session["song"].get("id"), source,
                         {"song": session["song"], "subject": session.get("subject", "shared"), "episode": self._episode})

    def _confirmed(self, song, metadata):
        return (metadata.get("playing") is True and metadata.get("available") is True
                and bool(metadata.get("title")) and self._matches(song, metadata["title"], str(metadata.get("artist") or "")))

    @staticmethod
    def _within_start_grace(session: dict[str, Any]) -> bool:
        try:
            age = (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(
                session.get("commanded_at") or session["observed_at"]
            )).total_seconds()
        except (TypeError, ValueError, KeyError):
            return False
        return 0 <= age <= START_CONFIRMATION_GRACE_SECONDS

    def _require_session(self, session_id: str) -> dict[str, Any]:
        if not self._session or self._session["id"] != session_id:
            raise SessionConflict("播放会话已变化，请刷新后再操作")
        return self._session

    def _save(self, session: dict[str, Any]) -> None:
        session["observed_at"] = now_iso(); self.store.save_session(session)

    @staticmethod
    def _pause_timeout(value: int | None) -> int:
        if value is None:
            return DEFAULT_PAUSE_TIMEOUT_SECONDS
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            raise MusicNotFound("暂停结束时间无效") from None
        if timeout < 0 or timeout > MAX_PAUSE_TIMEOUT_SECONDS:
            raise MusicNotFound("暂停结束时间无效")
        return timeout

    @staticmethod
    def _matches(song: dict[str, Any], title: str, artist: str) -> bool:
        import unicodedata
        def normalized(value):
            return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())
        expected = {normalized(song.get("name") or ""), *[normalized(t) for t in song.get("title_aliases", [])]}
        actual = normalized(title)
        expected_artist, actual_artist = str(song.get("artist") or "").casefold(), artist.strip().casefold()
        return bool(actual) and actual in expected and (not expected_artist or not actual_artist or expected_artist in actual_artist or actual_artist in expected_artist)

    @staticmethod
    def _public_session(session: dict[str, Any] | None) -> dict[str, Any] | None:
        if not session:
            return None
        fields = ("id", "device", "mode", "quiet", "follow_external", "status", "song", "queue", "queue_index", "subject", "observed_at", "commanded_at", "position_ms", "error", "pause_since", "pause_timeout_seconds", "playlist_id")
        return {field: session.get(field) for field in fields}


def get_service() -> MusicService:
    global _service
    if _service is None:
        _service = MusicService()
    return _service


def configure(adapter: Adapter | None = None) -> MusicService:
    service = get_service(); service.configure(adapter); return service
