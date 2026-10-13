"""Public sentinel host: real observations, grounded decisions, durable delivery.

Built-in observations are declared status and this browser's visibility. Device
adapters may register timestamped DataSnapshot readers in this process. No
production relay, location file, account or device is discovered automatically.
"""
import asyncio
from dataclasses import fields
from datetime import datetime
import hashlib
import inspect
import json
import time
from silicon_perception.analysis.anomaly_detector import DataSnapshot
from silicon_perception.analysis.trigger_detector import TriggerDetector
from silicon_perception.analysis.sentinel_summarizer import SentinelSummarizer
from silicon_perception.analysis.sentinel_prompt import sentinel_messages
from silicon_perception.analysis.sentinel_grounding import build_sentinel_fact_contract, natural_care_motivation
from .context_sources import history_messages


class Sentinel:
    def __init__(self, store, provider, life):
        self.store, self.provider, self.life = store, provider, life
        self.sources = {}
        self.task = None
        self.lock = asyncio.Lock()
        self.detector = TriggerDetector()
        self.summarizer = SentinelSummarizer(self.observe_model)
        self.latest = None
        self.error = ''
        last = store.setting('sentinel_last_observation')
        if last:
            age = max(0, time.time() - float(last))
            self.detector._last_flash_time = time.monotonic() - age

    def register_source(self, name, read_snapshot):
        if not name or not callable(read_snapshot):
            raise ValueError('invalid_observation_source')
        self.sources[name] = read_snapshot

    def current_user_id(self):
        return next((r['message_id'] for r in reversed(self.store.messages()) if r['role'] == 'user'), '')

    async def observe_model(self, prompt):
        return (await self.provider.complete(sentinel_messages(prompt), internal=True))['content']

    async def snapshot(self):
        now = datetime.now().astimezone()
        status = self.life.status()
        snapshot = DataSnapshot(timestamp=now.isoformat(), user_status=status['status'],
            user_status_since=status.get('recorded_at'))
        last = self.store.setting('last_user_at')
        if last:
            snapshot.last_message_seconds = max(0, (now - datetime.fromisoformat(last)).total_seconds())
        presence = self.store.setting('browser_presence')
        if presence:
            age = (now - datetime.fromisoformat(presence['recorded_at'])).total_seconds()
            if age < 150:
                snapshot.mirrow_visible = presence['visible']
                snapshot.field_ts['mirrow_visible'] = presence['recorded_at']
        allowed = {f.name for f in fields(DataSnapshot)} - {'user_status', 'user_status_since', 'last_message_seconds', 'timestamp'}
        for name, reader in self.sources.items():
            try:
                values = reader()
                if inspect.isawaitable(values):
                    values = await values
                if not isinstance(values, dict):
                    raise ValueError('snapshot_dict_required')
                for key in allowed & values.keys():
                    if key == 'field_ts':
                        snapshot.field_ts.update(values[key])
                    else:
                        setattr(snapshot, key, values[key])
            except Exception:
                # No stale cached success from an adapter that failed this tick.
                self.error = '部分感知来源暂不可用'
        return snapshot

    async def tick(self):
        if not self.life.settings().sentinel_enabled or not self.provider.config.configured or not self.store.profile()['persona'].strip():
            return
        async with self.lock:
            snapshot = await self.snapshot()
            triggers = self.detector.evaluate(snapshot)
            self.latest = snapshot
            if not triggers or not self.life.can_push():
                return
            user_id = self.current_user_id()
            active = self.life.days.active()['date']
            rows, _ = self.life.memory.context_sources.select_history()
            index = {f'H{i}': {k: r[k] for k in ('message_id', 'role', 'content', 'timestamp')}
                     for i, r in enumerate(rows, 1) if r['role'] in {'user', 'assistant'}}
            historical = '\n'.join(f"[{key}] {json.dumps(row, ensure_ascii=False)}" for key, row in index.items())
            self.store.set_setting('sentinel_last_observation', time.time())
            await self.summarizer.update(snapshot, [], {}, snapshot.last_message_seconds,
                triggers=triggers, current_time=snapshot.timestamp, status_timeline=json.dumps(self.life.status(), ensure_ascii=False),
                recent_conversation=historical, conversation_evidence=index)
            decision = self.summarizer.get_decision().to_dict()
            self.detector.record_flash_decision(snapshot, triggers, decision['should_push'])
            if not decision['should_push'] or user_id != self.current_user_id() or not self.life.can_push():
                return
            old_contract = self.summarizer.get_fact_contract()
            current = build_sentinel_fact_contract(await self.snapshot())
            if current.location_detail_changed_from(old_contract):
                # A deferred decision must not replay a place the person has left.
                decision['observation'] = ''
                decision['push_motivation'] = natural_care_motivation(should_push=True)
            facts = await self.life.memory.context_sources.facts(active_date=active)
            facts.update(current_observations=current.to_dict(),
                subjective_contact_decision={'should_push': True,
                    'subjective_motivation': decision.get('push_motivation', '')},
                history_evidence=decision.get('verified_history_evidence', []))
            messages = [*history_messages(rows), {'role': 'system', 'name': 'mirrow_sources',
                'content': json.dumps(facts, ensure_ascii=False)},
                {'role': 'user', 'content': '【后端主动联系请求】本次没有新的用户发言。结合当前观测、经过验证的历史和自己的联系念头，写一条自然的主动消息。联系念头是主观判断，不能当作对方说过的话。直接输出正文。'}]
            result = await self.provider.complete(messages, internal=True)
            text = result['content'].strip()
            newest = build_sentinel_fact_contract(await self.snapshot())
            if newest.location_detail_changed_from(current) or not newest.validate_text(text).accepted:
                return  # next real observation may create a fresh decision
            if user_id != self.current_user_id() or not self.life.can_push():
                return
            identity = hashlib.sha256(json.dumps([active, user_id, snapshot.timestamp, triggers], ensure_ascii=False).encode()).hexdigest()
            async with self.life.chat.lock:
                if user_id != self.current_user_id() or not self.life.days.active() or self.life.days.active()['date'] != active:
                    return
                if self.life.status()['status'] in {'sleeping', 'napping', 'bathing'}:
                    return
                self.store.append('assistant', text, message_id='sentinel_' + identity,
                    event_type='sentinel', sentinel=True, active_date=active)
                self.store.set_setting('sentinel_delivery', {'message_id': 'sentinel_' + identity,
                    'state': 'delivered', 'recorded_at': datetime.now().astimezone().isoformat()})

    async def loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = '本轮守望未完成，等待下一次真实观测。'
            await asyncio.sleep(60)

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def state(self):
        return {'enabled': self.life.settings().sentinel_enabled,
            'sources': ['声明状态', '本页面可见性', *self.sources.keys()], 'error': self.error,
            'last_delivery': self.store.setting('sentinel_delivery')}
