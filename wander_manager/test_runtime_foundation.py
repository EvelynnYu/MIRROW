"""Deterministic tests for the persistent Wander runtime foundation."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest

from wander_manager.event_catalog import EVENT_CATALOG, normalize_goal
from wander_manager.event_types import EventType
from wander_manager.runtime_models import (
    ActivityState, GoalMode, NodeState, RunState, WanderActivity, WanderNode, WanderRun,
    ensure_activity_transition, ensure_node_transition, ensure_run_transition,
)
from wander_manager.runtime_store import WanderRuntimeStore


class RuntimeFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "runtime.db")
        self.store = WanderRuntimeStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_catalog_exactly_covers_current_events_and_normalizes_goals(self):
        self.assertEqual(set(EVENT_CATALOG), set(EventType))
        self.assertEqual("💌 打开了朋友圈", EVENT_CATALOG[EventType.BROWSE_SOCIAL_FEED].display_name)
        self.assertEqual(normalize_goal(EventType.LISTEN_MUSIC, GoalMode.COUNT, 99), (GoalMode.COUNT, 15))
        self.assertEqual(normalize_goal(EventType.LISTEN_MUSIC, GoalMode.DURATION, 0), (GoalMode.DURATION, 5))
        self.assertEqual(normalize_goal(EventType.SLEEP, GoalMode.COUNT, 3), (GoalMode.DURATION, 60))
        self.assertEqual(normalize_goal(EventType.BROWSE_NEWS, GoalMode.DURATION, 16), (GoalMode.COUNT, 3))
        self.assertEqual(normalize_goal(EventType.BROWSE_XIAOHONGSHU, GoalMode.COUNT, 99), (GoalMode.COUNT, 5))
        self.assertEqual(normalize_goal(EventType.BROWSE_BOOKMARKS, GoalMode.COUNT, -2), (GoalMode.COUNT, 3))
        self.assertEqual(normalize_goal(EventType.MEMORY_FETCH, None), (GoalMode.OPEN_ENDED, None))
        self.assertEqual(normalize_goal(EventType.KEYWORD_EXPANSION, GoalMode.COUNT, 3), (GoalMode.OPEN_ENDED, None))
        self.assertEqual(normalize_goal(EventType.SELF_REFLECTION, None), (GoalMode.SINGLE, 1))
        self.assertEqual(normalize_goal(EventType.USER_TRACKING, None), (GoalMode.SINGLE, 1))
        self.assertEqual(normalize_goal(EventType.HOST_GROUP_ACTIVITY, None), (GoalMode.SINGLE, 1))
        for event in (EventType.VISIT_LOUNGE, EventType.BROWSE_TAOBAO):
            self.assertEqual(EVENT_CATALOG[event].allowed_goal_modes, (GoalMode.SINGLE,))
            self.assertEqual(normalize_goal(event, GoalMode.COUNT, 5), (GoalMode.SINGLE, 1))

    def test_state_transitions_allow_lifecycle_and_reject_skips(self):
        run = WanderRun()
        for state in (RunState.ACTIVE_WAIT, RunState.NODE_RUNNING, RunState.NODE_REVIEW, RunState.ACTIVE_WAIT, RunState.SETTLING, RunState.COMPLETED):
            run.transition_to(state)
        self.assertEqual(run.state, RunState.COMPLETED)
        with self.assertRaises(ValueError):
            ensure_run_transition(RunState.PLANNING, RunState.NODE_RUNNING)
        with self.assertRaises(ValueError):
            ensure_activity_transition(ActivityState.PLANNED, ActivityState.NODE_RUNNING)
        with self.assertRaises(ValueError):
            ensure_node_transition(NodeState.PLANNED, NodeState.COMPLETED)

    def test_store_round_trip_audit_redaction_and_delivery_identity(self):
        run = WanderRun(session_id="session-main", inclination_text="想听歌")
        self.store.save_run(run)
        activity = WanderActivity(activity_type=EventType.LISTEN_MUSIC.value, run_id=run.run_id, goal_mode=GoalMode.COUNT, goal_value=3)
        self.store.save_activity(activity)
        node = WanderNode(activity_id=activity.activity_id, round_index=1, source_summary="一首真实歌曲")
        self.store.save_node(node)

        decision_id = self.store.record_decision(
            run_id=run.run_id, activity_id=activity.activity_id, node_id=node.node_id,
            phase="node_review", model="flash", recipe="WANDER_V2",
            input_context={
                "persona": "AI",
                "api_key": "must-not-leak",
                "nested": {"token": "hidden", "keep": "感想", "host": "192.168.1.20"},
            },
            raw_output="authorization: Bearer definitely-not-a-real-secret", reasoning={"why": "旋律触发了联想"}, parsed_output={"continue": True},
            temperature=0.0, status="error", error="token=decision-secret",
            duration_ms=123, prompt_tokens=50, completion_tokens=10, cache_tokens=4,
        )
        delivery_id = self.store.record_delivery(
            run_id=run.run_id, activity_id=activity.activity_id, delivery_type="wander_push",
            message_id="wander_message_1", message_content="想和用户分享 password:delivery-secret",
            tool_calls=[{"name": "自己听歌", "authorization": "delivery-tool-secret"}],
        )
        stored_run = self.store.get_run(run.run_id)
        decision = self.store.get_decision(decision_id)
        delivery = self.store.get_delivery(delivery_id)
        self.assertEqual(stored_run["run_id"], run.run_id)
        self.assertEqual(decision["run_id"], run.run_id)
        self.assertEqual(decision["activity_id"], activity.activity_id)
        self.assertEqual(decision["node_id"], node.node_id)
        self.assertEqual(decision["reasoning"]["why"], "旋律触发了联想")
        self.assertEqual(decision["prompt_tokens"], 50)
        self.assertEqual(decision["input_context"]["api_key"], "[REDACTED]")
        self.assertEqual(decision["input_context"]["nested"]["token"], "[REDACTED]")
        self.assertEqual(decision["input_context"]["nested"]["keep"], "感想")
        self.assertEqual(decision["input_context"]["nested"]["host"], "[REDACTED_IP]")
        self.assertEqual(decision["raw_output"], "authorization:[REDACTED]")
        self.assertEqual(decision["error"], "token=[REDACTED]")
        self.assertEqual(delivery["run_id"], run.run_id)
        self.assertEqual(delivery["activity_id"], activity.activity_id)
        self.assertEqual(delivery["message_content"], "想和用户分享 password:[REDACTED]")
        self.assertEqual(delivery["tool_calls"][0]["name"], "自己听歌")
        self.assertEqual(delivery["tool_calls"][0]["authorization"], "[REDACTED]")

    def test_foreign_keys_reject_unrelated_identity(self):
        with self.assertRaises(ValueError):
            self.store.record_delivery(run_id="missing-run", activity_id="missing-activity", delivery_type="wander_push")

        first_run, second_run = WanderRun(), WanderRun()
        self.store.save_run(first_run)
        self.store.save_run(second_run)
        activity = WanderActivity(activity_type=EventType.SLEEP.value, run_id=first_run.run_id, goal_mode=GoalMode.DURATION, goal_value=30)
        self.store.save_activity(activity)
        with self.assertRaises(ValueError):
            self.store.record_delivery(run_id=second_run.run_id, activity_id=activity.activity_id, delivery_type="wander_push")

    def test_import_has_no_database_side_effect(self):
        untouched_path = os.path.join(self.tempdir.name, "not-created.db")
        self.assertFalse(os.path.exists(untouched_path))
        importlib.import_module("wander_manager.runtime_store")
        self.assertFalse(os.path.exists(untouched_path))


if __name__ == "__main__":
    unittest.main()
