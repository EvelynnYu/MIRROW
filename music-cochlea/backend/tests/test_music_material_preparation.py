"""Offline contracts for bounded lifecycle material preparation."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from music_system.material_preparation import MaterialPreparationScheduler


SONG = {"id": "song-1", "name": "A", "artist": "K"}


class MaterialPreparationSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.scheduler = MaterialPreparationScheduler(
            cooldown_seconds=30, max_attempts=3, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.scheduler.stop()

    async def _settle(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_partial_result_retries_after_cooldown_then_stays_ready(self):
        prepare = AsyncMock(side_effect=[
            {"lyrics": "lyric only", "melody_summary": ""},
            {"lyrics": "lyric", "melody_summary": "tempo and contour"},
        ])
        with patch("music_system.material_preparation.prepare", prepare):
            self.assertTrue(self.scheduler.schedule(SONG))
            await self._settle()
            self.assertFalse(self.scheduler.schedule(SONG))
            self.now += 30
            self.assertTrue(self.scheduler.schedule(SONG))
            await self._settle()
            self.assertFalse(self.scheduler.schedule(SONG))
        self.assertEqual(2, prepare.await_count)

    async def test_failure_is_bounded_to_three_total_attempts(self):
        prepare = AsyncMock(return_value={"lyrics": "", "melody_summary": ""})
        with patch("music_system.material_preparation.prepare", prepare):
            for _ in range(3):
                self.assertTrue(self.scheduler.schedule(SONG))
                await self._settle()
                self.now += 30
            self.assertFalse(self.scheduler.schedule(SONG))
        self.assertEqual(3, prepare.await_count)

    async def test_concurrent_observations_create_one_task(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_prepare(song, *, analyze):
            started.set()
            await release.wait()
            return {"lyrics": "lyric", "melody_summary": "summary"}

        with patch("music_system.material_preparation.prepare", slow_prepare):
            self.assertTrue(self.scheduler.schedule(SONG))
            await started.wait()
            self.assertFalse(self.scheduler.schedule(SONG))
            release.set()
            await self._settle()

    async def test_exception_retries_and_cancellation_before_start_refunds_budget(self):
        prepare = AsyncMock(side_effect=[RuntimeError('offline'), {'lyrics': 'text', 'melody_summary': 'summary'}])
        with patch('music_system.material_preparation.prepare', prepare):
            self.scheduler.schedule(SONG)
            self.scheduler._attempts['song-1'].task.cancel()
            await self._settle()
            self.assertEqual(0, self.scheduler._attempts['song-1'].attempts)
            self.assertTrue(self.scheduler.schedule(SONG))
            await self._settle()
            self.assertFalse(self.scheduler.schedule(SONG))
            self.now += 30
            self.assertTrue(self.scheduler.schedule(SONG))
            await self._settle()
            self.assertTrue(self.scheduler._attempts['song-1'].ready)

    async def test_cancelled_attempt_does_not_consume_budget_and_stop_clears_state(self):
        started = asyncio.Event()

        async def blocked_prepare(song, *, analyze):
            started.set()
            await asyncio.Event().wait()

        with patch("music_system.material_preparation.prepare", blocked_prepare):
            self.assertTrue(self.scheduler.schedule(SONG))
            await started.wait()
            state = self.scheduler._attempts["song-1"]
            state.task.cancel()
            await self._settle()
            self.assertEqual(0, state.attempts)
            self.assertTrue(self.scheduler.schedule(SONG))
            await self.scheduler.stop()
            self.assertEqual({}, self.scheduler._attempts)
