"""Deterministic coverage for the second-stage Wander runtime contracts."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from context_builder.recipes import get_recipe
from context_builder.builder import ContextBuilder
from wander_manager.event_types import EventType
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.plan_decision_adapter import (ActivitySpec, PlanDecision, PlanDecisionAdapter,
    RuntimeContext, build_inclination_text)
from wander_manager.runtime_controller import RuntimeActionType, WanderRuntimeController
from wander_manager.runtime_models import GoalMode, RunState, SettlementReason, WanderRun
from wander_manager.runtime_store import WanderRuntimeStore


class RuntimeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WanderRuntimeStore(Path(self.tmp.name) / "runtime.db")
        self.store.initialize()
        self.now = datetime(2026, 8, 18, 12, 0, 0)
        self.controller = WanderRuntimeController(self.store, clock=lambda: self.now, max_open_nodes=2)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        run = WanderRun(session_id="s")
        self.store.save_run(run)
        return run

    def test_inclination_rules_and_threshold_boundaries(self):
        self.assertIn("没什么特别", build_inclination_text({"listen_music": .20}))
        self.assertIn("都有点想做", build_inclination_text({"listen_music": .30, "sleep": .28, "browse_news": .26}))
        self.assertIn("实在不行", build_inclination_text({"listen_music": .50, "sleep": .48, "browse_news": .41}))
        self.assertIn("有点想做", build_inclination_text({"listen_music": .60, "sleep": .58, "browse_news": .40}))
        self.assertIn("也不是不行", build_inclination_text({"listen_music": .70, "sleep": .50, "browse_news": .48}))
        self.assertIn("没什么其他", build_inclination_text({"listen_music": .80, "sleep": .60, "browse_news": .40}))

    def test_plan_audits_success_and_json_failure_without_network(self):
        runtime = RuntimeContext(persona="AI 的人格", session_id="s", mood="平静", probabilities={"listen_music": .7, "sleep": .3})
        received = {}
        async def fake_builder(_name, *, wander_runtime_text, persona, session_id):
            received.update(persona=persona, session_id=session_id)
            class Result:
                system_content = "稳定前缀\n" + wander_runtime_text
                sections = {"wander_runtime": {"text": wander_runtime_text}}
            return Result()
        async def good_llm(**_kwargs):
            return FlashJsonResult(parsed={"horizon_min": 999, "activities": [{"event_type": "听歌", "goal": {"mode": "count", "value": 2}}]},
                raw_content="{good}", reasoning="because", usage={"prompt_tokens": 7, "completion_tokens": 3}, model="fake", status="ok")
        run = self._run()
        decision = asyncio.run(PlanDecisionAdapter(self.store, good_llm, fake_builder).decide(run, runtime))
        self.assertEqual(999, decision.horizon_min)
        self.assertEqual(EventType.LISTEN_MUSIC, decision.activities[0].event_type)
        self.assertEqual({"persona": "AI 的人格", "session_id": "s"}, received)
        audited = self.store.get_decision(self._latest_decision_id())
        self.assertEqual("because", audited["reasoning"])
        self.assertEqual(7, audited["prompt_tokens"])
        self.assertEqual("listen_music", audited["parsed_output"]["normalized"]["activities"][0]["event_type"])
        async def bad_llm(**_kwargs):
            return FlashJsonResult(raw_content="not json", reasoning="partial", usage={"prompt_tokens": 1}, model="fake", status="json_parse_error", error="bad")
        failed = asyncio.run(PlanDecisionAdapter(self.store, bad_llm, fake_builder).decide(self._run(), runtime))
        self.assertIsNone(failed)
        self.assertEqual("json_parse_error", self.store.get_decision(self._latest_decision_id())["status"])
        async def unknown_llm(**_kwargs):
            return FlashJsonResult(parsed={"activities": [{"event_type": "不存在", "goal": {}}]}, raw_content="{unknown}", model="fake", status="ok")
        invalid = asyncio.run(PlanDecisionAdapter(self.store, unknown_llm, fake_builder).decide(self._run(), runtime))
        self.assertIsNone(invalid)
        self.assertEqual("validation_error", self.store.get_decision(self._latest_decision_id())["status"])
        async def broken_builder(**_kwargs):
            raise RuntimeError("broken")
        context_failed = asyncio.run(PlanDecisionAdapter(self.store, good_llm, broken_builder).decide(self._run(), runtime))
        self.assertIsNone(context_failed)
        self.assertEqual("context_error", self.store.get_decision(self._latest_decision_id())["status"])

        missing_persona = asyncio.run(PlanDecisionAdapter(self.store, good_llm, fake_builder).decide(
            self._run(), RuntimeContext(persona="", session_id="s")))
        self.assertIsNone(missing_persona)
        self.assertEqual("persona_is_required", self.store.get_decision(self._latest_decision_id())["error"])

        mismatched = asyncio.run(PlanDecisionAdapter(self.store, good_llm, fake_builder).decide(
            self._run(), RuntimeContext(persona="AI", session_id="another-session")))
        self.assertIsNone(mismatched)
        self.assertEqual("session_id_mismatch", self.store.get_decision(self._latest_decision_id())["error"])

        duplicate_payload = {"horizon_min": 30, "activities": [
            {"event_type": "listen_music", "goal": {"mode": "count", "value": 2}},
            {"event_type": "listen_music", "goal": {"mode": "count", "value": 4}},
        ]}
        duplicate_plan = PlanDecisionAdapter._normalize(duplicate_payload)
        self.assertEqual(1, len(duplicate_plan.activities))

    def _latest_decision_id(self):
        # Read through the public DB-independent behavior is intentionally not needed by production;
        # this test uses the known audit table only to assert persisted fields.
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        try:
            return conn.execute("SELECT decision_id FROM wander_decisions ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
        finally:
            conn.close()

    def test_recipe_runtime_segment_is_last(self):
        recipe = get_recipe("WANDER_V2")
        self.assertEqual("wander_runtime", recipe.sections[-1].ingredient)
        result = asyncio.run(ContextBuilder.build("WANDER_V2", wander_runtime_text="[运行态]\n可审计", persona="人格实际注入"))
        self.assertEqual("[运行态]\n可审计", result.sections["wander_runtime"]["text"])

        plan_ingredients = [section.ingredient for section in recipe.sections]
        activity_recipe = get_recipe("WANDER_ACTIVITY")
        activity_ingredients = [section.ingredient for section in activity_recipe.sections]
        self.assertIn("recent_inner_wander", plan_ingredients)
        self.assertNotIn("recent_inner_wander", activity_ingredients)
        self.assertEqual("wander_runtime", activity_recipe.sections[-1].ingredient)
        self.assertTrue(result.system_content.endswith("[运行态]\n可审计"))
        self.assertIn("人格实际注入", result.system_content)

    def test_duration_count_interrupt_and_recovery(self):
        sleep = PlanDecision((ActivitySpec(EventType.SLEEP, GoalMode.DURATION, 10),), 30, {})
        activities = self.controller.apply_plan(self._run(), sleep)
        self.assertEqual(RuntimeActionType.WAIT, self.controller.poll(activities[0].run_id).action)
        self.now += timedelta(minutes=10)
        self.assertEqual(RuntimeActionType.SETTLE, self.controller.poll(activities[0].run_id).action)
        music = PlanDecision((ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 2),), 30, {})
        activity = self.controller.apply_plan(self._run(), music)[0]
        first = self.controller.begin_node(activity.activity_id)
        self.controller.request_interrupt(activity.activity_id)
        self.assertEqual(RuntimeActionType.RECOVER_NODE, self.controller.poll(activity.run_id).action)
        settle = self.controller.complete_node_review(first.node_id, continue_activity=True)
        self.assertEqual(RuntimeActionType.SETTLE, settle.action)
        self.controller.settle_activity(activity.activity_id, SettlementReason.USER_INTERRUPT)
        self.assertEqual(RunState.INTERRUPTED.value, self.store.get_run(activity.run_id)["state"])
        social = PlanDecision((ActivitySpec(EventType.HOST_GROUP_ACTIVITY, GoalMode.SINGLE, 1),), 30, {})
        social_activity = self.controller.apply_plan(self._run(), social)[0]
        self.assertEqual(RuntimeActionType.START_NODE, self.controller.poll(social_activity.run_id).action)

    def test_count_nodes_and_successor_identity(self):
        plan = PlanDecision((
            ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 2),
            ActivitySpec(EventType.SELF_REFLECTION, GoalMode.SINGLE, 1),
        ), 30, {})
        first, second = self.controller.apply_plan(self._run(), plan)
        node1 = self.controller.begin_node(first.activity_id)
        self.controller.complete_node_review(node1.node_id, next_node_at=self.now)
        node2 = self.controller.begin_node(first.activity_id)
        action = self.controller.complete_node_review(node2.node_id)
        self.assertEqual(RuntimeActionType.SETTLE, action.action)
        self.controller.settle_activity(first.activity_id, SettlementReason.GOAL_REACHED, continue_next=True)
        self.assertEqual(RuntimeActionType.START_NODE, self.controller.poll(first.run_id).action)
        self.assertEqual(second.activity_id, self.controller.poll(first.run_id).activity_id)
        self.assertEqual(first.run_id, self.store.get_activity(second.activity_id)["run_id"])
        self.assertEqual(first.activity_id, self.store.get_node(node1.node_id)["activity_id"])

    def test_execution_error_terminalizes_remaining_planned_activities_without_nodes(self):
        run = self._run()
        first, second = self.controller.apply_plan(run, PlanDecision((
            ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 1),
            ActivitySpec(EventType.MEMORY_FETCH, GoalMode.OPEN_ENDED, None),
        ), 30, {}))
        node = self.controller.begin_node(first.activity_id)
        self.controller.complete_node_review(
            node.node_id, continue_activity=False, abort_reason="provider_failed", execution_failed=True,
        )
        self.controller.settle_activity(first.activity_id, SettlementReason.EXECUTION_ERROR)
        pending = self.store.get_activity(second.activity_id)
        self.assertEqual("aborted", pending["state"])
        self.assertEqual("not_started_after_prior_execution_error", pending["abort_reason"])
        self.assertIsNotNone(pending["ended_at"])
        self.assertEqual([], self.store.list_nodes(second.activity_id))
        self.assertEqual("aborted", self.store.get_run(run.run_id)["state"])

    def test_user_interrupt_terminalizes_remaining_planned_activities_without_nodes(self):
        run = self._run()
        first, second = self.controller.apply_plan(run, PlanDecision((
            ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 1),
            ActivitySpec(EventType.MEMORY_FETCH, GoalMode.OPEN_ENDED, None),
        ), 30, {}))
        self.controller.request_interrupt(first.activity_id)
        self.controller.settle_activity(first.activity_id, SettlementReason.USER_INTERRUPT)
        pending = self.store.get_activity(second.activity_id)
        self.assertEqual("interrupted", pending["state"])
        self.assertEqual("not_started_after_user_interrupt", pending["abort_reason"])
        self.assertIsNotNone(pending["ended_at"])
        self.assertEqual([], self.store.list_nodes(second.activity_id))
        self.assertEqual("interrupted", self.store.get_run(run.run_id)["state"])

    def test_empty_plan_future_due_and_horizon_are_enforced(self):
        run = self._run()
        with self.assertRaises(ValueError):
            self.controller.apply_plan(run, PlanDecision((), 30, {}))
        self.assertEqual(RunState.PLANNING.value, self.store.get_run(run.run_id)["state"])
        self.assertEqual([], self.store.list_activities(run.run_id))
        plan = PlanDecision((ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 2),), 10, {})
        activity = self.controller.apply_plan(self._run(), plan)[0]
        row = self.store.get_activity(activity.activity_id)
        row["next_node_at"] = (self.now + timedelta(minutes=1)).isoformat()
        from wander_manager.runtime_controller import WanderRuntimeController as _Controller
        delayed = _Controller._activity(row)
        self.store.save_activity(delayed)
        with self.assertRaises(ValueError):
            self.controller.begin_node(activity.activity_id)
        self.now += timedelta(minutes=10)
        action = self.controller.poll(activity.run_id)
        self.assertEqual((RuntimeActionType.SETTLE, SettlementReason.PLAN_HORIZON), (action.action, action.reason))
        open_activity = self.controller.apply_plan(self._run(), PlanDecision(
            (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 10, {}))[0]
        self.now += timedelta(minutes=10)
        open_action = self.controller.poll(open_activity.run_id)
        self.assertEqual((RuntimeActionType.SETTLE, SettlementReason.PLAN_HORIZON), (open_action.action, open_action.reason))


if __name__ == "__main__":
    unittest.main()
