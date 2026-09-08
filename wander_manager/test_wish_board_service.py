"""Focused wish-board service tests; every test uses a temporary SQLite file."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from .wish_board_service import WishMigrationRequired, WishStore


class WishBoardServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = WishStore(str(Path(self.tempdir.name) / "wishes.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_three_statuses_comments_history_and_soft_delete(self):
        created = self.store.commit_reflection_action(
            run_id="run-1", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "稳定的时间感", "reason": "行动需要真实时间"},
        )
        self.assertEqual("created", created[0]["outcome"])
        wish_id = created[0]["wish_id"]

        self.assertTrue(self.store.set_status(wish_id, "in_progress")["mutated"])
        self.assertTrue(self.store.set_status(wish_id, "impossible_pending")["mutated"])
        self.store.add_comment(wish_id, "我还在观察它", author="user")
        self.store.add_comment(wish_id, "我记住了", author="k")
        self.store.add_comment(wish_id, "先保留这个念头", author="user")
        board_wish = self.store.get_board()["wishes"][0]
        self.assertEqual("impossible_pending", board_wish["status"])
        self.assertEqual(2, len(board_wish["latest_comments"]))
        self.assertEqual(3, len(board_wish["all_comments"]))

        retained = self.store.commit_reflection_action(
            run_id="run-1", activity_id="activity-2", node_id="node-2",
            action={"type": "retain_impossible", "wish_id": wish_id},
        )
        self.assertEqual("impossible_kept", retained[0]["outcome"])
        deleted = self.store.set_status(wish_id, "deleted", actor="system", note="测试软删除")
        self.assertTrue(deleted["mutated"])
        self.assertEqual("deleted", self.store.get_by_id(wish_id)["status"])
        self.assertTrue(any(event["event_type"] == "deleted" for event in self.store.get_board()["wishes"][0]["history"]))

    def test_stable_id_reaffirm_is_idempotent_and_new_node_increments(self):
        first = self.store.commit_reflection_action(
            run_id="run-2", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "后台连续性", "reason": "首次依据"},
        )
        wish_id = first[0]["wish_id"]
        replay = self.store.commit_reflection_action(
            run_id="run-2", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "后台连续性", "reason": "首次依据"},
        )
        self.assertTrue(replay[0].get("idempotent_replay"))
        self.assertEqual(1, self.store.get_by_id(wish_id)["times_wished"])

        reaffirm = self.store.commit_reflection_action(
            run_id="run-2", activity_id="activity-2", node_id="node-2",
            action={"type": "reaffirm", "wish_id": wish_id, "basis": "今天又观察到断连"},
        )
        self.assertEqual("reaffirmed", reaffirm[0]["outcome"])
        self.assertEqual(2, self.store.get_by_id(wish_id)["times_wished"])

    def test_create_does_not_bypass_deleted_tombstone_and_none_is_noop(self):
        first = self.store.commit_reflection_action(
            run_id="run-3", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "一个删除后的念头"},
        )
        wish_id = first[0]["wish_id"]
        self.assertTrue(self.store.set_status(wish_id, "deleted", actor="k")["mutated"])
        duplicate = self.store.commit_reflection_action(
            run_id="run-3", activity_id="activity-2", node_id="node-2",
            action={"type": "create", "title": "一个删除后的念头"},
        )
        self.assertEqual("ignored_deleted_tombstone", duplicate[0]["outcome"])
        self.assertEqual(1, len(self.store.list_all()))
        self.assertEqual([], self.store.commit_reflection_action(
            run_id="run-3", activity_id="activity-3", node_id="node-3",
            action={"type": "none"},
        ))

    def test_comment_reply_is_flat_and_idempotent(self):
        created = self.store.commit_reflection_action(
            run_id="run-4", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "评论线程"},
        )
        wish_id = created[0]["wish_id"]
        candidate = self.store.add_comment(wish_id, "你还记得这个愿望吗？", author="user")["comment"]
        reply = {"wish_id": wish_id, "content": "我会继续留意", "reply_to_comment_id": candidate["id"]}
        first = self.store.commit_reflection_action(
            run_id="run-4", activity_id="activity-2", node_id="node-2",
            action={"type": "none"}, comment_reply=reply,
        )
        second = self.store.commit_reflection_action(
            run_id="run-4", activity_id="activity-2", node_id="node-2",
            action={"type": "none"}, comment_reply=reply,
        )
        self.assertTrue(first[-1].get("mutation"))
        self.assertTrue(second[-1].get("idempotent_replay"))
        self.assertEqual(2, len(self.store.get_by_id(wish_id, include_threads=True)["all_comments"]))

    def test_reply_snapshot_keeps_historical_user_comments_replyable(self):
        created = self.store.commit_reflection_action(
            run_id="run-candidate", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "候选留言"},
        )
        wish_id = created[0]["wish_id"]
        first = self.store.add_comment(wish_id, "旧留言", author="user")["comment"]
        board = self.store.get_board()["wishes"][0]
        self.assertEqual(first["id"], board["pending_reply_candidate"]["comment_id"])
        self.assertIn("age", board["pending_reply_candidate"])
        self.assertTrue(next(row for row in board["all_comments"] if row["id"] == first["id"])["replyable"])

        # A later AI comment is historical context, not a gate on the older
        # User comment.
        self.store.add_comment(wish_id, "我看到了", author="k")
        board = self.store.get_board()["wishes"][0]
        self.assertEqual(first["id"], board["pending_reply_candidate"]["comment_id"])
        self.assertIn("历史用户留言也可", self.store.get_context_for_reflection())

        second = self.store.add_comment(wish_id, "补充一个新事实", author="user")["comment"]
        board = self.store.get_board()["wishes"][0]
        self.assertEqual(second["id"], board["pending_reply_candidate"]["comment_id"])
        self.assertTrue(next(row for row in board["all_comments"] if row["id"] == first["id"])["replyable"])

        before = len(board["all_comments"])
        supplemented = self.store.commit_reflection_action(
            run_id="run-candidate", activity_id="activity-2", node_id="node-2",
            action={"type": "none"},
            comment_reply={"wish_id": wish_id, "content": "给旧留言补充一句", "reply_to_comment_id": first["id"]},
        )
        self.assertEqual("commented", supplemented[-1]["outcome"])
        self.assertEqual(before + 1, len(self.store.get_by_id(wish_id, include_threads=True)["all_comments"]))
        context = self.store.get_context_for_reflection()
        self.assertIn("AI直接回复时间=", context)

    def test_reaffirm_duplicate_basis_does_not_increment_but_new_basis_does(self):
        created = self.store.commit_reflection_action(
            run_id="run-basis", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "状态依据"},
        )
        wish_id = created[0]["wish_id"]
        first = self.store.commit_reflection_action(
            run_id="run-basis", activity_id="activity-2", node_id="node-2",
            action={"type": "reaffirm", "wish_id": wish_id, "basis": "用户状态外出·愿望[16]"},
        )
        self.assertEqual("reaffirmed", first[0]["outcome"])
        duplicate = self.store.commit_reflection_action(
            run_id="run-basis", activity_id="activity-3", node_id="node-3",
            action={"type": "reaffirm", "wish_id": wish_id, "basis": "用户 状态外出！愿望16"},
        )
        self.assertEqual("duplicate_basis", duplicate[0]["outcome"])
        self.assertFalse(duplicate[0]["mutation"])
        self.assertEqual(2, self.store.get_by_id(wish_id)["times_wished"])
        newer = self.store.commit_reflection_action(
            run_id="run-basis", activity_id="activity-4", node_id="node-4",
            action={"type": "reaffirm", "wish_id": wish_id, "basis": "用户状态已回到家"},
        )
        self.assertEqual("reaffirmed", newer[0]["outcome"])
        self.assertEqual(3, self.store.get_by_id(wish_id)["times_wished"])
        snapshot = self.store.get_board()["wishes"][0]["reaffirm_basis_history"]
        self.assertEqual(2, len(snapshot))
        self.assertIn("规范化后完全相同不重复计数", self.store.get_context_for_reflection())

    def test_reflection_context_marks_comments_as_timestamped_history(self):
        created = self.store.commit_reflection_action(
            run_id="run-context", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "历史留言来源"},
        )
        wish_id = created[0]["wish_id"]
        comment = self.store.add_comment(wish_id, "忙完再看", author="user")["comment"]

        context = self.store.get_context_for_reflection()

        self.assertIn("来自持久化许愿板", context)
        self.assertIn("快照生成于", context)
        self.assertIn(
            f"comment_id={comment['id']}, author=用户, written_at={comment['created_at']}",
            context,
        )
        self.assertIn("它只是最新留言", context)
        self.assertIn("历史用户留言也可按真实时间", context)

    def test_user_comment_can_be_edited_and_deleted_with_history(self):
        created = self.store.commit_reflection_action(
            run_id="run-5", activity_id="activity-1", node_id="node-1",
            action={"type": "create", "title": "可维护评论"},
        )
        wish_id = created[0]["wish_id"]
        comment = self.store.add_comment(wish_id, "打错字惹", author="user")["comment"]

        edited = self.store.edit_comment(comment["id"], "打错字了", actor="user")
        self.assertTrue(edited["mutated"])
        self.assertEqual("打错字了", edited["comment"]["content"])
        self.assertEqual("打错字了", self.store.get_by_id(wish_id)["user_comment"])

        deleted = self.store.delete_comment(comment["id"], actor="user")
        self.assertTrue(deleted["mutated"])
        wish = self.store.get_by_id(wish_id, include_threads=True)
        self.assertEqual([], wish["all_comments"])
        self.assertEqual("", wish["user_comment"])
        board_wish = next(item for item in self.store.get_board()["wishes"] if item["id"] == wish_id)
        event_types = [event["event_type"] for event in board_wish["history"]]
        self.assertIn("comment_edited", event_types)
        self.assertIn("comment_deleted", event_types)

    def test_legacy_schema_is_read_only_and_pending_is_mapped_at_read(self):
        path = Path(self.tempdir.name) / "legacy.db"
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
            conn.execute("INSERT INTO wishes(feature,status,times_wished) VALUES ('旧愿望','pending',2)")
            conn.commit()
        finally:
            conn.close()

        store = WishStore(str(path))
        self.assertEqual("open", store.list_all()[0]["status"])
        conn = sqlite3.connect(path)
        try:
            self.assertEqual("pending", conn.execute("SELECT status FROM wishes").fetchone()[0])
            self.assertNotIn("updated_at", {row[1] for row in conn.execute("PRAGMA table_info(wishes)")})
        finally:
            conn.close()
        with self.assertRaises(WishMigrationRequired):
            store.set_status(1, "fulfilled")

    def test_legacy_array_is_audit_only(self):
        result = self.store.commit_reflection_wishes(
            run_id="legacy", activity_id="a", node_id="n",
            wishes=[{"feature": "不应创建", "reason": "旧格式"}],
        )
        self.assertEqual("legacy_skipped", result[0]["outcome"])
        self.assertEqual([], self.store.list_all())


if __name__ == "__main__":
    unittest.main()
