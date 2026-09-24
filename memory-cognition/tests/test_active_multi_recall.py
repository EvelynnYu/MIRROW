from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from behavior_scheduler.base_tool import ToolStatus
from behavior_scheduler.bookshelf_tool import BookshelfTool
from memory_v2.active_multi_recall import _candidate_date, search_active_stages
from memory_v2.active_query_plan import ActiveQueryPlan, plan_from_stage_queries
from memory_v2.conversation_source import ConversationMessage
from memory_v2.recall import RecallDocument, RecallHit, RecallQuery, RecallResult


def _hit(name: str, day: str, summary: str, score: float) -> RecallHit:
    document = RecallDocument(
        document_id=f"event:{name}",
        event_ids=(name,),
        source_message_ids=(f"message:{name}",),
        summaries=(summary,),
        subject_ids=("human",),
        participant_ids=("human", "agent"),
        event_types=("episode",),
        facets=(),
        source_kinds=("chat",),
        active_date_from=day,
        active_date_to=day,
        date_from=day,
        date_to=day,
        importance=0.8,
        confidence=0.9,
    )
    return RecallHit(
        document=document,
        score=score,
        lexical_score=score,
        fuzzy_score=0.0,
        semantic_score=None,
        rarity_score=0.0,
        direct_match=True,
        matched_terms=(),
    )


class _Source:
    def __init__(self, hits: tuple[RecallHit, ...]):
        self.messages = {
            hit.document.source_message_ids[0]: ConversationMessage(
                row_id=index + 1,
                active_date=hit.document.date_from,
                calendar_date=hit.document.date_from,
                session_id="main",
                timestamp=f"{hit.document.date_from}T{(index + 8):02d}:00:00+08:00",
                role="user",
                content="source text",
                message_id=hit.document.source_message_ids[0],
            )
            for index, hit in enumerate(hits)
        }

    def read_message_ids(self, ids):
        return tuple(self.messages[value] for value in ids if value in self.messages)


class _Index:
    def __init__(self, answers):
        self.answers = answers
        self.queries: list[RecallQuery] = []

    def search(self, query: RecallQuery, *, limit: int) -> RecallResult:
        self.queries.append(query)
        hits = self.answers.get((query.text, query.date_from), ())
        return RecallResult(
            hits=tuple(hits[:limit]),
            candidate_count=12,
            semantic_available=False,
            semantic_status="disabled",
            evaluated_count=12,
        )


class ActiveMultiRecallTests(unittest.TestCase):
    def test_original_question_breaks_tie_between_two_supported_dates(self) -> None:
        right = _hit("right", "2025-01-10", "下午的事", 0.12)
        wrong = _hit("wrong", "2025-02-01", "相机的事", 0.4)
        chosen, count = _candidate_date((
            (right, wrong),
            (wrong, right),
            (wrong, right),
        ))
        self.assertEqual("2025-01-10", chosen)
        self.assertEqual(2, count)

    def test_explicit_stage_contract_is_bounded_and_optional(self) -> None:
        self.assertEqual(
            ActiveQueryPlan("起点", ("后续",)),
            plan_from_stage_queries(["起点", "后续"]),
        )
        for invalid in (None, [], ["只有一条"], ["重复", "重复"], ["短", 2]):
            self.assertIsNone(plan_from_stage_queries(invalid))
        self.assertIsNone(plan_from_stage_queries(["一", "后续"]))
        self.assertIsNone(plan_from_stage_queries(["起点"] * 5))

    def test_agreed_date_projects_separate_stages_without_unrelated_time_hit(self) -> None:
        day = "2025-01-10"
        early = _hit("early", day, "早上计划看展", 0.95)
        middle = _hit("middle", day, "晚上回顾展览，提及一幅画", 0.9)
        late = _hit("late", day, "深夜又补充观展感想", 0.8)
        decoy = _hit("decoy", day, "上午讨论工作状态", 0.7)
        answers = {
            ("看展后来聊到深夜", ""): (early, late),
            ("看展", ""): (early, middle),
            ("展览", ""): (middle,),
            ("深夜 感想", ""): (late, decoy),
            ("看展", day): (early, middle),
            ("展览", day): (middle,),
            ("深夜 感想", day): (decoy, late),
        }
        index = _Index(answers)
        result = search_active_stages(
            index,  # type: ignore[arg-type]
            _Source((early, middle, late, decoy)),  # type: ignore[arg-type]
            "看展后来聊到深夜",
            ActiveQueryPlan("看展", ("展览", "深夜 感想")),
            reference_date=date(2025, 2, 10),
        )
        self.assertTrue(result.date_scope_used)
        self.assertEqual(3, result.selected_hit_count)
        self.assertIn("早上计划看展", result.text)
        self.assertIn("晚上回顾展览", result.text)
        self.assertIn("深夜又补充观展感想", result.text)
        self.assertNotIn("工作状态", result.text)
        self.assertNotIn("message:", str(result.safe_observation()))

    def test_explicit_cross_day_question_never_collapses_to_one_date(self) -> None:
        first = _hit("first", "2025-01-10", "盆栽换盆", 0.9)
        later = _hit("later", "2025-01-12", "几天后叶子恢复", 0.8)
        index = _Index({
            ("盆栽换盆几天后怎样", ""): (first, later),
            ("盆栽换盆", ""): (first,),
            ("几天后 叶子", ""): (later, first),
        })
        result = search_active_stages(
            index,  # type: ignore[arg-type]
            _Source((first, later)),  # type: ignore[arg-type]
            "盆栽换盆几天后怎样",
            ActiveQueryPlan("盆栽换盆", ("几天后 叶子",)),
            reference_date=date(2025, 2, 10),
        )
        self.assertFalse(result.date_scope_used)
        self.assertIn("2025年1月10日", result.text)
        self.assertIn("2025年1月12日", result.text)
        self.assertFalse(any(query.date_from for query in index.queries))


class ActiveMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_stage_queries_reach_the_shared_recall_path(self) -> None:
        with (
            patch(
                "memory_v2.context_shadow.search_memory_v2_from_environment",
                return_value=(
                    {"status": "recalled", "multi_stage": True},
                    "[source-backed staged result]",
                ),
            ) as search,
        ):
            result = await BookshelfTool().execute(
                source="memory", query="看展后来为何聊到深夜",
                stage_queries=["看展", "展览", "深夜"],
            )
        self.assertEqual(ToolStatus.SUCCESS, result.status)
        self.assertEqual(
            ActiveQueryPlan("看展", ("展览", "深夜")),
            search.call_args.kwargs["active_plan"],
        )
        self.assertEqual(
            "agent_action",
            result.extra_data["memory_v2_search"]["stage_search"]["source"],
        )

    async def test_no_stages_preserves_ordinary_active_search(self) -> None:
        with (
            patch(
                "memory_v2.context_shadow.search_memory_v2_from_environment",
                return_value=({"status": "no_hits"}, ""),
            ) as search,
        ):
            result = await BookshelfTool().execute(
                source="memory", query="一件模糊旧事"
            )
        self.assertEqual(ToolStatus.SUCCESS, result.status)
        self.assertIsNone(search.call_args.kwargs["active_plan"])
        self.assertEqual(
            "none",
            result.extra_data["memory_v2_search"]["stage_search"]["source"],
        )

    async def test_compiler_omits_query_but_explicit_stages_still_search(self) -> None:
        with patch(
            "memory_v2.context_shadow.search_memory_v2_from_environment",
            return_value=({"status": "recalled"}, "[staged result]"),
        ) as search:
            result = await BookshelfTool().execute(
                source="memory", stage_queries=["看展", "展览", "深夜感想"]
            )
        self.assertEqual(ToolStatus.SUCCESS, result.status)
        self.assertEqual("看展；展览；深夜感想", search.call_args.args[0])
        self.assertEqual(
            ActiveQueryPlan("看展", ("展览", "深夜感想")),
            search.call_args.kwargs["active_plan"],
        )


if __name__ == "__main__":
    unittest.main()
