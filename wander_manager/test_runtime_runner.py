"""End-to-end tests for the single-owner Wander runtime runner."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wander_manager.event_types import EventType
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.node_execution_adapter import NodeExecutionAdapter
from wander_manager.plan_decision_adapter import (
    ActivitySpec, PlanDecision, PlanDecisionAdapter, RuntimeContext,
)
from wander_manager.runtime_controller import WanderRuntimeController
from wander_manager.runtime_decision_adapters import DecisionContext, NodeReviewAdapter, SettlementAdapter
from wander_manager.runtime_models import (
    ActivityState, DecisionPhase, GoalMode, RunState, SettlementReason,
    WanderActivity, WanderRun,
)
from wander_manager.runtime_runner import RuntimeContextBundle, WanderRuntimeRunner
from wander_manager.runtime_share_adapter import RuntimeShareAdapter
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.self_reflection_adapter import SelfReflectionAdapter, SelfReflectionContext
from wander_manager.wish_commit_adapter import WishCommitAdapter
from wander_manager.wish_store import WishStore


class WanderManagerRestoreTests(unittest.TestCase):
    def test_startup_timestamp_restore_does_not_interrupt_resumable_activity(self):
        from wander_manager.manager import WanderManager

        runner = Mock()
        manager = WanderManager.__new__(WanderManager)
        manager._config = SimpleNamespace(runtime_v3_enabled=True)
        manager._wander_creator = SimpleNamespace(_runtime_runner=runner)
        manager._mode_switch = Mock(is_wander_mode_active=True)
        manager._away_pending = False
        manager._away_info = None
        manager._away_retracted = False
        manager._activity_interrupt_snapshot = None
        manager._pending_judgment_tasks = []
        manager._on_status_change = None
        restored_at = datetime(2026, 9, 6, 17, 35, 56)

        manager.on_user_reply(timestamp=restored_at, capture_away=False)

        runner.request_interrupt.assert_not_called()
        manager._mode_switch.update_user_reply_time.assert_called_once_with(restored_at)
        self.assertIsNone(manager.get_activity_interrupt_snapshot())


async def fake_builder(_recipe, **kwargs):
    block = kwargs["wander_runtime_text"]
    return type("Built", (), {
        "system_content": f"{kwargs['persona']}\n{block}",
        "sections": {"wander_runtime": {"text": block}},
    })()


class KeywordHandler:
    async def fetch_one_expansion(self, exclude_keywords=None):
        return {"keyword": "连续性", "expansion": "行动与记录应当使用同一身份。"}


class HandlerFactory:
    def get_handler(self, event_type):
        if event_type != EventType.KEYWORD_EXPANSION:
            raise AssertionError(f"unexpected event: {event_type}")
        return KeywordHandler()


class RuntimeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.store = WanderRuntimeStore(root / "runtime.db")
        self.store.initialize()
        self.wishes = WishStore(str(root / "wishes.db"))
        self.now = datetime(2026, 8, 18, 20, 0, 0)
        self.controller = WanderRuntimeController(self.store, clock=lambda: self.now, max_open_nodes=3)
        self.settled = []

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    async def _plan_llm(**_kwargs):
        return FlashJsonResult(
            parsed={
                "horizon_min": 20,
                "activities": [
                    {"event_type": "keyword_expansion", "goal": {"mode": "open_ended", "value": None}}
                ],
            },
            raw_content="plan raw", model="fake-plan", status="ok",
        )

    @staticmethod
    async def _review_llm(**_kwargs):
        return FlashJsonResult(
            parsed={
                "reflection": "统一身份比生成更多文字更重要。",
                "continue_activity": False,
                "abort_reason": "自然停下",
                "emotion_effect": {"changed": False},
            },
            raw_content="review raw", model="fake-review", status="ok",
        )

    @staticmethod
    async def _settlement_llm(**_kwargs):
        return FlashJsonResult(
            parsed={
                "summary": "沿着连续性想了一会儿。",
                "share": True,
                "continue_next": False,
                "next_inclination_note": "之后还可以继续整理身份链。",
                "emotion_effect": {"changed": False},
            },
            raw_content="settlement raw", model="fake-settlement", status="ok",
        )

    async def _context(self):
        return RuntimeContextBundle(
            plan=RuntimeContext(
                persona="AI 的人格", session_id="main", user_status="用户离开中",
                probabilities={"keyword_expansion": 0.8},
            ),
            decision=DecisionContext(persona="AI 的人格", session_id="main"),
            self_reflection=SelfReflectionContext(persona="AI 的人格", session_id="main"),
        )

    def _runner(self):
        return WanderRuntimeRunner(
            store=self.store,
            controller=self.controller,
            planner=PlanDecisionAdapter(
                self.store, self._plan_llm, fake_builder,
                allowed_event_types={EventType.KEYWORD_EXPANSION}, max_activities=1,
            ),
            executor=NodeExecutionAdapter(self.store, HandlerFactory()),
            reviewer=NodeReviewAdapter(self.store, self._review_llm, fake_builder),
            settlement=SettlementAdapter(self.store, self._settlement_llm, fake_builder),
            self_reflection=SelfReflectionAdapter(self.store, self._review_llm, fake_builder),
            wish_commit=WishCommitAdapter(self.store, self.wishes),
            context_provider=self._context,
            on_activity_settled=self.settled.append,
            clock=lambda: self.now,
            default_interval_seconds=900,
        )

    def test_complete_real_node_chain_defers_requested_share(self):
        result = asyncio.run(self._runner().tick())
        self.assertEqual("scheduled_wait", result.action)
        run = self.store.get_run(result.run_id)
        self.assertEqual(RunState.COMPLETED.value, run["state"])
        activity = self.store.list_activities(run["run_id"])[0]
        node = self.store.list_nodes(activity["activity_id"])[0]
        self.assertEqual("completed", node["state"])
        self.assertEqual("succeeded", node["execution_status"])
        self.assertEqual("连续性", node["source_payload"]["keyword"])
        self.assertTrue(activity["share_decision"])
        self.assertEqual("之后还可以继续整理身份链。", activity["next_inclination_note"])
        self.assertTrue(self.store.has_delivery_for_activity(activity["activity_id"]))
        self.assertEqual([EventType.KEYWORD_EXPANSION], self.settled)
        logs = self.store.list_activity_logs(date=activity["created_at"][:10])
        self.assertEqual(activity["activity_id"], logs[0]["event_id"])
        self.assertEqual("deferred", logs[0]["details"]["delivery_status"])
        self.assertFalse(logs[0]["pushed"])
        self.assertEqual("natural_success", logs[0]["details"]["wake_reason"])

    def test_review_time_switch_carries_prior_thought_and_pushes_once(self):
        review_calls = 0
        settlement_blocks = []
        pushed = []

        async def review_llm(**_kwargs):
            nonlocal review_calls
            review_calls += 1
            return FlashJsonResult(parsed={
                "reflection": "第一项让我想换个角度继续。" if review_calls == 1 else "第二项接住了前面的念头。",
                "continue_activity": False,
                "abort_reason": "",
                "switch_to": ({"event_type": "keyword_expansion", "reason": "沿着第一项的感想换个角度"}
                              if review_calls == 1 else None),
                "emotion_effect": {"changed": False},
            }, raw_content="review", model="fake-review", status="ok")

        settlement_calls = 0
        async def settlement_llm(**kwargs):
            nonlocal settlement_calls
            settlement_calls += 1
            settlement_blocks.append(kwargs["messages"][0]["content"])
            return FlashJsonResult(parsed={
                "summary": "第一项总结" if settlement_calls == 1 else "第二项总结",
                "share": settlement_calls == 1,
                "continue_next": False,
                "next_inclination_note": "",
                "emotion_effect": {"changed": False},
            }, raw_content="settlement", model="fake-settlement", status="ok")

        async def share_llm(_messages):
            return {"content": "第一项的念头把我带到了第二项。", "reasoning": "按活动顺序串联"}

        async def push(payload):
            pushed.append(payload)
            return True

        async def share_builder(_recipe, **_kwargs):
            return type("Built", (), {
                "formatted_messages": [{"role": "system", "content": "AI 的人格"}],
                "system_content": "AI 的人格",
            })()

        runner = self._runner()
        runner.reviewer = NodeReviewAdapter(self.store, review_llm, fake_builder)
        runner.settlement = SettlementAdapter(self.store, settlement_llm, fake_builder)
        runner.share_adapter = RuntimeShareAdapter(self.store, share_llm, push, share_builder)

        result = asyncio.run(runner.tick())

        activities = self.store.list_activities(result.run_id)
        self.assertEqual(2, len(activities))
        self.assertEqual("not_requested", self.store.get_delivery_for_activity(activities[0]["activity_id"])["status"])
        self.assertEqual("sent", self.store.get_delivery_for_activity(activities[1]["activity_id"])["status"])
        self.assertIn("第一项总结", settlement_blocks[1])
        self.assertIn("第一项让我想换个角度继续", settlement_blocks[1])
        self.assertEqual(1, len(pushed))
        self.assertEqual(2, len(pushed[0]["tool_calls"]))
        wake = self.store.get_scheduler_wake("main")
        self.assertEqual("natural_success", wake["wake_reason"])
        self.assertEqual(900.0, (datetime.fromisoformat(wake["next_plan_at"]) - self.now).total_seconds())


    def test_settlement_effect_maps_affect_fields_and_reports_rejection(self):
        service = Mock()
        service.trace_id.return_value = "trace-test"
        service.submit_self_sidecar.return_value = {"accepted": False, "reason": "engine_disabled"}
        runner = self._runner()
        runner.affect_service = service
        activity = {"activity_id": "activity-map", "state": "completed"}
        run = {"run_id": "run-map"}
        decision = type("Decision", (), {
            "emotion_effect": type("Effect", (), {
                "changed": True, "from_emotion": "混乱", "to_emotion": "清晰",
                "delta": "重新整理后更清楚", "confidence": 0.9,
                "family": "兴趣", "intensity": 0.7, "persistence": "lingering",
                "operation": "reappraise",
            })(),
        })()
        with self.assertLogs("wander_manager.runtime_runner", level="WARNING"):
            asyncio.run(runner._persist_settlement_emotion(
                activity, run, decision, SettlementReason.NATURAL_STOP,
            ))
        payload = service.submit_self_sidecar.call_args.kwargs["self_state"]
        self.assertEqual("reappraise", payload["operation"])
        self.assertEqual("清晰", payload["feeling"])
        self.assertEqual("兴趣", payload["family"])
        self.assertEqual("重新整理后更清楚", payload["reason"])
        self.assertEqual(0.7, payload["intensity"])
        self.assertEqual("lingering", payload["persistence"])



    def test_successful_node_with_unavailable_review_is_natural_stop(self):
        async def bad_review_llm(**_kwargs):
            return FlashJsonResult(
                raw_content="{broken", model="fake-review", status="json_parse_error",
                error="content_is_not_json_object",
            )

        base = self._runner()
        base.reviewer = NodeReviewAdapter(self.store, bad_review_llm, fake_builder)
        result = asyncio.run(base.tick())
        activity = self.store.list_activities(result.run_id)[0]
        node = self.store.list_nodes(activity["activity_id"])[0]
        self.assertEqual("completed", node["state"])
        self.assertEqual("succeeded", node["execution_status"])
        self.assertEqual("node_review_unavailable", node["abort_reason"])
        self.assertFalse(node["execution_error"])
        self.assertEqual("completed", activity["state"])

    def test_user_interrupt_settles_without_share(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(
            run,
            PlanDecision(
                (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 20, {}
            ),
        )[0]
        runner = self._runner()
        snapshot = runner.request_interrupt()
        self.assertEqual(activity.activity_id, snapshot["activity_id"])
        settled = asyncio.run(runner.settle_interrupt(activity.activity_id))
        stored_run = self.store.get_run(run.run_id)
        stored_activity = self.store.get_activity(activity.activity_id)
        self.assertEqual(activity.activity_id, settled["activity_id"])
        self.assertEqual("interrupted", stored_run["state"])
        self.assertFalse(stored_activity["share_decision"])

    def test_interrupt_recovery_aborts_unconfirmed_node_without_replay(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(
            run,
            PlanDecision(
                (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 20, {}
            ),
        )[0]
        node = self.controller.begin_node(activity.activity_id)
        runner = self._runner()
        runner.request_interrupt()
        asyncio.run(runner.settle_interrupt(activity.activity_id))
        stored_node = self.store.get_node(node.node_id)
        self.assertEqual("aborted", stored_node["state"])
        self.assertEqual("pending", stored_node["execution_status"])
        self.assertEqual("interrupted", self.store.get_activity(activity.activity_id)["state"])
        self.assertEqual([], self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION))

    def test_planner_whitelist_rejects_unavailable_event(self):
        decision = PlanDecisionAdapter._normalize(
            {"activities": [{"event_type": "listen_music", "goal": {"mode": "count", "value": 2}}]},
            allowed_event_types=frozenset({EventType.KEYWORD_EXPANSION}),
            max_activities=1,
        )
        self.assertIsNone(decision)

    def test_flash_share_uses_activity_identity_and_correct_tool_card(self):
        result = asyncio.run(self._runner().tick())
        activity = self.store.list_activities(result.run_id)[0]
        pushed = []

        async def flash_llm(_messages):
            return {"content": "【漫想】我刚才沿着连续性想了一会儿。", "reasoning": "基于真实节点"}

        async def push(payload):
            pushed.append(payload)
            return True

        async def share_builder(_recipe, **_kwargs):
            return type("Built", (), {
                "formatted_messages": [{"role": "system", "content": "AI 的人格"}],
                "system_content": "AI 的人格",
            })()

        adapter = RuntimeShareAdapter(self.store, flash_llm, push, share_builder)
        self.assertTrue(asyncio.run(adapter.deliver(activity["activity_id"])))
        self.assertTrue(asyncio.run(adapter.deliver(activity["activity_id"])))
        self.assertEqual(1, len(pushed))
        self.assertEqual(activity["activity_id"], pushed[0]["event_id"])
        self.assertEqual("keyword_expansion", pushed[0]["event_type"])
        self.assertEqual("💭 关键词联想", pushed[0]["tool_calls"][0]["tool"])
        delivery = self.store.get_delivery_for_activity(activity["activity_id"])
        self.assertEqual("sent", delivery["status"])
        self.assertTrue(self.store.list_activity_logs(date=activity["created_at"][:10])[0]["pushed"])
        audits = self.store.list_decisions(result.run_id, DecisionPhase.SHARE)
        self.assertEqual(1, len(audits))
        self.assertEqual("flash", audits[0]["model"])
        self.assertEqual("基于真实节点", audits[0]["reasoning"])

    def test_review_cannot_switch_outside_host_allowed_events(self):
        runner = self._runner()
        async def review_llm(**kwargs):
            self.assertIn("[当前可续作事件]\nkeyword_expansion", kwargs["messages"][0]["content"])
            return FlashJsonResult(parsed={
                "reflection": "想听歌", "continue_activity": False,
                "switch_to": {"event_type": "listen_music", "reason": "继续想想"},
            }, status="ok")
        runner.reviewer = NodeReviewAdapter(self.store, review_llm, fake_builder)
        result = asyncio.run(runner.tick())
        activities = self.store.list_activities(result.run_id)
        self.assertEqual(["keyword_expansion"], [a["activity_type"] for a in activities])
        self.assertEqual("completed", activities[0]["state"])
        decisions = self.store.list_decisions(result.run_id, DecisionPhase.NODE_REVIEW)
        self.assertEqual("switch_event_unavailable", decisions[-1]["error"])

    def test_switch_rechecks_availability_after_model_returns(self):
        runner = self._runner()
        async def review_llm(**kwargs):
            runner.planner.allowed_event_types = frozenset()
            return FlashJsonResult(parsed={
                "reflection": "继续联想", "continue_activity": False,
                "switch_to": {"event_type": "keyword_expansion", "reason": "接着想"},
            }, status="ok")
        runner.reviewer = NodeReviewAdapter(self.store, review_llm, fake_builder)
        result = asyncio.run(runner.tick())
        self.assertEqual(1, len(self.store.list_activities(result.run_id)))
        decisions = self.store.list_decisions(result.run_id, DecisionPhase.NODE_REVIEW)
        self.assertEqual("rejected", decisions[-1]["status"])

    @staticmethod
    async def _share_builder(*_args, **_kwargs):
        return SimpleNamespace(formatted_messages=[{"role": "system", "content": "test"}])

    def test_dnd_suppresses_share_without_blocking_settlement_or_next_wake(self):
        runner = self._runner()
        calls = []
        async def llm(_):
            calls.append("llm")
            return "test"
        runner.share_adapter = RuntimeShareAdapter(
            self.store, llm, lambda _: calls.append("push"), self._share_builder,
        )
        with patch("mirrow_core.shared_state.should_suppress_proactive", return_value=True):
            result = asyncio.run(runner.tick())
        activity = self.store.list_activities(result.run_id)[0]
        self.assertEqual("completed", activity["state"])
        self.assertTrue(activity["share_decision"])
        self.assertEqual([], calls)
        self.assertEqual("suppressed", self.store.get_delivery_for_activity(activity["activity_id"])["status"])
        self.assertIsNotNone(self.store.get_scheduler_wake("main"))

    def test_dnd_enabled_during_generation_prevents_send(self):
        result = asyncio.run(self._runner().tick())
        activity = self.store.list_activities(result.run_id)[0]
        suppressed = False
        async def llm(_):
            nonlocal suppressed
            suppressed = True
            return "test"
        push = Mock(return_value=True)
        adapter = RuntimeShareAdapter(self.store, llm, push, self._share_builder,
                                      should_suppress=lambda: suppressed)
        self.assertFalse(asyncio.run(adapter.deliver(activity["activity_id"])))
        push.assert_not_called()
        self.assertEqual("suppressed", self.store.get_delivery_for_activity(activity["activity_id"])["status"])

    def test_delivery_requires_explicit_receipt_and_accepts_awaitable_receipt(self):
        result = asyncio.run(self._runner().tick())
        activity = self.store.list_activities(result.run_id)[0]
        delivery = self.store.get_delivery_for_activity(activity["activity_id"])
        async def llm(_): return "test"
        for receipt in (None, False):
            with self.subTest(receipt=receipt):
                self.store.update_delivery(delivery["delivery_id"], status="deferred")
                async def push(_, value=receipt): return value
                adapter = RuntimeShareAdapter(self.store, llm, push, self._share_builder,
                                              should_suppress=lambda: False)
                self.assertFalse(asyncio.run(adapter.deliver(activity["activity_id"])))
                self.assertEqual("failed", self.store.get_delivery_for_activity(activity["activity_id"])["status"])
        self.store.update_delivery(delivery["delivery_id"], status="deferred")
        def future_receipt(payload):
            self.assertEqual("main", payload["session_id"])
            future = asyncio.get_running_loop().create_future()
            future.set_result(True)
            return future
        adapter = RuntimeShareAdapter(self.store, llm, future_receipt, self._share_builder,
                                      should_suppress=lambda: False)
        self.assertTrue(asyncio.run(adapter.deliver(activity["activity_id"])))

    def test_preceding_lounge_activity_does_not_break_segment_share(self):
        result = asyncio.run(self._runner().tick())
        activity = self.store.list_activities(result.run_id)[0]
        visit = {**activity, "activity_id": "visit", "activity_type": "visit_lounge", "order_index": -1}
        real_nodes = self.store.list_nodes(activity["activity_id"])
        async def llm(_): return "test"
        push = Mock(return_value=True)
        adapter = RuntimeShareAdapter(self.store, llm, push, self._share_builder,
                                      should_suppress=lambda: False)
        with patch.object(self.store, "list_activities", return_value=[visit, activity]), \
             patch.object(self.store, "list_nodes", side_effect=lambda key: (
                 [{**real_nodes[0], "source_payload": {"notification_id": "visit-card"}}]
                 if key == "visit" else real_nodes
             )):
            self.assertTrue(asyncio.run(adapter.deliver(activity["activity_id"])))
        self.assertEqual(2, len(push.call_args.args[0]["tool_calls"]))

    def test_continuation_note_is_injected_once_after_successful_plan(self):
        settlement_calls = 0

        async def settlement_llm(**_kwargs):
            nonlocal settlement_calls
            settlement_calls += 1
            return FlashJsonResult(
                parsed={
                    "summary": "还想沿着这条线继续想。",
                    "share": False,
                    "continue_next": settlement_calls == 1,
                    "next_inclination_note": "刚才在整理连续性，还想继续。" if settlement_calls == 1 else "",
                    "emotion_effect": {"changed": False},
                },
                raw_content="settlement raw",
                model="fake-settlement",
                status="ok",
            )

        seen_runtime_blocks = []

        async def plan_llm(**kwargs):
            seen_runtime_blocks.append(kwargs["messages"][0]["content"])
            return await self._plan_llm(**kwargs)

        runner = self._runner()
        runner.planner = PlanDecisionAdapter(
            self.store,
            plan_llm,
            fake_builder,
            allowed_event_types={EventType.KEYWORD_EXPANSION},
            max_activities=1,
        )
        runner.settlement = SettlementAdapter(self.store, settlement_llm, fake_builder)

        first = asyncio.run(runner.tick())
        self.assertEqual("scheduled_wait", first.action)
        first_run = self.store.get_run(first.run_id)
        first_activity = self.store.list_activities(first_run["run_id"])[0]
        pending = self.store.get_pending_inclination(first_activity["activity_id"])
        self.assertEqual("pending", pending["status"])

        self.now += timedelta(seconds=180)
        second = asyncio.run(runner.tick())
        self.assertEqual("scheduled_wait", second.action)
        pending = self.store.get_pending_inclination(first_activity["activity_id"])
        self.assertEqual("consumed", pending["status"])
        self.assertIn("刚才在整理连续性，还想继续。", seen_runtime_blocks[1])

        self.now += timedelta(seconds=900)
        asyncio.run(runner.tick())
        self.assertEqual(3, len(seen_runtime_blocks))
        self.assertNotIn("刚才在整理连续性，还想继续。", seen_runtime_blocks[2])

    def test_scheduler_wake_survives_restart_and_is_claimed_at_due_time(self):
        self.store.save_scheduler_wake(
            session_id="main",
            next_plan_at=(self.now + timedelta(seconds=900)).isoformat(),
            wake_reason="natural_success",
            source_run_id="prior-run",
        )
        waiting = asyncio.run(self._runner().tick())
        self.assertEqual("scheduled_wait", waiting.action)
        self.assertEqual(900.0, waiting.delay_seconds)
        self.assertIsNone(self.store.get_active_run("main"))

        self.now += timedelta(seconds=900)
        restored = self._runner()
        result = asyncio.run(restored.tick())
        self.assertEqual("scheduled_wait", result.action)
        schedule = self.store.get_scheduler_wake("main")
        self.assertEqual(result.run_id, schedule["source_run_id"])

    def test_execution_error_backoff_is_per_event_and_bounded(self):
        run1 = WanderRun(session_id="main")
        self.store.save_run(run1)
        activity1 = self.controller.apply_plan(
            run1, PlanDecision((ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 20, {})
        )[0]
        runner = self._runner()
        delay, reason, streak = runner._settlement_schedule(
            activity_id=activity1.activity_id,
            session_id="main",
            reason=SettlementReason.EXECUTION_ERROR,
            continue_next=False,
            next_inclination_note="",
        )
        self.assertEqual((900.0, "execution_error_backoff", 1), (delay, reason, streak))
        self.store.save_scheduler_wake(
            session_id="main",
            next_plan_at=self.now.isoformat(),
            wake_reason=reason,
            source_activity_id=activity1.activity_id,
            failure_streak=streak,
        )

        run2 = WanderRun(session_id="main")
        self.store.save_run(run2)
        activity2 = self.controller.apply_plan(
            run2, PlanDecision((ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 20, {})
        )[0]
        delay, _, streak = runner._settlement_schedule(
            activity_id=activity2.activity_id,
            session_id="main",
            reason=SettlementReason.EXECUTION_ERROR,
            continue_next=False,
            next_inclination_note="",
        )
        self.assertEqual((1800.0, 2), (delay, streak))

    def test_user_stop_clears_future_scheduler_wake_without_active_run(self):
        self.store.save_scheduler_wake(
            session_id="main",
            next_plan_at=(self.now + timedelta(seconds=900)).isoformat(),
            wake_reason="natural_success",
        )
        self.assertIsNone(self._runner().request_interrupt())
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_user_reply_during_planning_aborts_without_retry_wake(self):
        runner = self._runner()

        class InterruptingPlanner:
            async def decide(_self, _run, _context):
                self.assertIsNone(runner.request_interrupt())
                return PlanDecision(
                    (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),),
                    20,
                    {},
                )

        runner.planner = InterruptingPlanner()
        result = asyncio.run(runner.tick())

        self.assertEqual("user_interrupt", result.action)
        self.assertEqual("user_interrupt_before_plan", self.store.get_run(result.run_id)["outcome"])
        self.assertEqual([], self.store.list_activities(result.run_id))
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_planner_timeout_closes_run_releases_claim_and_schedules_retry(self):
        runner = self._runner()
        runner.planner_timeout_seconds = 0.01

        source_run = WanderRun(session_id="main", state=RunState.COMPLETED)
        self.store.save_run(source_run)
        source_activity = WanderActivity(
            activity_type=EventType.KEYWORD_EXPANSION.value,
            run_id=source_run.run_id,
            state=ActivityState.COMPLETED,
        )
        self.store.save_activity(source_activity)
        self.store.save_pending_inclination(source_activity.activity_id, "main", "继续刚才的线")

        class HangingPlanner:
            async def decide(_self, _run, _context):
                await asyncio.sleep(1)

        runner.planner = HangingPlanner()
        result = asyncio.run(runner.tick())

        self.assertEqual("planner_timeout", result.action)
        run = self.store.get_run(result.run_id)
        self.assertEqual(RunState.ABORTED.value, run["state"])
        self.assertEqual("planner_timeout", run["outcome"])
        self.assertIsNone(self.store.get_active_run("main"))
        self.assertEqual("pending", self.store.get_pending_inclination(source_activity.activity_id)["status"])
        wake = self.store.get_scheduler_wake("main")
        self.assertEqual("planner_timeout", wake["wake_reason"])
        self.assertEqual(result.run_id, wake["source_run_id"])

    def test_repeated_initialize_does_not_abort_a_live_planning_run(self):
        runner = self._runner()
        runner.initialize()
        live = WanderRun(session_id="main")
        self.store.save_run(live)

        # Foreground chat entry points defensively call initialize().  Once
        # this runner owns the process, that call is table setup only and must
        # not reinterpret its own in-flight planner as a crash remnant.
        runner.initialize()

        stored = self.store.get_run(live.run_id)
        self.assertEqual(RunState.PLANNING.value, stored["state"])
        self.assertIsNone(self.store.get_scheduler_wake("main"))

    def test_invalid_accepted_plan_is_closed_instead_of_leaving_planning_ghost(self):
        runner = self._runner()

        class InvalidPlanner:
            async def decide(_self, _run, _context):
                return PlanDecision(
                    (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.DURATION, 1),),
                    20,
                    {},
                )

        runner.planner = InvalidPlanner()
        result = asyncio.run(runner.tick())
        self.assertEqual("plan_commit_error", result.action)
        self.assertEqual("plan_commit_error", self.store.get_run(result.run_id)["outcome"])
        self.assertEqual("plan_commit_error", self.store.get_scheduler_wake("main")["wake_reason"])

    def test_restart_never_replays_an_unfinished_external_node(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(
            run,
            PlanDecision(
                (ActivitySpec(EventType.KEYWORD_EXPANSION, GoalMode.OPEN_ENDED, None),), 20, {}
            ),
        )[0]
        node = self.controller.begin_node(activity.activity_id)
        asyncio.run(self._runner().tick())
        stored_run = self.store.get_run(run.run_id)
        stored_node = self.store.get_node(node.node_id)
        self.assertEqual(RunState.ABORTED.value, stored_run["state"])
        self.assertEqual("aborted", stored_node["state"])
        execution_audits = self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION)
        self.assertEqual([], execution_audits)

    def test_sleep_uses_timer_without_creating_fake_node(self):
        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(
            run,
            PlanDecision((ActivitySpec(EventType.SLEEP, GoalMode.DURATION, 10),), 20, {}),
        )[0]
        waiting = asyncio.run(self._runner().tick())
        self.assertEqual("wait", waiting.action)
        self.assertEqual([], self.store.list_nodes(activity.activity_id))
        self.now = self.now.replace(minute=10)
        asyncio.run(self._runner().tick())
        self.assertEqual("completed", self.store.get_activity(activity.activity_id)["state"])
        self.assertEqual([], self.store.list_nodes(activity.activity_id))

    def test_music_waits_for_real_duration_without_replaying_playback(self):
        class MusicHandler:
            starts = 0

            async def pick_and_fetch_song(self, exclude_fingerprints=None):
                return {
                    "fingerprint": "song-1", "title": "真实歌曲", "artist": "歌手",
                    "netease_song_id": "42", "duration_sec": 180,
                    "melody_summary": "旋律分析完成",
                }

            async def start_runtime_playback(self, _song_id):
                self.starts += 1
                return True

        class MusicFactory:
            def __init__(self):
                self.handler = MusicHandler()

            def get_handler(self, _event_type):
                return self.handler

        run = WanderRun(session_id="main")
        self.store.save_run(run)
        activity = self.controller.apply_plan(
            run,
            PlanDecision((ActivitySpec(EventType.LISTEN_MUSIC, GoalMode.COUNT, 1),), 20, {}),
        )[0]
        runner = self._runner()
        music_factory = MusicFactory()
        runner.executor = NodeExecutionAdapter(
            self.store,
            music_factory,
            clock=lambda: self.now,
            music_playback_enabled=lambda: True,
        )
        waiting = asyncio.run(runner.tick())
        self.assertEqual("waiting_external", waiting.action)
        self.assertEqual(1, music_factory.handler.starts)
        node = self.store.list_nodes(activity.activity_id)[0]
        self.assertEqual("waiting_external", node["execution_status"])
        self.now = self.now.replace(minute=3)
        asyncio.run(runner.tick())
        node = self.store.get_node(node["node_id"])
        self.assertEqual("duration_elapsed", node["completion_signal"])
        self.assertEqual("completed", self.store.get_activity(activity.activity_id)["state"])
        self.assertEqual(1, music_factory.handler.starts)


if __name__ == "__main__":
    unittest.main()
