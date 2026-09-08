"""Regression coverage for public-distribution privacy boundaries."""

from __future__ import annotations

import os
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_builder.builder import ContextBuilder
from context_builder.recipes import RecipeSection
from context_builder.ingredients import build_timeline_anchor, build_host_group_current
from wander_manager.wish_board_service import WishMigrationRequired, WishStore


class PublicSanitizationTests(unittest.TestCase):
    def setUp(self):
        self._timeline = os.environ.pop("PERSONA_TIMELINE_ANCHOR", None)
        self._legacy = os.environ.pop("MIRROW_LEGACY_USER_ROLE", None)

    def tearDown(self):
        for key, value in (("PERSONA_TIMELINE_ANCHOR", self._timeline), ("MIRROW_LEGACY_USER_ROLE", self._legacy)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_timeline_is_blank_until_host_configures_it(self):
        self.assertEqual("", build_timeline_anchor())
        os.environ["PERSONA_TIMELINE_ANCHOR"] = "host supplied timeline"
        self.assertEqual("host supplied timeline", build_timeline_anchor())

    def test_group_context_without_room_never_reads_a_provider(self):
        provider = Mock()
        with patch.dict("sys.modules", {"event_chronicle": SimpleNamespace(get_global_chronicle=provider)}):
            self.assertEqual("", asyncio.run(build_host_group_current()))
        provider.assert_not_called()

    def test_legacy_self_memory_subject_stays_in_self_block(self):
        builder = ContextBuilder()
        builder._kwargs = {"memories": [SimpleNamespace(
            content="stored self memory", metadata={"subject": "K"},
        )]}
        with patch("context_builder.builder.guide_for", side_effect=lambda key, _: key):
            result = asyncio.run(builder._call_ingredient(RecipeSection("memory", "memories")))
        self.assertTrue(result.startswith("self_memories\n"))

    def _legacy_role_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE wishes (id INTEGER PRIMARY KEY, feature TEXT, reason TEXT, status TEXT,
                times_wished INTEGER, first_wished_at TEXT, last_wished_at TEXT, user_comment TEXT,
                comment_updated_at TEXT, fulfilled_at TEXT, fulfilled_note TEXT, updated_at TEXT,
                deleted_at TEXT, merged_into_id INTEGER);
            INSERT INTO wishes VALUES (7, 'x', '', 'open', 1, '', '', '', '', '', '', '', '', NULL);
            CREATE TABLE wish_comments (id INTEGER PRIMARY KEY, wish_id INTEGER, author TEXT CHECK(author IN ('k','legacy_user','system')), content TEXT, reply_to_comment_id INTEGER, source_key TEXT UNIQUE, created_at TEXT);
            INSERT INTO wish_comments VALUES (11, 7, 'legacy_user', 'parent', NULL, 'c1', '2026-01-01');
            INSERT INTO wish_comments VALUES (12, 7, 'k', 'reply', 11, 'c2', '2026-01-02');
            CREATE TABLE wish_events (id INTEGER PRIMARY KEY, wish_id INTEGER, actor TEXT CHECK(actor IN ('k','legacy_user','system','migration')), event_type TEXT, content TEXT, payload_json TEXT, source_key TEXT UNIQUE, created_at TEXT);
            INSERT INTO wish_events VALUES (13, 7, 'legacy_user', 'comment', '', '{}', 'e1', '2026-01-01');
        """)
        conn.commit(); conn.close()

    def test_role_migration_preserves_ids_and_reply_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wishes.db"; self._legacy_role_db(path)
            with self.assertRaises(WishMigrationRequired): WishStore(str(path), migrate=True)
            os.environ["MIRROW_LEGACY_USER_ROLE"] = "legacy_user"
            store = WishStore(str(path), migrate=True)
            comments = store.list_all(include_threads=True)[0]["all_comments"]
            self.assertEqual([(11, "user", None), (12, "k", 11)], [(x["id"], x["author"], x["reply_to_comment_id"]) for x in comments])
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual([(11, "c1"), (12, "c2")], conn.execute("SELECT id,source_key FROM wish_comments ORDER BY id").fetchall())
                self.assertEqual((13, "user", "e1"), conn.execute("SELECT id,actor,source_key FROM wish_events WHERE id=13").fetchone())
            self.assertFalse(store.migrate())

    def test_failure_in_second_role_table_rolls_back_both_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wishes.db"
            self._legacy_role_db(path)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("DROP TABLE wish_events")
                conn.execute("CREATE TABLE wish_events (id INTEGER PRIMARY KEY, wish_id INTEGER, actor TEXT, event_type TEXT, content TEXT, payload_json TEXT, source_key TEXT UNIQUE, created_at TEXT)")
                conn.execute("INSERT INTO wish_events VALUES (13, 7, 'unknown_actor', 'comment', '', '{}', 'e1', '')")
                conn.commit()
                before = "\n".join(conn.iterdump())
            os.environ["MIRROW_LEGACY_USER_ROLE"] = "legacy_user"
            with self.assertRaises(sqlite3.IntegrityError):
                WishStore(str(path), migrate=True)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(before, "\n".join(conn.iterdump()))

    def test_invalid_role_mapping_rolls_back_without_rewriting_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wishes.db"; self._legacy_role_db(path)
            os.environ["MIRROW_LEGACY_USER_ROLE"] = "k"
            with self.assertRaises(WishMigrationRequired):
                WishStore(str(path), migrate=True)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT author FROM wish_comments WHERE id=11").fetchone()
            tables = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertEqual("legacy_user", row[0])
            self.assertNotIn("wish_comments_new", tables)

    def test_old_wishes_only_schema_needs_no_role_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wishes.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE wishes (id INTEGER PRIMARY KEY, feature TEXT, reason TEXT, status TEXT, times_wished INTEGER, first_wished_at TEXT, last_wished_at TEXT, user_comment TEXT, comment_updated_at TEXT, fulfilled_at TEXT, fulfilled_note TEXT)")
            conn.commit(); conn.close()
            self.assertEqual("current", WishStore(str(path), migrate=True)._schema_state)
