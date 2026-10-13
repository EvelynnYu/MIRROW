"""One local day clock shared by chat, digestion and autonomous activities."""
import asyncio
from datetime import datetime, timedelta
from .day_store import DayStore
from .daily import DailyDigestion
from .preferences import Preferences, in_window


class Lifecycle:
    def __init__(self, store, memory, provider, wander, chat, *, clock=None):
        self.store, self.memory, self.provider, self.wander, self.chat = store, memory, provider, wander, chat
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.days = DayStore(store)
        self.daily = DailyDigestion(store, self.days, memory, provider)
        self.task = self.digest_task = None
        self.error = ''
        memory.days = self.days
        store.active_date_provider = self.active_date
        wander.lifecycle = self
        chat.lifecycle = self
        # Existing messages keep their original date; no history migration.
        rows = store.messages()
        if rows and not self.days.active() and not self.days.get(rows[-1]['active_date']):
            self.days.open(rows[-1]['active_date'], self.clock())

    def settings(self):
        return Preferences(**self.store.setting('lifecycle_preferences', {}))

    def configure(self, preferences):
        self.store.set_setting('lifecycle_preferences', preferences.model_dump())
        self.store.set_setting('day_reservation', None)

    def status(self):
        return self.store.setting('user_status', {'status': 'unknown', 'detail': '', 'source': 'unknown', 'recorded_at': None})

    def set_status(self, status, detail='', source='explicit'):
        from wander_manager.user_status import UserStatus
        if status not in {'unknown', *(item.value for item in UserStatus)}:
            raise ValueError('unknown_status')
        previous = self.status()
        if (previous['status'], previous['detail'], previous['source']) == (status, detail, source):
            return previous
        now = self.clock().isoformat()
        value = {'status': status, 'detail': detail, 'source': source, 'recorded_at': now}
        self.store.set_setting('user_status', value)
        active = self.days.active()
        with self.store.connection() as db:
            db.execute('INSERT INTO local_status_history(recorded_at,active_date,status,detail,source) VALUES(?,?,?,?,?)',
                (now, active['date'] if active else None, status, detail, source))
        return value

    def active_date(self):
        active = self.days.active()
        if active:
            return active['date']
        day = self.clock().date().isoformat()
        self.days.open(day, self.clock())
        return day

    def can_push(self):
        return bool(self.days.active()) and self.status()['status'] not in {'sleeping', 'napping', 'bathing'} and not self.chat.lock.locked()

    async def before_message(self):
        # A restart / busy foreground can delay the timer past its due time.
        reservation = self.store.setting('day_reservation')
        if reservation and self.clock() >= datetime.fromisoformat(reservation['due_at']):
            await self.close(reservation['reason'])
        # New conversation cancels an unexpired reservation, not a closed day.
        self.store.set_setting('day_reservation', None)
        await self.check_rollover()
        self.active_date()
        if self.status()['status'] in {'sleeping', 'napping'}:
            self.set_status('idle', '', 'chat_return')

    def accepted_message(self, content):
        now, settings = self.clock(), self.settings()
        self.store.set_setting('last_user_at', now.isoformat())
        if settings.covenant_word and settings.covenant_word in content and in_window(now, settings.window_start, settings.window_end):
            self.reserve('covenant', delay_minutes=settings.wait_minutes)

    def reserve(self, reason='manual', delay_minutes=0):
        active = self.days.active()
        if not active:
            return None
        now = self.clock()
        # A date is immutable once digested. Before midnight reserve closure;
        # this also keeps late-night assistant events in their real source day.
        boundary = datetime.fromisoformat(active['date']).replace(tzinfo=now.tzinfo) + timedelta(days=1)
        due = max(now + timedelta(minutes=delay_minutes), boundary)
        reservation = {'active_date': active['date'], 'reason': reason, 'due_at': due.isoformat(),
            'deferred_to_midnight': due == boundary and boundary > now}
        self.store.set_setting('day_reservation', reservation)
        return reservation

    async def close(self, reason):
        active = self.days.active()
        if not active:
            return
        # The caller holds the foreground lock. Finish actual activity effects
        # before recording the immutable day boundary.
        await self.wander.stop()
        self.days.close(active['date'], reason, self.clock())
        self.store.set_setting('day_reservation', None)
        if reason in {'manual', 'covenant'}:
            self.set_status('sleeping', '', 'day_end')

    async def check_rollover(self):
        active, now = self.days.active(), self.clock()
        if active and active['date'] < now.date().isoformat() and now.hour >= self.settings().day_change_hour:
            await self.close('day_change')

    async def tick(self):
        if not self.chat.lock.locked():
            async with self.chat.lock:
                reservation = self.store.setting('day_reservation')
                if reservation and self.clock() >= datetime.fromisoformat(reservation['due_at']):
                    await self.close(reservation['reason'])
                else:
                    await self.check_rollover()
        ready = self.provider.config.configured and bool(self.store.profile()['persona'].strip())
        if ready and self.settings().automatic_digest and (self.digest_task is None or self.digest_task.done()):
            for day in reversed(self.days.list()):
                if day['state'] == 'closed' and any(job and job['state'] == 'pending' for job in day['jobs']):
                    self.digest_task = asyncio.create_task(self.daily.run(day['date']))
                    break
        last = self.store.setting('last_user_at')
        if ready and self.settings().autonomous_wander and last and self.can_push() and not self.wander.error:
            idle = (self.clock() - datetime.fromisoformat(last)).total_seconds()
            if idle >= self.settings().idle_minutes * 60:
                async with self.chat.lock:
                    if self.days.active() and self.status()['status'] not in {'sleeping', 'napping', 'bathing'}:
                        await self.wander.start()

    async def loop(self):
        while True:
            try:
                await self.tick()
                self.error = ''
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = '日常任务暂未完成，请查看日结记录。'
            await asyncio.sleep(2)

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def stop(self):
        for task in (self.task, self.digest_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.task = self.digest_task = None

    def snapshot(self):
        return {'preferences': self.settings().model_dump(), 'user_status': self.status(),
            'active_day': self.days.active(), 'reservation': self.store.setting('day_reservation'),
            'days': self.days.list(), 'error': self.error}
