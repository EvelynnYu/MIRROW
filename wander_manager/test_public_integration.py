"""Public package boundaries: no production service, credentials or devices."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from context_builder.builder import ContextBuilder
from context_builder.recipes import RecipeSection, get_recipe
from wander_manager import host_hooks
from wander_manager.event_catalog import EVENT_CATALOG
from wander_manager.event_handlers import EventHandlerFactory
from wander_manager.event_types import EventType
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.node_execution_adapter import NodeExecutionAdapter
from wander_manager.plan_decision_adapter import PlanDecisionAdapter
from wander_manager.runtime_models import GoalMode
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager import test_runtime_runner as runtime_fixtures


@pytest.mark.parametrize('event', [EventType.VISIT_LOUNGE, EventType.BROWSE_TAOBAO])
def test_optional_outing_is_disabled_until_registered_and_disconnects(event):
    with tempfile.TemporaryDirectory() as directory:
        store = WanderRuntimeStore(Path(directory) / 'runtime.db')
        store.initialize()
        planner = PlanDecisionAdapter(store, allowed_event_types={event})
        factory = EventHandlerFactory()
        host_hooks.register_event_handler(event, None)
        assert not planner.effective_allowed_event_types()
        handler = object()
        try:
            host_hooks.register_event_handler(event, handler, available=lambda: True)
            assert planner.effective_allowed_event_types() == {event}
            assert factory.get_handler(event) is handler
            host_hooks.register_event_handler(event, handler, available=lambda: False)
            assert not planner.effective_allowed_event_types()
        finally:
            host_hooks.register_event_handler(event, None)
        with pytest.raises(RuntimeError, match='optional_event_not_configured'):
            factory.get_handler(event)


@pytest.mark.parametrize('event', [EventType.VISIT_LOUNGE, EventType.BROWSE_TAOBAO])
def test_registered_outing_executes_exactly_one_real_node(event):
    fixture = runtime_fixtures.RuntimeRunnerTests()
    fixture.setUp()
    calls = []

    class Handler:
        async def visit_once(self):
            calls.append(event)
            return {'status': 'success', 'message': 'Synthetic verified visit', 'visit_id': 'test-visit'}

    async def plan(**kwargs):
        return FlashJsonResult(parsed={
            'horizon_min': 20,
            'activities': [{'event_type': event.value, 'goal': {'mode': 'single', 'value': 1}}],
        }, status='ok', model='test')

    try:
        host_hooks.register_event_handler(event, Handler())
        runner = fixture._runner()
        runner.planner = PlanDecisionAdapter(fixture.store, plan, runtime_fixtures.fake_builder,
                                             allowed_event_types={event})
        runner.executor = NodeExecutionAdapter(fixture.store, EventHandlerFactory())
        result = asyncio.run(runner.tick())
        activity = fixture.store.list_activities(result.run_id)[0]
        nodes = fixture.store.list_nodes(activity['activity_id'])
        assert calls == [event]
        assert len(nodes) == 1
        assert nodes[0]['execution_status'] == 'succeeded'
        assert activity['state'] == 'completed'
        assert EVENT_CATALOG[event].default_goal_mode == GoalMode.SINGLE
        assert EVENT_CATALOG[event].default_goal_value == 1
    finally:
        host_hooks.register_event_handler(event, None)
        fixture.tearDown()


def test_public_context_excludes_wander_but_preserves_real_conversation():
    builder = ContextBuilder('test-session')
    builder._kwargs = {'exclude_wander_history': True, 'conversation_messages': [
        SimpleNamespace(content='normal history', metadata={'role': 'user'}),
        SimpleNamespace(content='previous wander', metadata={'role': 'assistant', 'is_wander': True}),
    ]}
    result = asyncio.run(builder._call_ingredient(RecipeSection('history', 'conversation_messages')))
    assert len(result) == 1
    assert 'normal history' in result[0]['content']
    assert get_recipe('WANDER_ACTIVITY') is not None


def test_public_runtime_bootstraps_without_private_application():
    from wander_manager.manager import WanderManager
    from mirrow_core.shared_state import set_active_session_id, set_latest_persona_prompt
    async def model(*args, **kwargs):
        raise AssertionError('bootstrap must not call a model')
    manager = WanderManager(call_llm_func=model)
    set_active_session_id('test-public')
    set_latest_persona_prompt('Synthetic persona')
    try:
        runner = manager._wander_creator._ensure_runtime_runner()
        bundle = asyncio.run(manager._wander_creator._runtime_context_bundle())
        assert bundle.plan.session_id == 'test-public'
        assert bundle.plan.persona == 'Synthetic persona'
        assert runner.affect_service is None
    finally:
        set_active_session_id('')
        set_latest_persona_prompt('')
