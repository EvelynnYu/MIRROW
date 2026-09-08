"""Deterministic tests for the read-only Xiaohongshu material boundary."""

from __future__ import annotations

import asyncio
import unittest

from wander_manager.dots_client import DotsJsonResult
from wander_manager.xiaohongshu_handler import BrowseXiaohongshuHandler
from wander_manager.xiaohongshu_public_source import PublicPostCard, PublicSourceResult


class FakeDots:
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeSource:
    def __init__(self, result):
        self.result = result

    async def fetch_cards(self, **_kwargs):
        return self.result


class XiaohongshuHandlerTests(unittest.TestCase):
    def test_keeps_only_evidence_backed_xiaohongshu_url(self):
        dots = FakeDots([
            DotsJsonResult(parsed={"query": "租房书桌收纳", "mode": "goal"}, status="ok"),
            DotsJsonResult(parsed={
                "title": "一平米书桌收纳",
                "summary": "利用竖向隔板整理桌面。",
                "evidence_excerpt": "一平米书桌也能利用竖向空间",
                "media_kind": "image_cover",
                "limitations": "只读到文字摘要",
                "evidence_sufficient": True,
            }, model="dots-test", status="ok"),
        ])

        source = FakeSource(PublicSourceResult(status="success", cards=(
            PublicPostCard("abc123", "https://www.xiaohongshu.com/explore/abc123",
                           "一平米书桌也能利用竖向空间", "https://img.example/cover.webp"),
        )))

        payload = asyncio.run(BrowseXiaohongshuHandler(
            dots_client=dots, public_source=source,
        ).fetch_one_post(activity_reason="找收纳攻略"))
        self.assertEqual("success", payload["status"])
        self.assertEqual("https://www.xiaohongshu.com/explore/abc123", payload["source_url"])
        self.assertEqual("image_cover_and_text", payload["media_observation"])
        self.assertEqual(["https://img.example/cover.webp"], dots.calls[1]["image_urls"])

    def test_rejects_page_unavailable_summary(self):
        dots = FakeDots([
            DotsJsonResult(parsed={"query": "猫咪玩具测评", "mode": "wander"}, status="ok"),
            DotsJsonResult(parsed={
                "title": "未知",
                "summary": "页面不可用。",
                "evidence_excerpt": "",
                "media_kind": "unknown",
                "limitations": "没有读到帖子",
                "evidence_sufficient": False,
            }, status="ok"),
        ])

        source = FakeSource(PublicSourceResult(status="success", cards=(
            PublicPostCard("abc123", "https://www.xiaohongshu.com/explore/abc123",
                           "页面不可用"),
        )))

        payload = asyncio.run(BrowseXiaohongshuHandler(
            dots_client=dots, public_source=source,
        ).fetch_one_post())
        self.assertEqual("insufficient_evidence", payload["status"])


if __name__ == "__main__":
    unittest.main()
