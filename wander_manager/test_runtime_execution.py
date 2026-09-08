"""Deterministic tests for Wander node execution, review, and settlement."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from wander_manager.event_types import EventType
from wander_manager.flash_structured import FlashJsonResult
from wander_manager.node_execution_adapter import (
    NodeExecutionAdapter,
    NodeExecutionStatus,
)
from wander_manager.runtime_decision_adapters import (
    DecisionContext,
    NodeReviewAdapter,
    SettlementAdapter,
)
from wander_manager.runtime_models import (
    ActivityState,
    DecisionPhase,
    GoalMode,
    NodeState,
    RunState,
    SettlementReason,
    WanderActivity,
    WanderNode,
    WanderRun,
)
from wander_manager.runtime_store import WanderRuntimeStore


class RecordingHandler:
    def __init__(self):
        self.keyword_excluded = set()
        self.memory_excluded = set()
        self.bookmark_excluded = set()
        self.music_excluded = set()
        self.music_payload = {
            "fingerprint": "song-new",
            "title": "一首歌",
            "artist": "歌手",
            "melody_summary": "旋律材料",
            "duration_sec": 180,
            "netease_song_id": "123",
        }
        self.playback_result = True
        self.playback_ids = []
        self.news_result = {"status": "success", "query": "新发现", "content": "真实搜索结果"}
        self.raise_news = False
        self.xhs_result = {
            "status": "success",
            "source_id": "xhs-new",
            "query": "猫咪收纳",
            "title": "小户型收纳",
            "content_summary": "一篇有真实搜索证据的帖子摘要",
            "agent_audit": {"calls": [{
                "model": "dots-test",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }]},
        }
        self.interrupt_callback = None

    async def fetch_one_expansion(self, exclude_keywords=None):
        self.keyword_excluded = set(exclude_keywords or set())
        return {"keyword": "新词", "expansion": "一段联想"}

    async def fetch_one_memory(self, exclude_ids=None):
        self.memory_excluded = set(exclude_ids or set())
        if self.interrupt_callback:
            self.interrupt_callback()
        return {"memory_id": "memory-new", "topic": "夏天", "content": "一段记忆"}

    async def fetch_one_news(self):
        if self.raise_news:
            raise RuntimeError("search failed")
        return self.news_result

    async def fetch_one_post(self, exclude_ids=None, activity_reason=""):
        self.xhs_excluded = set(exclude_ids or set())
        self.xhs_reason = activity_reason
        return dict(self.xhs_result)

    async def fetch_one_bookmark(self, exclude_ids=None):
        self.bookmark_excluded = set(exclude_ids or set())
        return {
            "original_msg_id": "bookmark-new",
            "content": "收藏内容",
            "owner": "用户",
        }

    async def pick_and_fetch_song(self, exclude_fingerprints=None):
        self.music_excluded = set(exclude_fingerprints or set())
        return dict(self.music_payload)

    async def start_runtime_playback(self, song_id):
        self.playback_ids.append(song_id)
        return self.playback_result


class RecordingFactory:
    def __init__(self, handler=None):
        self.handler = handler or RecordingHandler()
        self.calls = []

    def get_handler(self, event_type):
        self.calls.append(event_type)
        return self.handler


class TrackingHandler:
    def __init__(self, error=""):
        self.event_id = ""
        self.idle_seconds = None
        self.error = error

    async def handle(self, event):
        self.event_id = event.event_id
        self.idle_seconds = event.details.get("idle_seconds")
        if self.error:
            event.details["error"] = self.error
            event.process_log = "用户追踪失败"
            return event
        event.details.update({
            "tracking_result": {"activity_type": "working", "confidence": 0.9},
            "activity_description": "正在写代码",
            "status_consistent": True,
        })
        event.process_log = "用户追踪: 正在写代码"
        return event


class HostGroupHandler:
    def __init__(self, skipped=False):
        self.skipped = skipped
        self.event_id = ""

    async def handle(self, event):
        self.event_id = event.event_id
        event.details["host_activity_completed"] = not self.skipped
        event.process_log = "宿主群组活动已完成" if not self.skipped else "宿主未报告群组活动完成"
        return event


class HostGroupErrorHandler:
    async def handle(self, event):
        event.details["error"] = "content_is_not_json_object"
        event.process_log = "AI-Peer 话题判断解析失败"
        return event


async def fake_builder(_recipe, **kwargs):
    block = kwargs["wander_runtime_text"]
    persona = kwargs["persona"]
    return type(
        "BuiltContext",
        (),
        {
            "system_content": f"{persona}\n{block}",
            "sections": {"wander_runtime": {"text": block}},
        },
    )()


class RuntimeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = WanderRuntimeStore(Path(self.tempdir.name) / "runtime.db")
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _make_running_node(self, event_type, prior_payload=None, reason=""):
        run = WanderRun(session_id="main", state=RunState.NODE_RUNNING)
        self.store.save_run(run)
        activity = WanderActivity(
            activity_type=event_type.value,
            run_id=run.run_id,
            state=ActivityState.NODE_RUNNING,
            goal_mode=GoalMode.SINGLE,
            goal_value=1,
            reason=reason,
        )
        self.store.save_activity(activity)
        if prior_payload is not None:
            prior = WanderNode(
                activity_id=activity.activity_id,
                round_index=1,
                state=NodeState.COMPLETED,
                source_payload=prior_payload,
                source_summary="之前的节点",
                execution_status=NodeExecutionStatus.SUCCEEDED.value,
            )
            self.store.save_node(prior)
            round_index = 2
        else:
            round_index = 1
        node = WanderNode(
            activity_id=activity.activity_id,
            round_index=round_index,
            state=NodeState.RUNNING,
        )
        self.store.save_node(node)
        return run, activity, node

    def _make_settling_activity(self, with_completed_node=True):
        run = WanderRun(session_id="main", state=RunState.SETTLING)
        self.store.save_run(run)
        activity = WanderActivity(
            activity_type=EventType.BROWSE_BOOKMARKS.value,
            run_id=run.run_id,
            state=ActivityState.SETTLING,
            goal_mode=GoalMode.COUNT,
            goal_value=2,
        )
        self.store.save_activity(activity)
        if with_completed_node:
            node = WanderNode(
                activity_id=activity.activity_id,
                round_index=1,
                state=NodeState.COMPLETED,
                source_summary="翻看用户收藏夹中的一条收藏",
                execution_status=NodeExecutionStatus.SUCCEEDED.value,
                reflection="这条值得留下",
            )
            self.store.save_node(node)
        return run, activity

    def test_safe_events_execute_and_preserve_seen_sets(self):
        cases = (
            (EventType.KEYWORD_EXPANSION, {"keyword": "旧词"}, "keyword_excluded", "旧词"),
            (EventType.MEMORY_FETCH, {"memory_id": "memory-old"}, "memory_excluded", "memory-old"),
            (EventType.BROWSE_BOOKMARKS, {"original_msg_id": "bookmark-old"}, "bookmark_excluded", "bookmark-old"),
        )
        for event_type, prior, attr, expected in cases:
            with self.subTest(event_type=event_type):
                run, _, node = self._make_running_node(event_type, prior)
                handler = RecordingHandler()
                result = asyncio.run(
                    NodeExecutionAdapter(self.store, RecordingFactory(handler)).execute(node.node_id)
                )
                self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
                self.assertIn(expected, getattr(handler, attr))
                stored = self.store.get_node(node.node_id)
                self.assertEqual("succeeded", stored["execution_status"])
                audit = self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION)[-1]
                self.assertEqual(node.node_id, audit["node_id"])
                self.assertIn("audit_gap", audit["input_context"])

        run, _, node = self._make_running_node(EventType.BROWSE_NEWS)
        result = asyncio.run(
            NodeExecutionAdapter(self.store, RecordingFactory()).execute(node.node_id)
        )
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
        self.assertIn("阅读", result.summary)
        self.assertEqual(run.run_id, self.store.list_decisions(run.run_id)[-1]["run_id"])

    def test_news_failures_are_not_misreported_as_success(self):
        _, _, node = self._make_running_node(EventType.BROWSE_NEWS)
        handler = RecordingHandler()
        handler.news_result = {"status": "no_search_tool", "query": "只有问题", "content": ""}
        skipped = asyncio.run(
            NodeExecutionAdapter(self.store, RecordingFactory(handler)).execute(node.node_id)
        )
        self.assertEqual(NodeExecutionStatus.SKIPPED, skipped.status)
        self.assertEqual("no_search_tool", skipped.error)

        _, _, failed_node = self._make_running_node(EventType.BROWSE_NEWS)
        failing = RecordingHandler()
        failing.raise_news = True
        failed = asyncio.run(
            NodeExecutionAdapter(self.store, RecordingFactory(failing)).execute(failed_node.node_id)
        )
        self.assertEqual(NodeExecutionStatus.FAILED, failed.status)
        self.assertTrue(failed.error.startswith("RuntimeError:"))
        self.assertNotIn("search failed", failed.error)

    def test_xiaohongshu_requires_real_material_and_audits_dots(self):
        run, activity, node = self._make_running_node(
            EventType.BROWSE_XIAOHONGSHU,
            {"source_id": "xhs-old"},
            reason="看看猫咪收纳攻略",
        )
        handler = RecordingHandler()
        result = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(handler)
        ).execute(node.node_id))
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
        self.assertIn("xhs-old", handler.xhs_excluded)
        self.assertEqual("看看猫咪收纳攻略", handler.xhs_reason)
        audit = self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION)[-1]
        self.assertEqual("dots-test", audit["model"])
        self.assertEqual(2, audit["prompt_tokens"])

        _, _, failed_node = self._make_running_node(EventType.BROWSE_XIAOHONGSHU)
        failed_handler = RecordingHandler()
        failed_handler.xhs_result = {"status": "search_failed", "error": "search_failed"}
        failed = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(failed_handler)
        ).execute(failed_node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, failed.status)
        self.assertEqual("search_failed", failed.error)

    def test_timer_and_self_reflection_do_not_call_legacy_handle(self):
        for event_type in (EventType.SLEEP, EventType.SELF_REFLECTION):
            with self.subTest(event_type=event_type):
                _, _, node = self._make_running_node(event_type)
                factory = RecordingFactory()
                result = asyncio.run(NodeExecutionAdapter(self.store, factory).execute(node.node_id))
                self.assertEqual(NodeExecutionStatus.SKIPPED, result.status)
                self.assertEqual([], factory.calls)

    def test_tracking_is_bounded_and_uses_activity_identity(self):
        run, activity, node = self._make_running_node(EventType.USER_TRACKING)
        handler = TrackingHandler()
        result = asyncio.run(NodeExecutionAdapter(
            self.store,
            RecordingFactory(handler),
            get_idle_seconds=lambda: 321.0,
        ).execute(node.node_id))
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
        self.assertEqual("observation_recorded", result.completion_signal)
        self.assertEqual(activity.activity_id, handler.event_id)
        self.assertEqual(321.0, handler.idle_seconds)
        self.assertEqual(activity.activity_id, result.side_effect_refs["runtime_activity_id"])
        audit = self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION)[-1]
        self.assertEqual("succeeded", audit["status"])

    def test_host_group_requires_explicit_completion_evidence(self):
        _, activity, node = self._make_running_node(EventType.HOST_GROUP_ACTIVITY)
        handler = HostGroupHandler()
        result = asyncio.run(
            NodeExecutionAdapter(self.store, RecordingFactory(handler)).execute(node.node_id)
        )
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
        self.assertEqual("host_activity_completed", result.completion_signal)
        self.assertEqual(activity.activity_id, handler.event_id)

        _, _, skipped_node = self._make_running_node(EventType.HOST_GROUP_ACTIVITY)
        skipped = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(HostGroupHandler(skipped=True))
        ).execute(skipped_node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, skipped.status)
        self.assertEqual("missing_host_activity_completion", skipped.error)

        class UnconfirmedHandler:
            async def handle(self, event):
                event.details["host_activity_completed"] = "false"
                return event

        _, _, unconfirmed_node = self._make_running_node(EventType.HOST_GROUP_ACTIVITY)
        unconfirmed = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(UnconfirmedHandler())
        ).execute(unconfirmed_node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, unconfirmed.status)

    def test_host_group_structured_parse_failure_is_not_no_topic_success(self):
        _, _, node = self._make_running_node(EventType.HOST_GROUP_ACTIVITY)
        result = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(HostGroupErrorHandler())
        ).execute(node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, result.status)
        self.assertEqual("content_is_not_json_object", result.error)

    def test_music_requires_real_external_completion(self):
        run, _, node = self._make_running_node(
            EventType.LISTEN_MUSIC,
            {"fingerprint": "song-old"},
        )
        handler = RecordingHandler()
        adapter = NodeExecutionAdapter(
            self.store,
            RecordingFactory(handler),
            music_playback_enabled=lambda: True,
        )
        prepared = asyncio.run(adapter.execute(node.node_id))
        self.assertEqual(NodeExecutionStatus.WAITING_EXTERNAL, prepared.status)
        self.assertIn("等待播放完成", prepared.summary)
        self.assertNotIn("听完", prepared.summary)
        self.assertIn("song-old", handler.music_excluded)
        with self.assertRaises(ValueError):
            asyncio.run(adapter.confirm_external_completion(node.node_id, "analysis_finished"))

        completed = asyncio.run(
            adapter.confirm_external_completion(node.node_id, "playback_finished")
        )
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, completed.status)
        self.assertEqual("playback_finished", completed.completion_signal)
        self.assertEqual(2, len(self.store.list_decisions(run.run_id, DecisionPhase.NODE_EXECUTION)))

    def test_music_setting_disables_only_real_playback(self):
        _, _, setting_false_node = self._make_running_node(EventType.LISTEN_MUSIC)
        setting_false_handler = RecordingHandler()
        setting_false = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(setting_false_handler), music_playback_enabled=lambda: False
        ).execute(setting_false_node.node_id))
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, setting_false.status)
        self.assertEqual("analysis_complete", setting_false.completion_signal)
        self.assertEqual([], setting_false_handler.playback_ids)
        self.assertFalse(setting_false.payload["played"])
        self.assertFalse(setting_false.payload["playback_requested"])
        self.assertEqual("none", setting_false.payload["playback_target"])

        _, _, missing_id_node = self._make_running_node(EventType.LISTEN_MUSIC)
        missing_id_handler = RecordingHandler()
        missing_id_handler.music_payload.update({"fingerprint": "song-missing-id", "title": "另一首歌"})
        missing_id_handler.music_payload["netease_song_id"] = None
        missing_id = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(missing_id_handler), music_playback_enabled=lambda: True
        ).execute(missing_id_node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, missing_id.status)
        self.assertEqual("missing_reliable_song_material", missing_id.error)
        self.assertEqual([], missing_id_handler.playback_ids)

        _, _, failed_start_node = self._make_running_node(EventType.LISTEN_MUSIC)
        failed_start_handler = RecordingHandler()
        failed_start_handler.music_payload.update({"fingerprint": "song-failed-start", "title": "第三首歌"})
        failed_start_handler.playback_result = False
        failed_start = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(failed_start_handler), music_playback_enabled=lambda: True
        ).execute(failed_start_node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, failed_start.status)
        self.assertEqual("playback_start_failed", failed_start.error)

    def test_music_post_hoc_title_artist_deduplication_is_authoritative(self):
        _, _, node = self._make_running_node(EventType.LISTEN_MUSIC, {
            "fingerprint": "old-different-fingerprint",
            "title": "一首歌",
            "artist": "歌手",
        })
        handler = RecordingHandler()
        result = asyncio.run(NodeExecutionAdapter(
            self.store, RecordingFactory(handler), music_playback_enabled=lambda: True
        ).execute(node.node_id))
        self.assertEqual(NodeExecutionStatus.FAILED, result.status)
        self.assertEqual("duplicate_recent_song", result.error)
        self.assertEqual([], handler.playback_ids)

    def test_interrupt_during_handler_is_recorded_without_advancing_state(self):
        _, activity, node = self._make_running_node(EventType.MEMORY_FETCH)
        handler = RecordingHandler()

        def interrupt():
            activity.interrupt_requested = True
            self.store.save_activity(activity)

        handler.interrupt_callback = interrupt
        result = asyncio.run(
            NodeExecutionAdapter(self.store, RecordingFactory(handler)).execute(node.node_id)
        )
        self.assertEqual(NodeExecutionStatus.SUCCEEDED, result.status)
        self.assertTrue(result.side_effect_refs["interrupted_after_execution"])
        self.assertEqual(ActivityState.NODE_RUNNING.value, self.store.get_activity(activity.activity_id)["state"])

    def test_node_review_normalizes_strings_and_audits_full_result(self):
        run, _, node = self._make_running_node(EventType.KEYWORD_EXPANSION)
        asyncio.run(NodeExecutionAdapter(self.store, RecordingFactory()).execute(node.node_id))

        async def llm(**_kwargs):
            return FlashJsonResult(
                parsed={
                    "reflection": "",
                    "emotion_effect": {
                        "changed": "N",
                        "from": "平静",
                        "to": "开心",
                        "delta": "更轻松",
                        "confidence": 9,
                    },
                    "continue_activity": "N",
                    "abort_reason": "",
                },
                raw_content="raw review",
                reasoning="review reasoning",
                usage={"prompt_tokens": 11, "completion_tokens": 5, "cached_tokens": 3},
                model="fake-flash",
                status="ok",
            )

        decision = asyncio.run(
            NodeReviewAdapter(self.store, llm, fake_builder).review(
                node.node_id,
                DecisionContext(persona="AI人格", session_id="main"),
            )
        )
        self.assertEqual("没有感想", decision.reflection)
        self.assertFalse(decision.continue_activity)
        self.assertEqual("自然停下", decision.abort_reason)
        self.assertFalse(decision.emotion_effect.changed)
        self.assertEqual("", decision.emotion_effect.delta)

        audit = self.store.list_decisions(run.run_id, DecisionPhase.NODE_REVIEW)[-1]
        self.assertEqual("review reasoning", audit["reasoning"])
        self.assertEqual(3, audit["cache_tokens"])
        self.assertEqual(node.node_id, audit["node_id"])
        self.assertIn("当前节点真实材料", audit["input_context"]["runtime"])

    def test_review_rejects_unfinished_source_and_audits_context_error(self):
        run, _, node = self._make_running_node(EventType.MEMORY_FETCH)
        with self.assertRaises(ValueError):
            asyncio.run(
                NodeReviewAdapter(self.store, context_builder=fake_builder).review(
                    node.node_id,
                    DecisionContext(persona="AI", session_id="main"),
                )
            )

        node.execution_status = NodeExecutionStatus.SUCCEEDED.value
        self.store.save_node(node)
        decision = asyncio.run(
            NodeReviewAdapter(self.store, context_builder=fake_builder).review(
                node.node_id,
                DecisionContext(persona="AI", session_id="wrong-session"),
            )
        )
        self.assertIsNone(decision)
        audit = self.store.list_decisions(run.run_id, DecisionPhase.NODE_REVIEW)[-1]
        self.assertEqual("context_error", audit["status"])
        self.assertEqual("session_id_mismatch", audit["error"])

    def test_settlement_suppresses_interrupt_share_and_has_factual_fallback(self):
        run, activity = self._make_settling_activity()

        async def llm(**_kwargs):
            return FlashJsonResult(
                parsed={
                    "summary": "整体有点感触",
                    "share": "Y",
                    "continue_next": "N",
                    "next_inclination_note": "",
                    "emotion_effect": {
                        "changed": True,
                        "from": "平静",
                        "to": "放松",
                        "delta": "更放松",
                        "confidence": 2,
                    },
                },
                raw_content="raw settlement",
                reasoning="settlement reasoning",
                model="fake-flash",
                status="ok",
            )

        context = DecisionContext(persona="AI人格", session_id="main")
        decision = asyncio.run(
            SettlementAdapter(self.store, llm, fake_builder).settle(
                activity.activity_id,
                SettlementReason.USER_INTERRUPT,
                context,
            )
        )
        self.assertFalse(decision.share)
        self.assertEqual("更放松", decision.emotion_effect.delta)
        self.assertEqual(1.0, decision.emotion_effect.confidence)
        audit = self.store.list_decisions(run.run_id, DecisionPhase.SETTLEMENT)[-1]
        self.assertEqual("Y", audit["parsed_output"]["raw"]["share"])
        self.assertFalse(audit["parsed_output"]["normalized"]["share"])

        fallback_run, fallback_activity = self._make_settling_activity()

        async def failed_llm(**_kwargs):
            return FlashJsonResult(
                raw_content="",
                reasoning="partial",
                model="fake-flash",
                status="timeout",
                error="TimeoutError",
            )

        fallback = asyncio.run(
            SettlementAdapter(self.store, failed_llm, fake_builder).settle(
                fallback_activity.activity_id,
                SettlementReason.EXECUTION_ERROR,
                context,
            )
        )
        self.assertIn("翻看用户收藏夹", fallback.summary)
        self.assertNotIn("Timeout", fallback.summary)
        fallback_audit = self.store.list_decisions(
            fallback_run.run_id,
            DecisionPhase.SETTLEMENT,
        )[-1]
        self.assertEqual("fallback_timeout", fallback_audit["status"])


if __name__ == "__main__":
    unittest.main()
