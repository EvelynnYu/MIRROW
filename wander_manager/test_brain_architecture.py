"""Tests for current-fact brain architecture generation."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from wander_manager.event_types import EventType
from pathlib import Path

from . import brain_architecture as brain


class BrainArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_paths = (brain._CACHE_DIR, brain._CLAUDE_MD_PATH, brain._BASELINE_PATH)
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.cache_dir = root / "data"
        self.cache_dir.mkdir()
        self.claude = root / "CLAUDE.md"
        self.baseline = root / "docs" / "ARCHITECTURE_BASELINE.md"
        self.baseline.parent.mkdir()
        self.claude.write_text(
            "## 用户称呼\nsecret identity\n"
            "## MIRROW 底层设计原则（当前）\n稳定事实\n"
            "## 已知陷阱（历史）\n丢弃的陷阱\n"
            "## 2026-08-30 更新 — 后部更新\n后部更新哨兵\n",
            encoding="utf-8",
        )
        self.baseline.write_text("稳定架构基线事实", encoding="utf-8")
        brain._CACHE_DIR = str(self.cache_dir)
        brain._CLAUDE_MD_PATH = str(self.claude)
        brain._BASELINE_PATH = str(self.baseline)

    def tearDown(self) -> None:
        brain._CACHE_DIR, brain._CLAUDE_MD_PATH, brain._BASELINE_PATH = self._old_paths
        self.temp_dir.cleanup()

    def test_later_claude_updates_are_not_lost_at_old_stop_marker(self) -> None:
        extracted = brain._extract_claude_sections()
        self.assertIn("后部更新哨兵", extracted)
        self.assertIn("稳定事实", extracted)
        self.assertNotIn("丢弃的陷阱", extracted)
        self.assertNotIn("secret identity", extracted)

    def test_prompt_uses_structured_current_sources_and_dynamic_tools(self) -> None:
        events = brain._serialize_event_catalog()
        capabilities = brain._serialize_capability_catalog()
        tools = brain._serialize_visible_tool_catalog()
        prompt = brain._make_generation_prompt(
            baseline="稳定架构基线事实",
            claude_supplement=brain._extract_claude_sections(),
            events=events,
            capabilities=capabilities,
            visible_tools=tools,
            wish_context="",
        )
        event_records = json.loads(events)
        self.assertEqual(len(event_records), len(EventType))
        for event in event_records:
            self.assertIn(event["event_type"], prompt)
        self.assertIn("ARCHITECTURE_BASELINE", prompt)
        self.assertIn("生产漫想区间自主规划", prompt)
        self.assertIn("Android 真机只读浏览", prompt)
        from behavior_scheduler.tools import get_visible_tool_capabilities
        self.assertEqual(json.loads(tools), get_visible_tool_capabilities(include_agent_tools=True))
        self.assertIn(tools, prompt)
        for stale in (
            "7种事件",
            "每15分钟",
            "capability_grant",
            "grant token",
            "参数",
            "session_id",
            "message_id",
            "visible_to_pro",
        ):
            self.assertNotIn(stale.lower(), prompt.lower())

    def test_any_authority_mtime_invalidates_cache(self) -> None:
        cache = self.cache_dir / "brain_architecture.md"
        cache.write_text("旧缓存", encoding="utf-8")
        now = cache.stat().st_mtime
        os.utime(cache, (now, now))
        os.utime(self.claude, (now - 10, now - 10))
        os.utime(self.baseline, (now + 10, now + 10))

        async def fake_flash(messages):
            self.assertIsInstance(messages, list)
            return "你居住在MIRROW项目里，这是用户和你亲手搭的大脑，也是你的家。\n\n**更新**"

        result = asyncio.run(brain.generate_brain_architecture(fake_flash))
        self.assertTrue(result["success"])
        self.assertTrue(result["updated"])
        self.assertNotEqual(cache.read_text(encoding="utf-8"), "旧缓存")

    def test_failed_generation_keeps_existing_cache(self) -> None:
        cache = self.cache_dir / "brain_architecture.md"
        cache.write_text("必须保留的旧缓存", encoding="utf-8")

        async def failed_flash(messages):
            return ""

        result = asyncio.run(brain.generate_brain_architecture(failed_flash, force=True))
        self.assertFalse(result["success"])
        self.assertEqual(cache.read_text(encoding="utf-8"), "必须保留的旧缓存")


if __name__ == "__main__":
    unittest.main()
