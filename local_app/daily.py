"""Recoverable daily digestion using source-validated public domain modules."""
import asyncio
import json
from cognition import daily
from .day_store import STAGES
from mirrow_core.model_provenance import day_model_evidence, PROVENANCE_GUIDE


class DailyDigestion:
    def __init__(self, store, days, memory, provider):
        self.store, self.days, self.memory, self.provider = store, days, memory, provider
        self.lock = asyncio.Lock()

    async def cognition(self, day, rows):
        async def model(prompt):
            self.days.verify(day)
            result = await self.provider.complete([{'role': 'user', 'content': prompt}], internal=True)
            self.days.verify(day)
            return result['content']

        async def lookup(evidence):
            text = '\n'.join(str(item.get('text') or item.get('content', '')) for item in evidence if isinstance(item, dict))
            return await self.memory.search(text) if text else []

        # Optional social integrations supply the same attributed evidence contract.
        extra = [e for event in self.days.events(day) for e in event['payload'].get('cognition_evidence', [])]
        activity_rows = []
        for event in self.days.events(day):
            if event['module'] != 'wander':
                continue
            payload = event['payload']
            activity_rows.append({'message_id':event['id'], 'role':'system', 'event_type':'wander_activity',
                'timestamp':event['occurred_at'], 'content':payload.get('summary') or '本次没有留下主观结算感想',
                'tool_calls':[{'description': json.dumps({'activity_type':payload.get('event_type'),
                    'state':payload.get('state'), 'delivery':payload.get('delivery')}, ensure_ascii=False)}]})
        result = await daily.run_daily([*rows, *activity_rows], model, source_date=day, session_id=self.store.session_id,
            memory_lookup=lookup, other_evidence=extra)
        return {'status': result['status'], 'lanes': result['lanes']}

    async def diary(self, day, rows):
        events = self.days.events(day)
        if not rows and not events:
            return {'status': 'no_source_messages', 'text': ''}
        packet = {'active_date': day, 'messages': [{k: r[k] for k in ('message_id', 'role', 'content', 'timestamp', 'event_type', 'tool_calls')}
            for r in rows], 'confirmed_module_events': events}
        packet['model_provenance'] = day_model_evidence(rows)
        result = await self.provider.structured(messages=[
            {'role': 'system', 'content': '根据这一天的原始材料写一篇第一人称日记。区分自己、伙伴和朋友的言行，保留未确定的事。后端记录是经历来源；未分享的活动不等于对方听过。输出 JSON：text（日记正文），source_message_ids（所用原话的真实消息 ID 数组），source_event_ids（所用模块事件的真实 ID 数组）。至少引用一项真实来源。没有发生的活动不补写。' + PROVENANCE_GUIDE},
            {'role': 'user', 'content': json.dumps(packet, ensure_ascii=False)}], max_tokens=8192)
        value = result.parsed or {}
        ids = value.get('source_message_ids')
        event_ids = value.get('source_event_ids', [])
        known = {r['message_id'] for r in rows}
        known_events = {event['id'] for event in events}
        if (not isinstance(value.get('text'), str) or not value['text'].strip()
                or not isinstance(ids, list) or not isinstance(event_ids, list) or not (ids or event_ids)
                or any(not isinstance(i, str) or i not in known for i in ids)
                or any(not isinstance(i, str) or i not in known_events for i in event_ids)):
            raise ValueError('diary_source_contract_invalid')
        return {'status': 'completed', 'text': value['text'], 'source_message_ids': list(dict.fromkeys(ids)),
            'source_event_ids': list(dict.fromkeys(event_ids))}

    async def run(self, day, *, retry=False):
        async with self.lock:
            try:
                rows = self.days.verify(day)
            except ValueError:
                for stage in STAGES:
                    previous = self.days.result(day, stage)
                    if previous and previous['state'] != 'completed':
                        self.days.update(day, stage, 'review_required', {'status': 'source_changed'})
                return
            for stage in STAGES:
                previous = self.days.result(day, stage)
                if previous['state'] == 'completed' or (previous['state'] in {'error', 'review_required'} and not retry):
                    continue
                self.days.update(day, stage, 'running')
                try:
                    self.days.verify(day)
                    if stage == 'memory':
                        result = await self.memory.encode(day)
                        success = result['status'] in {'completed', 'committed', 'cached', 'skipped', 'no_source_messages'}
                    elif stage == 'cognition':
                        result = await self.cognition(day, rows) if rows or self.days.events(day) else {'status': 'no_source_messages'}
                        success = result['status'] in {'completed', 'no_source_messages'}
                    else:
                        result = await self.diary(day, rows)
                        success = True
                    self.days.verify(day)
                    self.days.update(day, stage, 'completed' if success else 'review_required', result)
                except asyncio.CancelledError:
                    self.days.update(day, stage, 'pending', {'status': 'interrupted'})
                    raise
                except Exception:
                    self.days.update(day, stage, 'error', {'status': 'failed', 'detail': '本步未完成，已保存的材料保留，可重试。'})
