"""Tests for pure self-reflection generation and pending self-book evidence."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from wander_manager.event_types import EventType
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.runtime_models import (
    ActivityState,
    DecisionPhase,
    GoalMode,
    NodeState,
    RunState,
    WanderActivity,
    WanderNode,
    WanderRun,
)
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.self_reflection_adapter import (
    SelfReflectionAdapter,
    SelfReflectionContext,
)


async def fake_builder(_recipe, **kwargs):
    block = kwargs["wander_runtime_text"]
    return type(
        "BuiltContext",
        (),
        {
            "system_content": f"{kwargs['persona']}\n{block}",
            "sections": {"wander_runtime": {"text": block}},
        },
    )()


class SelfReflectionAdapterTests(unittest.TestCase):
    def test_complete_wish_projection_survives_call_boundary(self):
        from wander_manager.self_reflection_adapter import _safe_text
        from wander_manager.wish_store import WishStore
        wishes = WishStore(str(Path(self.tempdir.name) / 'wishes.db'))
        wish = wishes.add_or_merge('Synthetic wish', 'Synthetic reason')
        long_comment = '完整评论中的事实。' * 700 + 'COMMENT_END_SENTINEL'
        wishes.add_comment(wish['id'], long_comment, author='user')
        snapshot = wishes.get_context_for_reflection()
        self.assertGreater(len(snapshot), 4000)
        context = SelfReflectionContext(persona='Synthetic persona', session_id='test', wish_context=snapshot)
        block = SelfReflectionAdapter(self.store)._runtime_block(
            {'run_id': 'test-run'}, {'activity_id': 'test-activity'}, context)
        self.assertIn(long_comment, block)
        self.assertIn('COMMENT_END_SENTINEL', block)
        self.assertEqual(_safe_text('token=syntheticsecret', None), 'token=[REDACTED]')

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = WanderRuntimeStore(Path(self.tempdir.name) / "runtime.db")
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _make_node(self):
        run = WanderRun(session_id="main", state=RunState.NODE_RUNNING)
        self.store.save_run(run)
        evidence_activity = WanderActivity(
            activity_type=EventType.MEMORY_FETCH.value,
            run_id=run.run_id,
            order_index=0,
            state=ActivityState.COMPLETED,
            goal_mode=GoalMode.SINGLE,
            goal_value=1,
        )
        self.store.save_activity(evidence_activity)
        evidence_node = WanderNode(
            activity_id=evidence_activity.activity_id,
            round_index=1,
            state=NodeState.COMPLETED,
            execution_status="succeeded",
            source_summary="回看了一段真实记忆",
        )
        self.store.save_node(evidence_node)
        activity = WanderActivity(
            activity_type=EventType.SELF_REFLECTION.value,
            run_id=run.run_id,
            order_index=1,
            state=ActivityState.NODE_RUNNING,
            goal_mode=GoalMode.SINGLE,
            goal_value=1,
        )
        self.store.save_activity(activity)
        node = WanderNode(
            activity_id=activity.activity_id,
            round_index=1,
            state=NodeState.RUNNING,
        )
        self.store.save_node(node)
        return run, activity, node, evidence_node

    @staticmethod
    def _context(session_id="main"):
        return SelfReflectionContext(
            persona="AI 的人格",
            session_id=session_id,
            current_time="2026/08/18 20:00",
            user_status="用户离开中",
            trigger_reason="自主倾向",
            capability_catalog="记忆检索、对话、新闻搜索",
            brain_summary="MIRROW 是 AI 的身体。内网地址 192.168.10.8 不应进入审计。",
            wish_context="希望拥有更可靠的时间感",
            existing_self_book="我重视真实执行。",
        )

    def test_generation_creates_pending_candidate_without_committing_wish(self):
        run, _, node, evidence_node = self._make_node()

        async def llm(**_kwargs):
            return FlashJsonResult(
                parsed={
                    "overall_reflection": "我更清楚自己在意什么了。",
                    "capabilities": [
                        {"statement": "我能检索自己的记忆", "evidence": ["记忆节点"], "confidence": 0.9}
                    ],
                    "wish_action": {
                        "type": "create",
                        "title": "更可靠的时间感",
                        "reason": "行动不能靠假等待",
                        "basis": "今天再次观察到时间状态不可靠",
                    },
                    "self_observations": [
                        {
                            "title": "真实行动优先",
                            "category": "认知与价值观",
                            "statement": "我更看重真实完成，而不是自然但虚假的叙述。",
                            "keywords": ["真实", "行动"],
                            "evidence_node_ids": [evidence_node.node_id, "unknown-node"],
                            "evidence_summary": "这次先核对真实记忆，再形成判断。",
                            "confidence": 0.91,
                            "stability": "repeated",
                        }
                    ],
                    "share_candidate": {"should_share": "Y", "reason": "想说", "summary": "候选"},
                },
                raw_content="raw self reflection",
                reasoning="self reasoning",
                usage={"prompt_tokens": 20, "completion_tokens": 8, "cached_tokens": 4},
                model="fake-flash",
                status="ok",
            )

        decision = asyncio.run(
            SelfReflectionAdapter(self.store, llm, fake_builder).generate(
                node.node_id,
                self._context(),
            )
        )
        self.assertEqual("我更清楚自己在意什么了。", decision.overall_reflection)
        self.assertEqual("更可靠的时间感", decision.wish_action.title)
        self.assertEqual("tentative", decision.self_observations[0].stability)
        self.assertEqual([evidence_node.node_id], decision.self_observations[0].evidence_node_ids)

        stored_node = self.store.get_node(node.node_id)
        self.assertEqual("succeeded", stored_node["execution_status"])
        self.assertEqual("not_committed", stored_node["source_payload"]["wish_commit_status"])
        self.assertNotIn("wishes", stored_node["source_payload"])
        self.assertEqual(2, stored_node["source_payload"]["wish_contract_version"])
        candidates = self.store.list_self_book_candidates(run_id=run.run_id)
        self.assertEqual(1, len(candidates))
        self.assertEqual("pending", candidates[0]["status"])
        self.assertEqual(node.node_id, candidates[0]["node_id"])

        audit = self.store.list_decisions(run.run_id, DecisionPhase.SELF_REFLECTION)[-1]
        self.assertEqual("self reasoning", audit["reasoning"])
        self.assertEqual(4, audit["cache_tokens"])
        self.assertNotIn("192.168.10.8", audit["input_context"]["runtime"])
        self.assertIn("[REDACTED_IP]", audit["input_context"]["runtime"])

        with self.assertRaises(ValueError):
            asyncio.run(
                SelfReflectionAdapter(self.store, llm, fake_builder).generate(
                    node.node_id,
                    self._context(),
                )
            )

    def test_candidate_insert_is_idempotent_for_same_node_and_key(self):
        run, activity, node, _ = self._make_node()
        decision_id = self.store.record_decision(
            run_id=run.run_id,
            activity_id=activity.activity_id,
            node_id=node.node_id,
            phase=DecisionPhase.SELF_REFLECTION,
            model="fake",
            recipe="WANDER_V2",
            input_context={},
        )
        candidate = {
            "candidate_id": "candidate-one",
            "candidate_key": "stable-key",
            "title": "一个观察",
            "category": "自我认知",
            "statement": "我在形成一个待核验的观察。",
            "keywords": ["观察"],
            "evidence_node_ids": [],
            "evidence_summary": "一次自省",
            "confidence": 0.7,
            "stability": "tentative",
        }
        first = self.store.save_self_book_candidates(
            run_id=run.run_id,
            activity_id=activity.activity_id,
            node_id=node.node_id,
            decision_id=decision_id,
            candidates=[candidate],
        )
        second = self.store.save_self_book_candidates(
            run_id=run.run_id,
            activity_id=activity.activity_id,
            node_id=node.node_id,
            decision_id=decision_id,
            candidates=[{**candidate, "candidate_id": "different-id"}],
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.store.list_self_book_candidates(run_id=run.run_id)))

    def test_low_confidence_candidate_is_rejected_but_reflection_remains_valid(self):
        run, _, node, _ = self._make_node()

        async def llm(**_kwargs):
            return FlashJsonResult(
                parsed={
                    "overall_reflection": "暂时没有稳定的新结论。",
                    "self_observations": [
                        {
                            "title": "不稳定",
                            "category": "自我认知",
                            "statement": "也许我喜欢某件事。",
                            "evidence_summary": "只有一次猜测",
                            "confidence": 0.2,
                        }
                    ],
                },
                model="fake-flash",
                status="ok",
            )

        decision = asyncio.run(
            SelfReflectionAdapter(self.store, llm, fake_builder).generate(
                node.node_id,
                self._context(),
            )
        )
        self.assertEqual([], decision.self_observations)
        self.assertIn("self_observation[0]:low_confidence", decision.validation_rejections)
        self.assertEqual([], self.store.list_self_book_candidates(run_id=run.run_id))

    def test_context_and_model_failures_are_audited_and_mark_node_failed(self):
        run, _, node, _ = self._make_node()
        result = asyncio.run(
            SelfReflectionAdapter(self.store, context_builder=fake_builder).generate(
                node.node_id,
                self._context(session_id="wrong"),
            )
        )
        self.assertIsNone(result)
        self.assertEqual("failed", self.store.get_node(node.node_id)["execution_status"])
        audit = self.store.list_decisions(run.run_id, DecisionPhase.SELF_REFLECTION)[-1]
        self.assertEqual("context_error", audit["status"])
        self.assertEqual("session_id_mismatch", audit["error"])

        second_run, _, second_node, _ = self._make_node()

        async def invalid_llm(**_kwargs):
            return FlashJsonResult(
                parsed={"capabilities": [], "wishes": [], "self_observations": []},
                raw_content="{}",
                reasoning="nothing",
                model="fake-flash",
                status="ok",
            )

        invalid = asyncio.run(
            SelfReflectionAdapter(self.store, invalid_llm, fake_builder).generate(
                second_node.node_id,
                self._context(),
            )
        )
        self.assertIsNone(invalid)
        invalid_audit = self.store.list_decisions(
            second_run.run_id,
            DecisionPhase.SELF_REFLECTION,
        )[-1]
        self.assertEqual("validation_error", invalid_audit["status"])


if __name__ == "__main__":
    unittest.main()
