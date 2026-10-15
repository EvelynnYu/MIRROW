"""Real persisted Wander runner; only configured local handlers are admitted."""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace

from wander_manager.event_types import EventType
from wander_manager.node_execution_adapter import NodeExecutionAdapter
from wander_manager.plan_decision_adapter import PlanDecisionAdapter, RuntimeContext
from wander_manager.runtime_controller import WanderRuntimeController
from wander_manager.runtime_decision_adapters import DecisionContext, NodeReviewAdapter, SettlementAdapter
from wander_manager.runtime_runner import RuntimeContextBundle, WanderRuntimeRunner
from wander_manager.runtime_share_adapter import RuntimeShareAdapter
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.self_reflection_adapter import SelfReflectionAdapter, SelfReflectionContext
from wander_manager.wish_commit_adapter import WishCommitAdapter
from wander_manager.wish_store import WishStore
from .context import runtime_context


class LocalHandlers:
    def __init__(self, owner):
        self.owner = owner

    def get_handler(self, event_type):
        if event_type not in {EventType.KEYWORD_EXPANSION, EventType.MEMORY_FETCH}:
            raise ValueError('optional_event_not_configured')
        return self

    async def fetch_one_expansion(self, exclude_keywords=None):
        context = await self.owner.context()
        result = await self.owner.provider.structured(messages=[
            {'role': 'system', 'content': '结合提供的真实对话，自主选择一个想继续思考的关键词。输出 JSON：keyword、expansion 两个非空字符串。联想不表示外部事实已发生。'},
            {'role': 'user', 'content': json.dumps({'conversation': context.plan.today_conversation,
                'excluded_keywords': sorted(exclude_keywords or [])}, ensure_ascii=False)}])
        value = result.parsed or {}
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ('keyword', 'expansion')):
            return None
        if value['keyword'] in (exclude_keywords or set()):
            return None
        return value

    async def fetch_one_memory(self, exclude_ids=None):
        rows = self.owner.messages.messages()
        query = next((r['content'] for r in reversed(rows) if r['role'] == 'user'), '')
        for item in await self.owner.memory.search(query):
            if item['id'] not in (exclude_ids or set()):
                return {'memory_id': item['id'], 'topic': item['summary'], 'content': json.dumps(item, ensure_ascii=False)}
        return None


class Wander:
    def __init__(self, config, messages, provider, memory):
        self.messages, self.provider, self.memory = messages, provider, memory
        self.store = WanderRuntimeStore(config.data_dir / 'wander_runtime.db')
        self.store.initialize()
        self.wishes = WishStore(str(config.data_dir / 'wishes.db'))
        self.task = None
        self.error = ''
        self.stopping = False
        self.lifecycle = None
        self.runner = WanderRuntimeRunner(store=self.store,
            controller=WanderRuntimeController(self.store, clock=datetime.now, max_open_nodes=8),
            planner=PlanDecisionAdapter(self.store, provider.structured, self.build,
                allowed_event_types={EventType.KEYWORD_EXPANSION, EventType.MEMORY_FETCH, EventType.SELF_REFLECTION}, max_activities=1),
            executor=NodeExecutionAdapter(self.store, LocalHandlers(self)),
            reviewer=NodeReviewAdapter(self.store, provider.structured, self.build),
            settlement=SettlementAdapter(self.store, provider.structured, self.build),
            self_reflection=SelfReflectionAdapter(self.store, provider.structured, self.build),
            wish_commit=WishCommitAdapter(self.store, self.wishes),
            share_adapter=RuntimeShareAdapter(self.store, self.share, self.push, self.build,
                should_suppress=lambda: self.stopping),
            context_provider=self.context, clock=datetime.now)
        self.runner.initialize()

    async def build(self, recipe, **kwargs):
        persona = self.messages.profile()['persona']
        if self.lifecycle:
            from context_builder import ContextBuilder
            from context_builder.assembly import assemble_context_result
            from .context_sources import history_messages
            rows, diaries = self.memory.context_sources.select_history()
            active = self.lifecycle.days.active()
            facts = await self.memory.context_sources.facts(active_date=active['date'] if active else None)
            facts.update(earlier_diaries=diaries,
                current_activity=kwargs.get('wander_activity_text', ''))
            built = await ContextBuilder.build(recipe, persona=persona,
                host_history=history_messages(rows), host_materials=facts)
            wire = assemble_context_result(built)
            runtime = kwargs.get('wander_runtime_text', '')
            if runtime:
                wire.append({'role': 'system', 'name': 'mirrow_sources', 'content': runtime})
            return SimpleNamespace(system_content=persona, formatted_messages=wire, decision_messages=wire,
                sections={'wander_runtime': {'text': runtime}})
        if recipe == 'WANDER_PUSH':
            # Sharing receives the complete actual activity, not just its summary.
            packet = {'activity': kwargs['wander_activity_text'], 'history': [
                {key: row[key] for key in ('role', 'content', 'timestamp', 'message_id', 'event_type')}
                for row in self.messages.messages() if not row['is_wander']]}
            return SimpleNamespace(formatted_messages=[{'role': 'system', 'name': 'mirrow_sources',
                'content': json.dumps(packet, ensure_ascii=False)}])
        kwargs['persona'] = persona
        return await runtime_context(recipe, **kwargs)

    async def context(self):
        rows = self.messages.messages()
        active = self.lifecycle.days.active() if self.lifecycle else None
        day = active['date'] if active else datetime.now().date().isoformat()
        conversation = json.dumps([{k: r[k] for k in ('role', 'content', 'timestamp', 'message_id', 'event_type')}
            for r in rows if r['active_date'] == day], ensure_ascii=False)
        common = dict(persona=self.messages.profile()['persona'], session_id=self.messages.session_id,
            current_time=datetime.now().isoformat(), user_status=json.dumps(self.lifecycle.status(), ensure_ascii=False) if self.lifecycle else '设备状态未知',
            today_conversation=conversation)
        return RuntimeContextBundle(plan=RuntimeContext(**common), decision=DecisionContext(**common),
            self_reflection=SelfReflectionContext(**common,
                capability_catalog='可用：关键词联想、回看已编码记忆、自省与许愿板。设备、会客和淘宝未接入。',
                wish_context=json.dumps(self.wishes.list_all(include_threads=True), ensure_ascii=False),
                existing_self_book=self.memory.core_context()))

    async def share(self, messages):
        return await self.provider.complete(messages, internal=True)

    def push(self, payload):
        if self.stopping or (self.lifecycle and not self.lifecycle.can_push()):
            return False
        self.messages.append('assistant', payload['message'], message_id='wander_' + payload['event_id'],
            event_type=payload['event_type'], wander=True, generation_model=payload.get('generation_model', ''))
        return True

    async def start(self):
        if not self.provider.config.configured:
            raise ValueError('model_not_configured')
        if self.task is not None and not self.task.done():
            return
        self.error = ''
        self.stopping = False
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        from .wander_events import capture_activity
        try:
            while not self.stopping:
                result = await self.runner.tick()
                if result.run_id:
                    for activity in self.store.list_activities(result.run_id):
                        capture_activity(self, activity['activity_id'])
                if not self.stopping:
                    await self.runner.wait(result.delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = 'wander_failed'

    async def stop(self):
        self.stopping = True
        snapshot = self.runner.request_interrupt()
        if self.task is not None:
            # Let an in-flight real node finish and settle; never mark it successful by cancellation.
            self.runner.wake()
            await self.task
            self.task = None
        if snapshot:
            await self.runner.settle_interrupt(snapshot['activity_id'])
        from .wander_events import capture_open_day
        capture_open_day(self)

    def status(self):
        return {'running': bool(self.task and not self.task.done() and not self.stopping),
            'stopping': bool(self.task and not self.task.done() and self.stopping), 'error': self.error,
            'schedule': self.store.get_scheduler_wake()}
