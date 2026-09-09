"""Autonomous timing regressions; all clocks, model calls and databases are local fakes."""
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from wander_manager import test_runtime_runner as fixtures
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.event_types import EventType
from wander_manager.plan_decision_adapter import ActivitySpec, PlanDecision
from wander_manager.runtime_controller import RuntimeActionType
from wander_manager.runtime_models import DecisionPhase, GoalMode, WanderRun
from wander_manager.timing_policy import normalize_delay_seconds


class AutonomousTimingTests(unittest.TestCase):
    setUp = fixtures.RuntimeRunnerTests.setUp
    tearDown = fixtures.RuntimeRunnerTests.tearDown
    _runner = fixtures.RuntimeRunnerTests._runner
    _context = fixtures.RuntimeRunnerTests._context
    _plan_llm = staticmethod(fixtures.RuntimeRunnerTests._plan_llm)
    _review_llm = staticmethod(fixtures.RuntimeRunnerTests._review_llm)
    _settlement_llm = staticmethod(fixtures.RuntimeRunnerTests._settlement_llm)

    @staticmethod
    async def rest(**kwargs):
        return FlashJsonResult(parsed={"summary": "完成", "share": False,
            "continue_next": False, "next_run_delay_seconds": 120,
            "timing_reason": "稍后再想"}, status="ok")

    def test_model_rest_is_independent_of_continuation_and_survives_restart(self):
        runner = self._runner()
        runner.settlement.llm_caller = self.rest
        result = asyncio.run(runner.tick())
        self.assertEqual(120, result.delay_seconds)
        wake = self.store.get_scheduler_wake("main")
        self.assertEqual("model_wait", wake["wake_reason"])
        self.now += timedelta(seconds=30)
        self.assertEqual(90, asyncio.run(self._runner().tick()).delay_seconds)

    def test_missing_model_delay_is_audited_as_fallback(self):
        asyncio.run(self._runner().tick())
        wake = self.store.get_scheduler_wake("main")
        self.assertEqual("fallback_wait", wake["wake_reason"])
        self.assertEqual((self.now + timedelta(seconds=900)).isoformat(), wake["next_plan_at"])

    def test_new_normalized_plan_defaults_to_estimate_not_goal_mode(self):
        result = asyncio.run(self._runner().tick())
        run = self.store.get_run(result.run_id)
        self.assertEqual("estimate", run["context_snapshot"]["timing"]["horizon_mode"])

    def test_explicit_deadline_and_old_run_metadata_remain_hard_deadlines(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(run, PlanDecision(
            (ActivitySpec(EventType.SLEEP, GoalMode.DURATION, 60, "休息"),), 1, {}))[0]
        self.now += timedelta(minutes=1)
        self.assertEqual(RuntimeActionType.SETTLE, self.controller.poll().action)
        self.assertEqual("plan_horizon", self.controller.poll().reason.value)
        run.context_snapshot.pop("timing")
        self.store.save_run(run)
        self.assertEqual(RuntimeActionType.SETTLE, self.controller.poll().action)
        self.assertEqual([], self.store.list_nodes(activity.activity_id))

    def test_explicit_activity_timer_takes_precedence_over_estimate_review(self):
        _, activity_id = self._estimated_sleep()
        self.now += timedelta(minutes=59)
        action = self.controller.poll()
        self.assertEqual(RuntimeActionType.SETTLE, action.action)
        self.assertEqual("timer_ended", action.reason.value)
        self.assertEqual([], self.store.list_nodes(activity_id))

    def test_node_review_crash_reuses_absolute_due_without_second_model_call(self):
        runner = self._runner()
        async def review(**kwargs):
            return FlashJsonResult(parsed={"continue_activity": True,
                "next_node_delay_seconds": 120}, status="ok")
        runner.reviewer.llm_caller = review
        with patch.object(self.controller, "complete_node_review", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.tick())
        self.now += timedelta(seconds=60)
        restored = self._runner()
        async def forbidden(**kwargs):
            raise AssertionError("review must not be repeated")
        restored.reviewer.llm_caller = forbidden
        result = asyncio.run(restored.tick())
        self.assertEqual(60, result.delay_seconds)
        activity = self.store.list_activities(result.run_id)[0]
        self.assertEqual(1, len(self.store.list_nodes(activity["activity_id"])))

    def test_prepared_wake_crash_before_terminal_does_not_restart_rest(self):
        runner = self._runner()
        runner.settlement.llm_caller = self.rest
        with patch.object(self.controller, "settle_activity", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.tick())
        wake = self.store.get_scheduler_wake("main")
        self.assertIsNotNone(wake)
        self.now += timedelta(seconds=60)
        restored = self._runner()
        async def forbidden(**kwargs):
            raise AssertionError("settlement must not be repeated")
        restored.settlement.llm_caller = forbidden
        self.assertEqual(60, asyncio.run(restored.tick()).delay_seconds)
        self.assertEqual(wake["next_plan_at"], self.store.get_scheduler_wake("main")["next_plan_at"])

    def test_crash_after_terminal_keeps_wake(self):
        runner = self._runner()
        runner.settlement.llm_caller = self.rest
        async def crash(*args):
            raise RuntimeError("sidecar crash")
        runner._persist_settlement_emotion = crash
        with self.assertRaises(RuntimeError):
            asyncio.run(runner.tick())
        self.assertIsNone(self.store.get_active_run("main"))
        self.now += timedelta(seconds=60)
        self.assertEqual(60, asyncio.run(self._runner().tick()).delay_seconds)

    def test_reply_during_settlement_model_cancels_wake_and_share(self):
        runner = self._runner()
        async def interrupted(**kwargs):
            runner.request_interrupt()
            return await self._settlement_llm(**kwargs)
        runner.settlement.llm_caller = interrupted
        result = asyncio.run(runner.tick())
        activity = self.store.list_activities(result.run_id)[0]
        self.assertEqual("user_interrupt", activity["settlement_reason"])
        self.assertFalse(activity["share_decision"])
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_reply_during_post_terminal_delivery_cannot_recreate_wake(self):
        runner = self._runner()
        async def deliver(activity_id):
            self.assertIsNone(self.store.get_active_run("main"))
            self.assertIsNotNone(self.store.get_scheduler_wake("main"))
            runner.request_interrupt()
        runner.share_adapter = SimpleNamespace(deliver=deliver)
        asyncio.run(runner.tick())
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_persisted_settling_interrupt_overrides_prior_natural_reason(self):
        runner = self._runner()
        with patch.object(self.controller, "settle_activity", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.tick())
        runner.request_interrupt()
        result = asyncio.run(self._runner().tick())
        self.assertEqual("user_interrupt", self.store.list_activities(result.run_id)[0]["settlement_reason"])
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def _estimated_sleep(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activities = self.controller.apply_plan(run, PlanDecision(
            (ActivitySpec(EventType.SLEEP, GoalMode.DURATION, 60, "休息"),),
            1, {}, horizon_mode="estimate"))
        self.now += timedelta(minutes=1)
        return run.run_id, activities[0].activity_id

    def test_horizon_audit_recovery_preserves_absolute_extension_and_sleep_timer(self):
        run_id, activity_id = self._estimated_sleep()
        timer = self.store.get_activity(activity_id)["timer_ends_at"]
        runner = self._runner()
        async def extend(**kwargs):
            return FlashJsonResult(parsed={"continue_activity": True,
                "extend_minutes": 20, "next_node_delay_seconds": 120}, status="ok")
        runner.horizon_reviewer.llm_caller = extend
        expected = (self.now + timedelta(minutes=20)).isoformat()
        with patch.object(self.controller, "apply_horizon_review", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.tick())
        self.now += timedelta(minutes=5)
        restored = self._runner()
        asyncio.run(restored.tick())
        self.assertEqual(expected, self.store.get_run(run_id)["next_wake_at"])
        activity = self.store.get_activity(activity_id)
        self.assertEqual(timer, activity["timer_ends_at"])
        self.assertIsNone(activity["next_node_at"])
        self.assertEqual([], self.store.list_nodes(activity_id))
        self.assertEqual(1, len(self.store.list_decisions(run_id, DecisionPhase.TIMING_REVIEW)))

    def test_horizon_stop_zero_is_valid_and_creates_no_node(self):
        run_id, activity_id = self._estimated_sleep()
        runner = self._runner()
        async def stop(**kwargs):
            return FlashJsonResult(parsed={"continue_activity": False, "extend_minutes": 0}, status="ok")
        runner.horizon_reviewer.llm_caller = stop
        asyncio.run(runner.tick())
        self.assertEqual("plan_horizon", self.store.get_activity(activity_id)["settlement_reason"])
        self.assertEqual([], self.store.list_nodes(activity_id))
        self.assertEqual("ok", self.store.list_decisions(run_id, DecisionPhase.TIMING_REVIEW)[0]["status"])

    def test_reply_during_horizon_review_prevents_extension(self):
        run_id, activity_id = self._estimated_sleep()
        old_horizon = self.store.get_run(run_id)["next_wake_at"]
        runner = self._runner()
        async def interrupted(**kwargs):
            runner.request_interrupt()
            return FlashJsonResult(parsed={"continue_activity": True,
                "extend_minutes": 20, "next_node_delay_seconds": 120}, status="ok")
        runner.horizon_reviewer.llm_caller = interrupted
        asyncio.run(runner.tick())
        self.assertEqual(old_horizon, self.store.get_run(run_id)["next_wake_at"])
        self.assertEqual("user_interrupt", self.store.get_activity(activity_id)["settlement_reason"])
        self.assertEqual([], self.store.list_nodes(activity_id))
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_invalid_horizon_extension_settles_without_fake_success(self):
        run_id, activity_id = self._estimated_sleep()
        runner = self._runner()
        async def invalid(**kwargs):
            return FlashJsonResult(parsed={"continue_activity": True,
                "extend_minutes": 10 ** 100, "next_node_delay_seconds": 120}, status="ok")
        runner.horizon_reviewer.llm_caller = invalid
        asyncio.run(runner.tick())
        self.assertEqual("plan_horizon", self.store.get_activity(activity_id)["settlement_reason"])
        self.assertEqual("timing_review_error", self.store.list_decisions(run_id, DecisionPhase.TIMING_REVIEW)[0]["status"])
        self.assertEqual([], self.store.list_nodes(activity_id))


class TimingPolicyTests(unittest.TestCase):
    def test_numeric_boundary_is_not_an_autonomy_cap(self):
        for value in (None, True, False, -1, "bad", float("nan"), float("inf"), 1e100):
            with self.subTest(value=value):
                self.assertEqual((900, "fallback"), normalize_delay_seconds(value))
        self.assertEqual((5, "model"), normalize_delay_seconds(0))
        self.assertEqual((86400 * 3, "model"), normalize_delay_seconds(86400 * 3))
