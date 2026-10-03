"""Bounded lifecycle scheduling for optional song material enrichment.

Playback remains truthful even when lyrics or objective melody analysis are not
available.  This scheduler therefore keeps enrichment independent from the
playback loop, but does not let a transient failure permanently mark a song as
handled for the lifetime of the backend process.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .materials import prepare

log = logging.getLogger(__name__)


@dataclass
class _Attempt:
    attempts: int = 0
    ready: bool = False
    retry_after: float = 0.0
    task: asyncio.Task | None = None


class MaterialPreparationScheduler:
    """One bounded material preparation sequence per provider song ID.

    ``max_attempts`` counts the initial preparation as well: a song can make at
    most that many attempts in one backend process.  A partial result is kept
    by the material cache, while this scheduler can retry it after a short
    cooldown until both independently useful fields are available.
    """

    def __init__(self, *, cooldown_seconds: float = 30.0, max_attempts: int = 3,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.max_attempts = max_attempts
        self._clock = clock
        self._attempts: dict[str, _Attempt] = {}

    @staticmethod
    def _ready(material: dict | None) -> bool:
        material = material or {}
        return bool(str(material.get("lyrics") or "").strip()
                    and str(material.get("melody_summary") or "").strip())

    def schedule(self, song: dict) -> bool:
        """Queue preparation when eligible, returning whether work is pending.

        The caller intentionally does not await this method: observing a real
        device state must never be delayed by optional network/audio work.
        """
        song_id = str(song.get("id") or "")
        if not song_id:
            return False
        state = self._attempts.setdefault(song_id, _Attempt())
        if state.ready or (state.task and not state.task.done()):
            return False
        if state.attempts >= self.max_attempts or self._clock() < state.retry_after:
            return False

        state.attempts += 1
        task = asyncio.create_task(self._run(song_id, dict(song)))
        state.task = task
        task.add_done_callback(lambda completed, sid=song_id: self._finished(sid, completed))
        return True

    async def _run(self, song_id: str, song: dict) -> None:
        try:
            result = await prepare(song, analyze=True)
        except asyncio.CancelledError:
            # The callback refunds even cancellation before this coroutine starts.
            raise
        except Exception:
            result = None
            log.info("音乐资料暂不可用，播放事实保留")

        state = self._attempts.get(song_id)
        if not state:
            return
        if self._ready(result):
            state.ready = True
            state.retry_after = 0.0
        else:
            state.retry_after = self._clock() + self.cooldown_seconds

    def _finished(self, song_id: str, completed: asyncio.Task) -> None:
        state = self._attempts.get(song_id)
        if state and state.task is completed:
            state.task = None
            if completed.cancelled():
                state.attempts = max(0, state.attempts - 1)
        # _run handles ordinary preparation errors to preserve the playback
        # loop.  Retrieve unexpected exceptions so asyncio does not log them as
        # unobserved task failures.
        if not completed.cancelled():
            try:
                completed.result()
            except Exception:
                pass

    async def stop(self) -> None:
        tasks = [state.task for state in self._attempts.values()
                 if state.task and not state.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._attempts.clear()


_scheduler = MaterialPreparationScheduler()


def schedule(song: dict) -> bool:
    return _scheduler.schedule(song)


async def stop() -> None:
    await _scheduler.stop()
