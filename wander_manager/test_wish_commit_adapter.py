"""Tests for same-database idempotent self-reflection wish commits."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from wander_manager.event_types import EventType
from wander_manager.runtime_models import (
    ActivityState, DecisionPhase, GoalMode, NodeState, RunState,
    WanderActivity, WanderNode, WanderRun,
)
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.wish_commit_adapter import WishCommitAdapter
from wander_manager.wish_store import WishStore


class WishCommitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.runtime = WanderRuntimeStore(root / "runtime.db")
        self.runtime.initialize()
        self.wishes = WishStore(str(root / "wishes.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    def _reflection_node(self, wishes):
        run = WanderRun(session_id="main", state=RunState.NODE_RUNNING)
        self.runtime.save_run(run)
        activity = WanderActivity(
            activity_type=EventType.SELF_REFLECTION.value, run_id=run.run_id,
            state=ActivityState.NODE_RUNNING, goal_mode=GoalMode.SINGLE, goal_value=1,
        )
        self.runtime.save_activity(activity)
        node = WanderNode(
            activity_id=activity.activity_id, round_index=1, state=NodeState.RUNNING,
            execution_status="succeeded",
            source_payload={"wishes": wishes, "wish_commit_status": "not_committed"},
        )
        self.runtime.save_node(node)
        return run, activity, node

    def _legacy_wish_store(self):
        path = Path(self.tempdir.name) / "legacy-wishes.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("""
                CREATE TABLE wishes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    times_wished INTEGER DEFAULT 1,
                    first_wished_at TEXT DEFAULT '',
                    last_wished_at TEXT DEFAULT '',
                    user_comment TEXT DEFAULT '',
                    comment_updated_at TEXT DEFAULT '',
                    fulfilled_at TEXT DEFAULT '',
                    fulfilled_note TEXT DEFAULT ''
                )
            """)
            conn.execute("INSERT INTO wishes(feature,status) VALUES ('旧愿望','pending')")
            conn.commit()
        finally:
            conn.close()
        return WishStore(str(path)), path

    def test_same_observation_is_committed_exactly_once(self):
        wishes = [{"feature": "更可靠的时间感", "reason": "行动需要真实时间", "novelty": "new"}]
        first = self.wishes.commit_reflection_wishes(
            run_id="run", activity_id="activity", node_id="node", wishes=wishes
        )
        second = self.wishes.commit_reflection_wishes(
            run_id="run", activity_id="activity", node_id="node", wishes=wishes
        )
        self.assertFalse(first[0]["idempotent_replay"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual([], self.wishes.list_all())

    def test_distinct_node_is_a_real_reaffirmation(self):
        wish = [{"feature": "稳定的时间感", "reason": "再次确认", "novelty": "reaffirmed"}]
        self.wishes.commit_reflection_wishes(
            run_id="run", activity_id="activity", node_id="node-1", wishes=wish
        )
        self.wishes.commit_reflection_wishes(
            run_id="run", activity_id="activity", node_id="node-2", wishes=wish
        )
        self.assertEqual([], self.wishes.list_all())

    def test_fulfilled_observation_is_audited_without_mutating_wishes(self):
        result = self.wishes.commit_reflection_wishes(
            run_id="run", activity_id="activity", node_id="node",
            wishes=[{"feature": "已经具备的能力", "novelty": "fulfilled"}],
        )
        self.assertEqual("skipped_fulfilled", result[0]["outcome"])
        self.assertEqual([], self.wishes.list_all())
        self.assertEqual(1, len(self.wishes.get_reflection_wish_observations("node")))

    def test_adapter_updates_runtime_audit_and_is_safe_to_retry(self):
        run, _, node = self._reflection_node([
            {"feature": "更清晰的行动进度", "reason": "保持连续性", "novelty": "new"},
            {"feature": "已经完成的东西", "reason": "无需再许愿", "novelty": "fulfilled"},
        ])
        adapter = WishCommitAdapter(self.runtime, self.wishes)
        first = adapter.commit(node.node_id)
        second = adapter.commit(node.node_id)
        self.assertEqual(first, second)
        self.assertEqual(0, len(self.wishes.list_all()))
        stored = self.runtime.get_node(node.node_id)
        self.assertEqual("committed", stored["source_payload"]["wish_commit_status"])
        self.assertEqual(
            [item["id"] for item in first], stored["side_effect_refs"]["wish_observation_ids"]
        )
        audits = self.runtime.list_decisions(run.run_id, DecisionPhase.WISH_COMMIT)
        self.assertEqual(1, len(audits))
        self.assertEqual("ok", audits[0]["status"])

    def test_new_contract_none_never_falls_back_to_legacy_array(self):
        run, _, node = self._reflection_node([
            {"feature": "旧数组不应落板", "reason": "不得创建"},
        ])
        node.source_payload = {
            "wish_contract_version": 2,
            "wish_action": {"type": "none"},
            "comment_reply": None,
            "wishes": [{"feature": "旧数组不应落板", "reason": "不得创建"}],
            "wish_commit_status": "not_committed",
        }
        self.runtime.save_node(node)
        results = WishCommitAdapter(self.runtime, self.wishes).commit(node.node_id)
        self.assertEqual([], results)
        self.assertEqual([], self.wishes.list_all())
        self.assertEqual("committed", self.runtime.get_node(node.node_id)["source_payload"]["wish_commit_status"])

    def test_new_contract_none_commits_without_touching_legacy_store(self):
        legacy, path = self._legacy_wish_store()
        run, _, node = self._reflection_node([])
        node.source_payload = {
            "wish_contract_version": 2,
            "wish_action": {"type": "none"},
            "comment_reply": None,
            "wishes": [{"feature": "旧数组不应落板"}],
            "wish_commit_status": "not_committed",
        }
        self.runtime.save_node(node)

        results = WishCommitAdapter(self.runtime, legacy).commit(node.node_id)

        self.assertEqual([], results)
        stored = self.runtime.get_node(node.node_id)
        self.assertEqual("committed", stored["source_payload"]["wish_commit_status"])
        self.assertEqual("no_op", stored["source_payload"]["wish_commit_outcome"])
        self.assertFalse(stored["source_payload"]["wish_commit_mutation"])
        self.assertEqual("ok", self.runtime.list_decisions(run.run_id)[0]["status"])
        conn = sqlite3.connect(path)
        try:
            self.assertEqual({"wishes", "sqlite_sequence"}, {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            })
            self.assertEqual("pending", conn.execute("SELECT status FROM wishes").fetchone()[0])
        finally:
            conn.close()

    def test_real_action_on_legacy_store_is_controlled_migration_required(self):
        legacy, path = self._legacy_wish_store()
        run, _, node = self._reflection_node([])
        node.source_payload = {
            "wish_contract_version": 2,
            "wish_action": {"type": "create", "title": "不能静默写入旧库"},
            "comment_reply": None,
            "wish_commit_status": "not_committed",
        }
        self.runtime.save_node(node)

        adapter = WishCommitAdapter(self.runtime, legacy)
        results = adapter.commit(node.node_id)

        self.assertEqual([], results)
        stored = self.runtime.get_node(node.node_id)
        self.assertEqual("migration_required", stored["source_payload"]["wish_commit_status"])
        self.assertFalse(stored["source_payload"]["wish_commit_mutation"])
        self.assertIn("旧 schema", stored["source_payload"]["wish_commit_error"])
        self.assertEqual("migration_required", self.runtime.list_decisions(run.run_id)[0]["status"])
        self.assertEqual([], adapter.commit(node.node_id))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual({"wishes", "sqlite_sequence"}, {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            })
            self.assertEqual("pending", conn.execute("SELECT status FROM wishes").fetchone()[0])
            self.assertNotIn("updated_at", {
                row[1] for row in conn.execute("PRAGMA table_info(wishes)")
            })
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
