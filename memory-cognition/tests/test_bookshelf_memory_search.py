from __future__ import annotations

import unittest
from unittest.mock import patch

from behavior_scheduler.base_tool import ToolStatus
from behavior_scheduler.bookshelf_tool import BookshelfTool


class BookshelfMemorySearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_partition_uses_shared_v2_search_and_detail_flag(self) -> None:
        tool = BookshelfTool()
        with patch(
            "memory_v2.context_shadow.search_memory_v2_from_environment",
            return_value=(
                {
                    "status": "recalled",
                    "rendered_hit_count": 5,
                    "evidence_expanded": True,
                },
                "[主动记忆深搜结果]\n五条摘要和一处原话",
            ),
        ) as search:
            result = await tool.execute(
                source="memory",
                query="以前看展的事",
                detail=True,
                limit=9,
            )

        self.assertEqual(ToolStatus.SUCCESS, result.status)
        self.assertIn("五条摘要和一处原话", result.content)
        self.assertEqual("recalled", result.extra_data["memory_v2_search"]["status"])
        self.assertEqual("以前看展的事", search.call_args.args[0])
        self.assertEqual(9, search.call_args.kwargs["limit"])
        self.assertTrue(search.call_args.kwargs["include_source_detail"])

    async def test_memory_partition_reports_summary_miss_without_fake_content(self) -> None:
        tool = BookshelfTool()
        with patch(
            "memory_v2.context_shadow.search_memory_v2_from_environment",
            return_value=({"status": "no_hits", "rendered_hit_count": 0}, ""),
        ):
            result = await tool.execute(source="memory", query="不存在的往事")

        self.assertEqual(ToolStatus.SUCCESS, result.status)
        self.assertIn("没有找到足以确认", result.content)
        self.assertNotIn("可能发生过", result.content)

    def test_memory_partition_is_described_in_tool_schema(self) -> None:
        tool = BookshelfTool()
        self.assertIn("memory", tool.parameters_schema["properties"]["source"]["enum"])
        self.assertEqual(
            "boolean", tool.parameters_schema["properties"]["detail"]["type"]
        )


if __name__ == "__main__":
    unittest.main()
