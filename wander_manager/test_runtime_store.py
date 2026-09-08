"""Regression tests for unbounded, filtered Wander log reads."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from wander_manager.runtime_models import ActivityState, GoalMode, NodeState, RunState, WanderActivity, WanderNode, WanderRun
from wander_manager.runtime_store import WanderRuntimeStore


class RuntimeStoreLogTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = WanderRuntimeStore(Path(self.tempdir.name) / "runtime.db")
        self.store.initialize()
        today = datetime.now().isoformat()
        for index in range(3):
            run = WanderRun(session_id="main", state=RunState.ACTIVE_WAIT, created_at=today, updated_at=today)
            self.store.save_run(run)
            activity = WanderActivity(
                activity_type="keyword_expansion",
                run_id=run.run_id,
                state=ActivityState.ACTIVE_WAIT,
                goal_mode=GoalMode.SINGLE,
                goal_value=1,
                reason=f"test-{index}",
                created_at=today,
                updated_at=today,
            )
            self.store.save_activity(activity)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_limit_zero_returns_all_rows_after_date_filter(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(1, len(self.store.list_activity_logs(limit=1, date=today)))
        self.assertEqual(3, len(self.store.list_activity_logs(limit=0, date=today)))
        stats = self.store.activity_log_stats(date=today)
        self.assertEqual(3, stats["total_entries"])

    def test_activity_log_projects_time_ranges_and_failed_material_cooldown(self):
        now = datetime.now()
        start = now.replace(microsecond=0).isoformat()
        ended = (now.replace(microsecond=0)).isoformat()
        run = WanderRun(session_id="main", state=RunState.ACTIVE_WAIT, created_at=start, updated_at=ended)
        self.store.save_run(run)
        activity = WanderActivity(
            activity_type="listen_music", run_id=run.run_id,
            state=ActivityState.COMPLETED, goal_mode=GoalMode.SINGLE, goal_value=1,
            created_at=start, ended_at=ended, updated_at=ended,
        )
        self.store.save_activity(activity)
        node = WanderNode(
            activity_id=activity.activity_id, round_index=1,
            started_at=start, completed_at=ended,
            execution_status="failed", execution_error="search_results_missing_required_fields",
        )
        node.transition_to(NodeState.RUNNING)
        self.store.save_node(node)

        logs = self.store.list_activity_logs(limit=0, date=start[:10])
        log = next(item for item in logs if item["event_id"] == activity.activity_id)
        self.assertEqual(start, log["created_at"])
        self.assertEqual(ended, log["ended_at"])
        self.assertIsNotNone(log["duration_seconds"])
        self.assertEqual("failed", log["nodes"][0]["status"])
        self.assertEqual("search_results_missing_required_fields", log["nodes"][0]["error"])
        self.assertTrue(self.store.has_recent_failed_activity("listen_music", minutes=15))

    def test_scheduler_projection_is_attached_only_to_its_source_activity(self):
        logs = self.store.list_activity_logs(limit=0)
        source_id = logs[0]["event_id"]
        source_run_id = logs[0]["details"]["run_id"]
        self.store.save_scheduler_wake(
            session_id="main",
            next_plan_at=datetime.now().isoformat(),
            wake_reason="natural_success",
            source_activity_id=source_id,
            source_run_id=source_run_id,
        )

        projected = self.store.list_activity_logs(limit=0)
        source = next(item for item in projected if item["event_id"] == source_id)
        others = [item for item in projected if item["event_id"] != source_id]
        self.assertEqual("natural_success", source["details"]["wake_reason"])
        self.assertTrue(all(not item["details"]["wake_reason"] for item in others))

    def test_terminal_run_projects_legacy_planned_activity_as_not_started(self):
        now = datetime.now().replace(microsecond=0).isoformat()
        run = WanderRun(
            session_id="main", state=RunState.INTERRUPTED, outcome="user_interrupt",
            created_at=now, updated_at=now, ended_at=now,
        )
        self.store.save_run(run)
        activity = WanderActivity(
            activity_type="memory_fetch", run_id=run.run_id, state=ActivityState.PLANNED,
            order_index=1, goal_mode=GoalMode.SINGLE, goal_value=1,
            created_at=now, updated_at=now,
        )
        self.store.save_activity(activity)
        log = next(item for item in self.store.list_activity_logs(limit=0, date=now[:10]) if item["event_id"] == activity.activity_id)
        self.assertEqual("planned", log["state"])
        self.assertEqual("not_started", log["projected_status"])
        self.assertEqual("not_started_after_user_interrupt", log["abort_reason"])
        self.assertEqual(1, log["details"]["activity_order"])
        self.assertEqual(1, log["details"]["run_activity_count"])
        self.assertIn("未执行·本轮被打断", log["process_log"])

    def test_recover_stale_planning_run_is_atomic_and_idempotent(self):
        now = datetime(2026, 9, 6, 13, 0, 0)
        prior = WanderRun(session_id="main", state=RunState.COMPLETED, created_at=now.isoformat(), updated_at=now.isoformat())
        self.store.save_run(prior)
        source = WanderActivity(
            activity_type="keyword_expansion", run_id=prior.run_id,
            state=ActivityState.COMPLETED, created_at=now.isoformat(), updated_at=now.isoformat(),
        )
        self.store.save_activity(source)

        stale = WanderRun(session_id="main", state=RunState.PLANNING, created_at=now.isoformat(), updated_at=now.isoformat())
        self.store.save_run(stale)
        self.store.save_pending_inclination(source.activity_id, "main", "继续整理这条线")
        claimed = self.store.claim_pending_inclination("main", stale.run_id)
        self.assertEqual(source.activity_id, claimed["source_activity_id"])
        self.assertEqual(stale.run_id, self.store.get_pending_inclination(source.activity_id)["claimed_run_id"])

        recovered = self.store.recover_stale_planning_runs(
            now=now, retry_delay_seconds=300,
        )
        self.assertEqual([stale.run_id], [item["run_id"] for item in recovered])
        stored = self.store.get_run(stale.run_id)
        self.assertEqual(RunState.ABORTED.value, stored["state"])
        self.assertEqual("recovered_stale_planning", stored["outcome"])
        self.assertEqual("pending", self.store.get_pending_inclination(source.activity_id)["status"])
        wake = self.store.get_scheduler_wake("main")
        self.assertEqual("recovered_stale_planning", wake["wake_reason"])
        self.assertEqual(stale.run_id, wake["source_run_id"])
        self.assertEqual(300, (datetime.fromisoformat(wake["next_plan_at"]) - now).total_seconds())

        # A second initializer sees no planning claim and cannot create a
        # second recovery or alter the already-audited wake.
        self.assertEqual([], self.store.recover_stale_planning_runs(now=now + timedelta(seconds=1)))
        self.assertEqual(1, len(self.store.list_activities(source.run_id)))

    def test_stale_planning_recovery_preserves_existing_scheduler_wake(self):
        now = datetime(2026, 9, 6, 13, 0, 0)
        existing_due = (now + timedelta(seconds=90)).isoformat()
        self.store.save_scheduler_wake(
            session_id="status",
            next_plan_at=existing_due,
            wake_reason="existing_successor",
            source_run_id="previous-run",
        )
        stale = WanderRun(
            session_id="status",
            state=RunState.PLANNING,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        self.store.save_run(stale)

        recovered = self.store.recover_stale_planning_runs(
            now=now,
            retry_delay_seconds=300,
        )

        self.assertEqual(stale.run_id, recovered[0]["run_id"])
        self.assertEqual(existing_due, recovered[0]["next_plan_at"])
        self.assertEqual("existing_successor", recovered[0]["wake_reason"])
        wake = self.store.get_scheduler_wake("status")
        self.assertEqual(existing_due, wake["next_plan_at"])
        self.assertEqual("existing_successor", wake["wake_reason"])
        self.assertEqual("previous-run", wake["source_run_id"])

    def test_runtime_status_distinguishes_blocked_active_and_scheduled(self):
        status = self.store.get_runtime_status("status")
        self.assertEqual("idle", status["runtime_state"])

        now = datetime(2026, 9, 6, 13, 0, 0)
        run = WanderRun(session_id="status", state=RunState.PLANNING, created_at=now.isoformat(), updated_at=now.isoformat())
        self.store.save_run(run)
        self.assertEqual("blocked", self.store.get_runtime_status("status")["runtime_state"])
        self.assertEqual("planner_without_accepted_plan", self.store.get_runtime_status("status")["blocked_reason"])

        self.store.recover_stale_planning_runs(now=now, retry_delay_seconds=300)
        status = self.store.get_runtime_status("status")
        self.assertEqual("scheduled", status["runtime_state"])
        self.assertEqual("recovered_stale_planning", status["wake_reason"])

    def test_runtime_status_keeps_recent_planner_in_planning_state(self):
        now = datetime.now().replace(microsecond=0)
        run = WanderRun(
            session_id="fresh-planner",
            state=RunState.PLANNING,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        self.store.save_run(run)

        status = self.store.get_runtime_status("fresh-planner")

        self.assertEqual("planning", status["runtime_state"])
        self.assertEqual("", status["blocked_reason"])

    def test_activity_log_projects_taobao_failure_stage_and_public_error(self):
        now = datetime.now().replace(microsecond=0).isoformat()
        run = WanderRun(session_id="main", state=RunState.ABORTED, created_at=now, updated_at=now, ended_at=now)
        self.store.save_run(run)
        activity = WanderActivity(
            activity_type="browse_taobao", run_id=run.run_id, state=ActivityState.ABORTED,
            created_at=now, updated_at=now, ended_at=now,
        )
        self.store.save_activity(activity)
        node = WanderNode(
            activity_id=activity.activity_id, round_index=1, state=NodeState.ABORTED,
            source_payload={
                "status": "failed", "failure_stage": "search_or_bridge",
                "public_error": "淘宝原生桥接尚未配置",
            }, execution_status="failed", execution_error="淘宝原生桥接尚未配置",
            started_at=now, completed_at=now,
        )
        self.store.save_node(node)

        log = next(item for item in self.store.list_activity_logs(limit=0, date=now[:10]) if item["event_id"] == activity.activity_id)
        self.assertEqual("search_or_bridge", log["failure_stage"])
        self.assertEqual("淘宝原生桥接尚未配置", log["public_error"])
        self.assertEqual("search_or_bridge", log["nodes"][0]["failure_stage"])
        self.assertEqual("淘宝原生桥接尚未配置", log["details"]["public_error"])


if __name__ == "__main__":
    unittest.main()
