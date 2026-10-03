import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from music_system.errors import SessionConflict
from music_system.service import MusicService
from music_system.store import MusicStore


SONG_A = {"id": "1", "name": "A", "artist": "K", "album": "", "cover": "", "duration": 1000, "link": ""}
SONG_B = {"id": "2", "name": "B", "artist": "K", "album": "", "cover": "", "duration": 1000, "link": ""}


class MusicSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        async def adapter(action, device, song=None):
            self.calls.append((action, device, (song or {}).get("id")))
            return {"playing": action in {"play", "resume"}, "position_ms": 0}
        self.store = MusicStore(Path(self.tmp.name) / "music.sqlite")
        self.service = MusicService(store=self.store, adapter=adapter)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_single_requires_explicit_end_and_then_stops(self):
        session = await self.service.start_song(SONG_A, "mobile", "single")
        await self.service.observe("mobile", {"available": True, "playing": True, "title": "A", "artist": "K", "position_ms": 999})
        self.assertEqual(self.service.status()["session"]["id"], session["id"])
        await self.service.observe("mobile", {"available": True, "playing": False, "title": "A", "artist": "K", "end_of_track": True})
        self.assertIsNone(self.service.status()["session"])
        self.assertNotIn(("pause", "mobile", "1"), self.calls)
        self.assertEqual("finished", self.service.playback_receipt(session["id"], "1")["state"])
        self.assertEqual("finished", self.service.playback_receipt(session["id"], "1")["state"])

    async def test_continuous_confirmed_pause_expires_without_remote_command(self):
        session = await self.service.start_song(SONG_A, "mobile", "single", pause_timeout_seconds=60)
        paused = await self.service.control("pause", session["id"])
        paused_at = datetime.fromisoformat(paused["pause_since"])
        calls_before_expiry = list(self.calls)
        self.assertFalse(await self.service.expire_paused(session["id"], paused_at + timedelta(seconds=59)))
        self.assertTrue(await self.service.expire_paused(session["id"], paused_at + timedelta(seconds=60)))
        self.assertEqual(calls_before_expiry, self.calls)
        self.assertIsNone(self.service.status()["session"])
        self.assertEqual("paused_timeout", self.service.playback_receipt(session["id"], "1")["state"])

    async def test_unknown_observation_cancels_pause_interval(self):
        session = await self.service.start_song(SONG_A, "mobile", "single", pause_timeout_seconds=1)
        paused = await self.service.control("pause", session["id"])
        paused_at = datetime.fromisoformat(paused["pause_since"])
        await self.service.observe("mobile", {"available": False}, session["id"])
        self.assertFalse(await self.service.expire_paused(session["id"], paused_at + timedelta(hours=1)))
        current = self.service.status()["session"]
        self.assertEqual("unknown", current["status"])
        self.assertIsNone(current["pause_since"])

    async def test_loop_moves_only_on_confirmed_end(self):
        session = await self.service.start_song(SONG_A, "computer", "loop", [SONG_A, SONG_B])
        await self.service.observe("computer", {"available": True, "playing": True, "title": "A", "artist": "K"})
        self.assertEqual(self.service.status()["session"]["queue_index"], 0)
        await self.service.observe("computer", {"available": True, "playing": False, "title": "A", "artist": "K", "end_of_track": True})
        self.assertEqual(self.service.status()["session"]["song"]["id"], "2")
        events = self.store.events_for_day(self.store.active_session()["observed_at"][:10])
        self.assertTrue(any(event["event_type"] == "advanced" for event in events))

    async def test_external_song_exits_without_cross_device_fallback(self):
        await self.service.start_song(SONG_A, "mobile", "single")
        await self.service.observe("mobile", {"available": True, "playing": True,
                                              "title": "A", "artist": "K"})
        result = await self.service.observe("mobile", {"available": True, "playing": True, "title": "else", "artist": "other"})
        self.assertIsNone(result)
        self.assertIsNone(self.service.status()["session"])
        self.assertNotIn(("play", "computer", "1"), self.calls)

    async def test_control_requires_current_session(self):
        session = await self.service.start_song(SONG_A, "mobile", "single")
        with self.assertRaises(SessionConflict):
            await self.service.control("pause", "old-" + session["id"])

    async def test_restart_marks_prior_session_unknown(self):
        await self.service.start_song(SONG_A, "mobile", "single")
        restarted = MusicStore(self.store.path)
        previous = restarted.active_session()
        self.assertEqual(previous["status"], "unknown")

    async def test_shared_song_catalog_counts_real_sources_once(self):
        first = self.store.record_song_share(SONG_A, "message:one", "owner")
        duplicate = self.store.record_song_share(SONG_A, "message:one", "owner")
        second = self.store.record_song_share(SONG_A, "generation:two", "k")
        self.assertEqual((1, 1, 2), (
            first["share_number"], duplicate["share_number"], second["share_number"],
        ))
        found = self.store.search_shared_songs("A")
        self.assertEqual("1", found[0]["id"])
        self.assertEqual(2, found[0]["share_count"])


if __name__ == "__main__":
    unittest.main()
