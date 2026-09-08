"""Isolated tests for bookmark/iceberg material and AI-only actions."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime

from wander_manager.bookmark_action_adapter import BookmarkActionAdapter
from wander_manager.event_handlers import BrowseBookmarksHandler
from wander_manager.historical_reader import HistoricalConversationReader


class _FixedRng:
    def __init__(self, owner="k"):
        self.owner = owner

    def choice(self, values):
        if self.owner in values:
            return self.owner
        return values[0]

    def sample(self, values, count):
        return list(values)[:count]

    def randint(self, lo, _hi):
        return lo

    def shuffle(self, values):
        return None


class _Chronicle:
    def __init__(self):
        self.messages = {}
        self.bookmarks = []
        self.next_id = 1

    def get_message_by_msg_id(self, message_id):
        return self.messages.get(message_id)

    def list_bookmarks(self, collected_by=None, offset=0, limit=20):
        rows = [
            dict(row) for row in self.bookmarks
            if collected_by is None or row.get("collected_by") == collected_by
        ]
        return rows[offset:offset + limit], len(rows)

    def add_bookmark(self, **kwargs):
        bookmark_id = f"bm-{self.next_id}"
        self.next_id += 1
        row = {
            "id": bookmark_id,
            "created": "2026-08-31T12:00:00",
            **kwargs,
        }
        self.bookmarks.append(row)
        return bookmark_id

    def get_bookmark_meta_by_bucket(self, bucket_id):
        return next((row for row in self.bookmarks if row.get("bucket_id") == bucket_id), None)

    def delete_bookmark_meta(self, bucket_id):
        before = len(self.bookmarks)
        self.bookmarks[:] = [row for row in self.bookmarks if row.get("bucket_id") != bucket_id]
        return len(self.bookmarks) < before

    def get_messages_by_date(self, active_date):
        return [row for row in self.messages.values() if row.get("active_date") == active_date]

    def list_active_dates(self, before_date=None, limit=90):
        return ["2026-08-30", "2026-08-31"][:limit]

    def get_message_context_by_id(self, session_id, message_id, before=3, after=3):
        row = self.messages.get(message_id)
        return [], row, []


def _message(message_id, role="user", date="2026-08-20", content="原始消息"):
    return {
        "message_id": message_id,
        "session_id": "session",
        "active_date": date,
        "calendar_date": date,
        "timestamp": f"{date}T10:00:00+08:00",
        "role": role,
        "content": content,
    }


class BookmarkIcebergTests(unittest.TestCase):
    def setUp(self):
        self.chronicle = _Chronicle()
        self.chronicle.messages.update({
            "u-old": _message("u-old", content="历史用户原话"),
            "a-old": _message("a-old", role="assistant", content="历史 AI 原话"),
            "u-day": _message("u-day", date="2026-08-30", content="随机日用户原话"),
            "notification": _message("notification", role="notification", date="2026-08-30", content="UI 标记"),
        })
        self.scene = {"id": "scene-old", "active_date": "2026-08-20", "session_id": "session"}
        self.reader = HistoricalConversationReader(
            chronicle=self.chronicle,
            scene_picker=lambda **_kwargs: self.scene,
            scene_message_loader=lambda _scene, **_kwargs: [
                self.chronicle.messages["u-old"], self.chronicle.messages["a-old"]
            ],
            memory_buckets=[],
            now=lambda: datetime(2026, 8, 31, 12, 0),
            rng=_FixedRng(),
        )

    @staticmethod
    def _llm(action_type="none", target_index=None):
        async def call(messages):
            return {"content": json.dumps({
                "reflection": "看到了真实回看的语气。",
                "bookmark_action": {
                    "type": action_type,
                    "target_index": target_index,
                    "reason": "测试动作",
                },
            }, ensure_ascii=False)}
        return call

    def test_unbookmarked_anchor_reads_scene_messages_and_can_add_k_bookmark(self):
        handler = BrowseBookmarksHandler(
            self._llm("add", 1),
            chronicle=self.chronicle,
            historical_reader=self.reader,
            action_adapter=BookmarkActionAdapter(self.chronicle),
            source_mode="unbookmarked_anchor",
            rng=_FixedRng(),
        )
        payload = asyncio.run(handler.fetch_one_bookmark(
            run_id="run", activity_id="activity", node_id="node"
        ))
        self.assertEqual("unbookmarked_anchor", payload["source_mode"])
        self.assertEqual("scene-old", payload["scene_id"])
        self.assertEqual(["u-old", "a-old"], payload["candidate_message_ids"])
        self.assertEqual("applied", payload["bookmark_action"]["status"])
        self.assertEqual("k", self.chronicle.bookmarks[0]["collected_by"])
        # Runtime evidence is compact; raw scene text is prompt-local only.
        self.assertNotIn("snapshot", payload)
        self.assertNotIn("context_text", payload)

    def test_random_day_excludes_today_and_ui_rows(self):
        handler = BrowseBookmarksHandler(
            self._llm(),
            chronicle=self.chronicle,
            historical_reader=self.reader,
            action_adapter=BookmarkActionAdapter(self.chronicle),
            source_mode="random_day",
            rng=_FixedRng(),
        )
        payload = asyncio.run(handler.fetch_one_bookmark(
            run_id="run", activity_id="activity", node_id="node"
        ))
        self.assertEqual("random_day", payload["source_mode"])
        self.assertEqual("2026-08-30", payload["active_date"])
        self.assertNotIn("2026-08-31", payload["active_date"])
        self.assertIn("u-day", payload["candidate_message_ids"])
        self.assertNotIn("notification", payload["candidate_message_ids"])

    def test_remove_requires_displayed_k_bookmark_and_protects_user_bookmark(self):
        k_row = {
            "id": "bm-k", "bucket_id": "bucket-k", "original_msg_id": "u-old",
            "session_id": "session", "original_timestamp": "2026-08-20T10:00:00+08:00",
            "collected_by": "k", "role": "user", "content": "历史用户原话",
        }
        self.chronicle.bookmarks.append(k_row)
        handler = BrowseBookmarksHandler(
            self._llm("remove", 1),
            chronicle=self.chronicle,
            historical_reader=self.reader,
            action_adapter=BookmarkActionAdapter(self.chronicle),
            source_mode="existing_bookmark",
            rng=_FixedRng("k"),
        )
        payload = asyncio.run(handler.fetch_one_bookmark(
            run_id="run", activity_id="activity", node_id="node"
        ))
        self.assertEqual("applied", payload["bookmark_action"]["status"])
        self.assertEqual([], self.chronicle.bookmarks)

        user_row = dict(k_row, id="bm-user", bucket_id="bucket-user", collected_by="user")
        self.chronicle.bookmarks.append(user_row)
        handler = BrowseBookmarksHandler(
            self._llm("remove", 1),
            chronicle=self.chronicle,
            historical_reader=self.reader,
            action_adapter=BookmarkActionAdapter(self.chronicle),
            source_mode="existing_bookmark",
            rng=_FixedRng("user"),
        )
        payload = asyncio.run(handler.fetch_one_bookmark(
            run_id="run", activity_id="activity", node_id="node-2"
        ))
        self.assertEqual("rejected", payload["bookmark_action"]["status"])
        self.assertEqual("user", self.chronicle.bookmarks[0]["collected_by"])

    def test_action_is_idempotent_and_rejects_target_outside_snapshot(self):
        snapshot = {
            "candidates": [{
                "message_id": "u-old", "session_id": "session", "role": "user",
                "timestamp": "2026-08-20T10:00:00+08:00", "content": "历史用户原话",
            }],
        }
        adapter = BookmarkActionAdapter(self.chronicle)
        decision = {"type": "add", "target_index": 1}
        first = adapter.apply(decision, snapshot, source_key="run:activity:node")
        second = adapter.apply(decision, snapshot, source_key="run:activity:node")
        self.assertEqual("applied", first["status"])
        self.assertEqual("already_bookmarked", second["status"])
        self.assertEqual(1, len(self.chronicle.bookmarks))
        outside = adapter.apply(
            {"type": "add", "message_id": "not-displayed"}, snapshot,
            source_key="run:activity:node-2",
        )
        self.assertEqual("rejected", outside["status"])
        self.assertEqual("target_not_in_snapshot", outside["reason"])


if __name__ == "__main__":
    unittest.main()
