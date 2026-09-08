"""Focused regressions for truthful Wander handler boundaries."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from behavior_scheduler.base_tool import ToolStatus
from wander_manager.event_handlers import BrowseNewsHandler, KeywordExpansionHandler, ListenMusicHandler


class _MusicTool:
    def __init__(self, search_result):
        self.search_result = search_result
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in {"search", "search_song"}:
            return self.search_result
        return {"success": True}


class _MusicCaller:
    def __init__(self, response):
        self.response = response

    async def __call__(self, _messages):
        return self.response


class _ReadOnlyChronicle:
    """Avoid touching MIRROW's production conversation database in tests."""

    def get_messages_by_date(self, _date):
        return []

    def get_diary_entry_by_date(self, _date):
        return None

    def get_or_create_default_room(self):
        return "test-room"

    def get_group_messages(self, _room_id, limit=20):
        return []


class WanderHandlerReliabilityTests(unittest.TestCase):
    def test_keyword_expansion_uses_llm_contract_without_fixed_fallback(self):
        response = '{"keyword":"量子耳鸣","expansion":"突然想知道它会不会影响听觉。"}'
        handler = KeywordExpansionHandler(call_llm_func=_MusicCaller(response))
        result = asyncio.run(handler.fetch_one_expansion())
        self.assertEqual("量子耳鸣", result["keyword"])
        self.assertNotIn("星空", result["keyword"])

        malformed = KeywordExpansionHandler(call_llm_func=_MusicCaller("坏输出"))
        self.assertIsNone(asyncio.run(malformed.fetch_one_expansion()))

    def test_news_prompt_has_no_fixed_category_and_no_pool_write(self):
        prompts = []

        async def llm(messages):
            prompts.append(messages[0]["content"])
            return "一个临时好奇的问题"

        async def search(_query):
            return "一条真实结果"

        handler = BrowseNewsHandler(call_llm_func=llm, web_search_func=search)
        result = asyncio.run(handler.fetch_one_news())
        self.assertEqual("success", result["status"])
        self.assertNotIn("新闻热点、科技趣闻", prompts[0])
        self.assertNotIn("keyword_pool", prompts[0])

    def test_music_search_rejects_keyword_as_fake_song(self):
        tool = _MusicTool({"success": True, "songs": [{"name": "搜索词", "artist": "未知"}]})
        handler = ListenMusicHandler(
            pro_llm_func=_MusicCaller("{}"),
            music_mcp_client=tool,
        )
        self.assertIsNone(asyncio.run(handler._search_and_analyze("搜索词")))
        self.assertEqual(["search_song"], [name for name, _args in tool.calls])

    def test_music_selection_parse_failure_does_not_inject_zhou_jielun(self):
        handler = ListenMusicHandler(pro_llm_func=_MusicCaller("not-json"))
        result = asyncio.run(handler._call_llm_json("select"))
        self.assertEqual({}, result)
        self.assertNotIn("周杰伦", result.values())

    def test_cache_fingerprint_mismatch_gets_one_candidate_facts_correction(self):
        candidate = {
            "title": "缓存歌", "artist": "歌手", "fingerprint": "fp-good",
            "netease_song_id": "123", "duration_sec": 180,
        }

        class Cache:
            def get_cached_playlist(self, _limit): return [candidate]

        class Sequenced:
            def __init__(self): self.calls = []
            async def __call__(self, messages):
                self.calls.append(messages[0]["content"])
                return {"from_cache": True, "fingerprint": "fp-bad" if len(self.calls) == 1 else "fp-good"}

        picker = Sequenced()
        handler = ListenMusicHandler(pro_llm_func=picker, song_cache=Cache())
        result = asyncio.run(handler.pick_and_fetch_song())
        self.assertEqual("fp-good", result["fingerprint"])
        self.assertEqual(2, len(picker.calls))
        self.assertIn("candidate_count", picker.calls[1])
        # The bounded correction deliberately keeps the first selection error
        # in the handler audit for node-level evidence.
        self.assertEqual("cache_fingerprint_not_found", handler.get_last_selection_audit()["error"])

    def test_invalid_cache_choice_with_empty_candidates_fails_truthfully_after_retry(self):
        class Cache:
            def get_cached_playlist(self, _limit): return []

        class Sequenced:
            def __init__(self): self.calls = 0
            async def __call__(self, _messages):
                self.calls += 1
                return {"from_cache": True, "fingerprint": "not-present"}

        picker = Sequenced()
        handler = ListenMusicHandler(pro_llm_func=picker, song_cache=Cache())
        self.assertIsNone(asyncio.run(handler.pick_and_fetch_song()))
        self.assertEqual(2, picker.calls)
        audit = handler.get_last_selection_audit()
        self.assertEqual("cache_fingerprint_not_found", audit["error"])
        self.assertEqual(0, audit["payload"]["candidate_count"])
        self.assertNotIn("title", audit["payload"])

    @unittest.skip("Private CloudMusicTool not shipped; public playback hook has separate tests")
    def test_runtime_playback_routes_both_targets_through_cloud_tool(self):
        handler = ListenMusicHandler(music_mcp_client=_MusicTool({}))
        result = SimpleNamespace(
            status=ToolStatus.SUCCESS, error="", extra_data={"duration": 120000}
        )
        with patch(
            "behavior_scheduler.cloud_music_tool.CloudMusicTool.play_song_by_id",
            new=AsyncMock(return_value=result),
        ) as play, patch.object(handler, "_call_music_tool", new=AsyncMock(
            side_effect=AssertionError("runtime playback must not use legacy MCP")
        )):
            self.assertTrue(asyncio.run(handler.start_runtime_playback(
                "123", target="computer", song_data={"title": "桌面歌", "artist": "歌手"}
            )))
            self.assertTrue(asyncio.run(handler.start_runtime_playback(
                "456", target="mobile", song_data={"title": "手机歌", "artist": "歌手", "duration_sec": 90}
            )))

        self.assertEqual(["computer", "mobile"], [call.kwargs["target"] for call in play.await_args_list])
        self.assertEqual(["123", "456"], [call.args[0] for call in play.await_args_list])

    @unittest.skip("Private CloudMusicTool not shipped; public playback hook has separate tests")
    def test_mobile_runtime_failure_never_falls_back_to_computer(self):
        handler = ListenMusicHandler(music_mcp_client=_MusicTool({}))
        failure = SimpleNamespace(
            status=ToolStatus.ERROR, error="手机后台未连接", extra_data=None
        )
        with patch(
            "behavior_scheduler.cloud_music_tool.CloudMusicTool.play_song_by_id",
            new=AsyncMock(return_value=failure),
        ) as play, patch.object(handler, "_call_music_tool", new=AsyncMock(
            side_effect=AssertionError("mobile failure must not use computer MCP")
        )):
            self.assertFalse(asyncio.run(handler.start_runtime_playback(
                "456", target="mobile", song_data={"title": "手机歌", "artist": "歌手"}
            )))
        play.assert_awaited_once()
        self.assertEqual("mobile", handler._last_runtime_playback["device"])
        self.assertIn("手机后台未连接", handler._last_runtime_playback["error"])


if __name__ == "__main__":
    unittest.main()
