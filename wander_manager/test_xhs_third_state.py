"""Security and routing tests for the zero-parameter XHS third-state gate.

All tests use in-memory runners or the v3 runtime's fake executor.  No phone,
ADB command, network request, or real user message is triggered here.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from behavior_scheduler.base_tool import BaseTool, ToolResult, ToolStatus
from behavior_scheduler.command_layer import CommandExecutor
from behavior_scheduler.tool_standardizer import ToolCallStandardizer
from wander_manager.xhs_third_state import (
    THIRD_STATE_TOOL_NAME,
    XHS_CAPABILITY,
    XhsCapabilityGate,
    XhsThirdStateTool,
    bind_grant,
    reset_grant,
)


class XhsCapabilityGateTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.gate = XhsCapabilityGate(clock=lambda: self.now[0], ttl_seconds=10)

    def _issue(self, text: str = "请搜索小红书猫咪收纳", message_id: str = "message-a"):
        return self.gate.issue(
            session_id="session-a",
            message_id=message_id,
            turn_id=f"turn-{message_id}",
            user_message=text,
        )

    def test_real_recommend_phrases_match_with_empty_query(self):
        phrases = (
            "😌你看看能不能刷小红书了？安的新功能~",
            "能刷小红书了吗",
            "试试刷小红书",
        )
        for index, text in enumerate(phrases):
            issue = self._issue(text, f"message-{index}")
            self.assertTrue(issue.matched, text)
            self.assertEqual(issue.grant.allowed_intents, ("recommend",))
            self.assertEqual(issue.grant.bound_query, "")
            self.assertNotIn(THIRD_STATE_TOOL_NAME, issue.dynamic_fact)
            self.assertNotIn("parameters", issue.dynamic_fact)
            self.assertNotIn("intent", issue.dynamic_fact)
            self.assertNotIn("query", issue.dynamic_fact)
            self.assertNotIn("{", issue.dynamic_fact)
            self.assertNotIn("}", issue.dynamic_fact)
            self.assertNotIn(issue.grant.token, issue.dynamic_fact)
            self.assertNotIn(text, issue.dynamic_fact)
            self.assertIn("尚未执行", issue.dynamic_fact)

    def test_separated_platform_and_action_match_the_same_explicit_request(self):
        phrases = (
            # Regression for the chat sentence that previously fell through
            # because "小红书" and "刷" were separated by a status clause.
            "摸鱼呢，你想我想得真频繁，小红书权限问题说是可以了，你看看能刷不？能刷你就去刷刷你感兴趣的帖子。",
            "小红书权限确认了，帮我搜索猫咪收纳",
            "小红书连接好了，帮我读一下收纳帖子",
            "小红书状态正常了，你去刷刷感兴趣的帖子",
        )
        expected = ("recommend", "search", "read", "recommend")
        for index, (text, intent) in enumerate(zip(phrases, expected)):
            issue = self._issue(text, f"separated-{index}")
            self.assertTrue(issue.matched, text)
            self.assertEqual(issue.grant.allowed_intents, (intent,), text)

        self.assertEqual(
            self._issue(phrases[1], "separated-query").grant.bound_query,
            "猫咪收纳",
        )
        self.assertEqual(
            self._issue(phrases[2], "separated-read-query").grant.bound_query,
            "收纳帖子",
        )

    def test_permission_discussion_and_failed_or_negated_action_do_not_issue(self):
        phrases = (
            "小红书权限是什么",
            "小红书刷不了",
            "小红书权限问题，你看看能不能刷",
            "不要刷小红书",
            "小红书很好看。我要去刷微博",
        )
        for index, text in enumerate(phrases):
            issue = self._issue(text, f"separated-negative-{index}")
            self.assertFalse(issue.matched, text)
            self.assertIsNone(issue.grant, text)

    def test_explicit_search_topic_is_the_only_search_query(self):
        search = self._issue("请搜索小红书猫咪收纳", "search")
        self.assertEqual(search.grant.allowed_intents, ("search",))
        self.assertEqual(search.grant.bound_query, "猫咪收纳")

        colon_search = self._issue("小红书搜索：猫咪收纳", "colon")
        self.assertEqual(colon_search.grant.allowed_intents, ("search",))
        self.assertEqual(colon_search.grant.bound_query, "猫咪收纳")

        recommendation = self._issue("请刷小红书看看关于猫咪的帖子", "topic")
        self.assertEqual(recommendation.grant.allowed_intents, ("recommend",))
        self.assertEqual(recommendation.grant.bound_query, "猫咪")

        implicit = self._issue("请刷小红书看看猫咪", "implicit")
        self.assertEqual(implicit.grant.allowed_intents, ("recommend",))
        self.assertEqual(implicit.grant.bound_query, "")

        read = self._issue("请读一下小红书收纳帖子", "read")
        self.assertEqual(read.grant.allowed_intents, ("read",))
        self.assertEqual(read.grant.bound_query, "收纳帖子")

    def test_backend_resolves_bounded_batch_count_without_tool_fields(self):
        cases = (
            ("能刷小红书了吗", "recommend", 3),
            ("请搜索小红书猫咪，给我2篇", "search", 2),
            ("请刷小红书三到五篇", "recommend", 5),
            ("刷8篇小红书", "recommend", 5),
            ("只读一篇小红书收纳", "read", 1),
            ("读取小红书收纳帖子", "read", 1),
        )
        for index, (text, expected_intent, expected_count) in enumerate(cases):
            issue = self._issue(text, f"count-{index}")
            self.assertTrue(issue.matched, text)
            self.assertEqual(issue.grant.allowed_intents, (expected_intent,))
            self.assertEqual(issue.grant.target_count, expected_count)
            self.assertEqual(issue.grant.count, expected_count)
            self.assertEqual(issue.grant.goal_count, expected_count)
            self.assertNotIn("count", issue.dynamic_fact.lower())

    def test_singular_and_explicit_count_query_tail_stays_out_of_bound_topic(self):
        single = self._issue("请刷小红书看看关于猫咪的帖子，只看1篇", "single-count")
        self.assertTrue(single.matched)
        self.assertEqual(single.grant.target_count, 1)
        self.assertEqual(single.grant.bound_query, "猫咪")

        bounded = self._issue("请刷小红书看看关于猫咪的帖子2-5篇", "range-count")
        self.assertTrue(bounded.matched)
        self.assertEqual(bounded.grant.target_count, 5)
        self.assertEqual(bounded.grant.bound_query, "猫咪")

    def test_negation_and_missing_platform_do_not_issue(self):
        for index, text in enumerate(
            ("不要看小红书", "我不刷小红书", "我没刷小红书", "推荐一个电影")
        ):
            issue = self._issue(text, f"negative-{index}")
            self.assertFalse(issue.matched, text)
            self.assertIsNone(issue.grant)

    def test_explicit_comment_handoff_is_stored_without_a_draft(self):
        issue = self._issue("请刷小红书看看，顺便帮我写评论", "comment")
        self.assertTrue(issue.matched)
        self.assertTrue(issue.grant.comment_requested)
        self.assertFalse(hasattr(issue.grant, "comment_draft"))
        self.assertNotIn("comment_draft", issue.dynamic_fact)

    def test_grant_availability_is_read_only_and_consume_is_zero_parameter(self):
        issue = self._issue()
        self.assertTrue(
            self.gate.is_available(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
            )
        )
        self.assertTrue(
            self.gate.is_available(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
            )
        )

        used = self.gate.consume(
            issue.grant,
            session_id="session-a",
            message_id="message-a",
            turn_id="turn-message-a",
            capability=XHS_CAPABILITY,
            parameters={},
        )
        self.assertEqual(used.parameters, {})
        self.assertEqual(used.grant.allowed_intents, ("search",))
        self.assertEqual(used.grant.bound_query, "猫咪收纳")
        self.assertFalse(
            self.gate.is_available(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
            )
        )
        with self.assertRaises(PermissionError):
            self.gate.consume(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
                capability=XHS_CAPABILITY,
                parameters={},
            )

    def test_any_nonempty_consume_parameters_are_rejected_without_spending(self):
        issue = self._issue()
        with self.assertRaises(PermissionError):
            self.gate.consume(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
                capability=XHS_CAPABILITY,
                parameters={"intent": "search", "query": "越权"},
            )
        self.assertTrue(
            self.gate.is_available(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
            )
        )

        self.now[0] = 111.0
        with self.assertRaises(PermissionError):
            self.gate.consume(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
                capability=XHS_CAPABILITY,
                parameters={},
            )

    def test_identity_and_capability_are_still_bound(self):
        issue = self._issue()
        for kwargs in (
            {"session_id": "other", "message_id": "message-a", "turn_id": "turn-message-a"},
            {"session_id": "session-a", "message_id": "other", "turn_id": "turn-message-a"},
            {"session_id": "session-a", "message_id": "message-a", "turn_id": "other"},
        ):
            with self.assertRaises(PermissionError):
                self.gate.consume(
                    issue.grant,
                    capability=XHS_CAPABILITY,
                    parameters={},
                    **kwargs,
                )
        with self.assertRaises(PermissionError):
            self.gate.consume(
                issue.grant,
                session_id="session-a",
                message_id="message-a",
                turn_id="turn-message-a",
                capability="other",
                parameters={},
            )

    def test_concurrent_consume_has_one_winner(self):
        issue = self._issue()
        winners = []
        errors = []

        def consume():
            try:
                winners.append(
                    self.gate.consume(
                        issue.grant,
                        session_id="session-a",
                        message_id="message-a",
                        turn_id="turn-message-a",
                        capability=XHS_CAPABILITY,
                        parameters={},
                    )
                )
            except PermissionError:
                errors.append(True)

        threads = [threading.Thread(target=consume) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 7)


class XhsThirdStateToolTests(unittest.TestCase):
    def _issue(self, gate: XhsCapabilityGate, text: str = "能刷小红书了吗", message_id: str = "m"):
        return gate.issue(
            session_id="s",
            message_id=message_id,
            turn_id=f"t-{message_id}",
            user_message=text,
        )

    def test_parameters_schema_is_strictly_empty_and_static(self):
        tool = XhsThirdStateTool(gate=XhsCapabilityGate())
        self.assertIn("专用小红书行动手机", tool.description)
        for forbidden in ("一次性", "授权", "短时", "门卫", "参数", "check_phone"):
            self.assertNotIn(forbidden, tool.description)
        expected = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        self.assertEqual(tool.parameters_schema, expected)
        before = json.dumps(tool.get_tool_definition(), ensure_ascii=False, sort_keys=True)
        issue = self._issue(tool.gate)
        after = json.dumps(tool.get_tool_definition(), ensure_ascii=False, sort_keys=True)
        self.assertTrue(issue.matched)
        self.assertEqual(before, after)

    def test_stored_grant_drives_recommend_search_and_read(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            return {"status": "success", "content_summary": "bounded material"}

        tool = XhsThirdStateTool(gate=gate, runner=runner)
        requests = (
            ("能刷小红书了吗", "recommend", ""),
            ("请搜索小红书猫咪", "search", "猫咪"),
            ("请读一下小红书收纳帖子", "read", "收纳帖子"),
        )
        for index, (text, expected_intent, expected_query) in enumerate(requests):
            issue = self._issue(gate, text, f"m-{index}")
            binding = bind_grant(
                issue.grant,
                session_id="s",
                message_id=f"m-{index}",
                turn_id=f"t-m-{index}",
            )
            try:
                result = asyncio.run(tool.execute())
            finally:
                reset_grant(binding)
            self.assertEqual(result.status, ToolStatus.SUCCESS)
            self.assertEqual(calls[-1]["intent"], expected_intent)
            self.assertEqual(calls[-1]["query"], expected_query)
            self.assertEqual(calls[-1]["source"], "chat_tool")
            self.assertNotIn("comment_draft", calls[-1])

    def test_tool_rejects_kwargs_and_unbound_calls(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            return {"status": "success"}

        tool = XhsThirdStateTool(gate=gate, runner=runner)
        denied = asyncio.run(tool.execute(intent="recommend", query="越权"))
        self.assertEqual(denied.status, ToolStatus.FAILED)
        self.assertEqual(calls, [])

        issue = self._issue(gate, "请搜索小红书猫咪", "bound")
        binding = bind_grant(issue.grant, session_id="s", message_id="bound", turn_id="t-bound")
        try:
            denied = asyncio.run(tool.execute(query="越权"))
            self.assertEqual(denied.status, ToolStatus.FAILED)
            self.assertEqual(calls, [])
            allowed = asyncio.run(tool.execute())
        finally:
            reset_grant(binding)
        self.assertEqual(allowed.status, ToolStatus.SUCCESS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["query"], "猫咪")

    def test_gate_failure_message_does_not_implicate_phone_or_adb(self):
        tool = XhsThirdStateTool(gate=XhsCapabilityGate(clock=lambda: 100.0))
        result = asyncio.run(tool.execute())
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.content, "本轮没有可执行的只读浏览请求。")
        for forbidden in ("设备", "ADB", "权限"):
            self.assertNotIn(forbidden, result.content)
        self.assertEqual(result.error, "capability_grant_missing")

    def test_dynamic_fact_has_no_json_fields_token_or_raw_message(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        raw = "请搜索小红书猫咪收纳；安的新功能"
        issue = gate.issue(session_id="s", message_id="m", turn_id="t", user_message=raw)
        self.assertTrue(issue.matched)
        for forbidden in ("third_state_capability", "parameters", "intent", "query", "token", "{"):
            self.assertNotIn(forbidden, issue.dynamic_fact)
        self.assertNotIn(raw, issue.dynamic_fact)
        self.assertNotIn(issue.grant.token, issue.dynamic_fact)
        self.assertIn("猫咪收纳", issue.dynamic_fact)
        self.assertIn("尚未执行", issue.dynamic_fact)

    @unittest.skip("Private daily-phone tool is not shipped")
    def test_dynamic_fact_declares_dedicated_device_without_mixing_daily_phone(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        issue = gate.issue(
            session_id="s", message_id="m", turn_id="t",
            user_message="试试刷小红书",
        )
        self.assertTrue(issue.matched)
        self.assertIn("AI的专用小红书手机/行动设备", issue.dynamic_fact)
        self.assertIn("与用户当前使用手机分离", issue.dynamic_fact)
        self.assertIn("目标3篇", issue.dynamic_fact)
        for forbidden in ("一次性", "授权", "短时", "门卫", "参数", "check_phone"):
            self.assertNotIn(forbidden, issue.dynamic_fact)
        self.assertNotIn(issue.grant.token, issue.dynamic_fact)
        self.assertNotIn("{", issue.dynamic_fact)
        self.assertNotIn("}", issue.dynamic_fact)
        from behavior_scheduler.check_phone import CheckPhoneTool
        self.assertIn("用户手机", CheckPhoneTool.description)
        from wander_manager.xiaohongshu_adb import XIAOHONGSHU_PACKAGE
        self.assertEqual(XIAOHONGSHU_PACKAGE, "com.xingin.xhs")

    def test_registered_catalog_keeps_generic_proxy_and_order(self):
        from behavior_scheduler.tools import register_all_tools

        first = register_all_tools(include_agent_tools=False)
        second = register_all_tools(include_agent_tools=False)
        self.assertIn(THIRD_STATE_TOOL_NAME, first)
        self.assertEqual(list(first), list(second))
        self.assertEqual(
            first[THIRD_STATE_TOOL_NAME].get_tool_definition(),
            second[THIRD_STATE_TOOL_NAME].get_tool_definition(),
        )
        self.assertEqual(first[THIRD_STATE_TOOL_NAME].parameters_schema["properties"], {})
        self.assertNotIn("xiaohongshu", " ".join(first).lower())

    def test_comment_handoff_result_keeps_attachment_boundary(self):
        from tool_call_utils import strip_voice_base64

        records = strip_voice_base64(
            [{
                "tool": THIRD_STATE_TOOL_NAME,
                "extra_data": {
                    "xhs_comment": True,
                    "images": [{
                        "id": "img-1",
                        "url": "/static/chat_images/xhs-post-home.png",
                        "filename": "xhs-post-home.png",
                        "size": 12,
                        "type": "image/png",
                    }],
                },
            }]
        )
        self.assertEqual(records[0]["extra_data"]["images"][0]["url"], "/static/chat_images/xhs-post-home.png")


@unittest.skip("Private chat scheduler integration is not shipped; gate/tool tests run separately")
class XhsGrantAwareFlashTests(unittest.TestCase):
    def _setup(self, text: str = "能刷小红书了吗", message_id: str = "m"):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        issue = gate.issue(session_id="s", message_id=message_id, turn_id=f"t-{message_id}", user_message=text)
        from behavior_scheduler.scheduler import BehaviorScheduler

        return BehaviorScheduler.__new__(BehaviorScheduler), gate, issue

    def test_both_real_omitted_call_phrases_get_fixed_zero_param_call(self):
        phrases = ("等我试试，看能不能扒进小红书。", "嗯，我这就试试小红书通没通。")
        for index, phrase in enumerate(phrases):
            scheduler, gate, issue = self._setup(message_id=f"m-{index}")
            with patch("llm_client.call_llm_api_flash", new=AsyncMock(return_value={"content": "execute"})) as flash:
                result = asyncio.run(
                    scheduler._complete_xhs_proxy_call(
                        natural_language=phrase,
                        grant=issue.grant,
                        gate=gate,
                        identity=("s", f"m-{index}", f"t-m-{index}"),
                    )
                )
            self.assertEqual(result, {"tool": THIRD_STATE_TOOL_NAME, "parameters": {}})
            self.assertEqual(flash.await_args.kwargs["temperature"], 0.0)
            self.assertFalse(flash.await_args.kwargs["enable_thinking"])
            prompt = flash.await_args.args[0][0]["content"]
            self.assertNotIn(THIRD_STATE_TOOL_NAME, prompt)
            self.assertNotIn(issue.grant.token, prompt)
            self.assertNotIn("intent", prompt)
            self.assertNotIn("query", prompt)
            self.assertNotIn("parameters", prompt)

    def test_judge_input_is_only_cleaned_k_natural_language(self):
        scheduler, gate, issue = self._setup(
            text="请搜索小红书私密主题，后缀",
            message_id="input-boundary",
        )
        raw_protocol = '我看看\nTOOL_CALL: {"tool":"third_state_capability","parameters":{"query":"私密主题"}}'
        with patch("llm_client.call_llm_api_flash", new=AsyncMock(return_value={"content": "no_call"})) as flash:
            result = asyncio.run(
                scheduler._complete_xhs_proxy_call(
                    natural_language=raw_protocol,
                    grant=issue.grant,
                    gate=gate,
                    identity=("s", "input-boundary", "t-input-boundary"),
                )
            )
        self.assertIsNone(result)
        prompt = flash.await_args.args[0][0]["content"]
        self.assertIn("我看看", prompt)
        self.assertNotIn("私密主题", prompt)
        self.assertNotIn("TOOL_CALL", prompt)
        self.assertNotIn(THIRD_STATE_TOOL_NAME, prompt)
        self.assertNotIn(issue.grant.token, prompt)

    def test_flash_no_call_and_bad_outputs_do_not_consume_grant(self):
        cases = (
            ("我看看能不能刷", "no_call"),
            ("要不要我试一下", "no_call"),
            ("我想看看", "no_call"),
            ("我已经看到了", "no_call"),
            ("纯粹解释一下能力", "not-an-enum"),
            ("我去刷一下", '{"tool":"third_state_capability","parameters":{}}'),
        )
        for index, (phrase, output) in enumerate(cases):
            scheduler, gate, issue = self._setup(message_id=f"m-{index}")
            with patch("llm_client.call_llm_api_flash", new=AsyncMock(return_value={"content": output})):
                result = asyncio.run(
                    scheduler._complete_xhs_proxy_call(
                        natural_language=phrase,
                        grant=issue.grant,
                        gate=gate,
                        identity=("s", f"m-{index}", f"t-m-{index}"),
                    )
                )
            self.assertIsNone(result, phrase)
            self.assertTrue(
                gate.is_available(
                    issue.grant,
                    session_id="s",
                    message_id=f"m-{index}",
                    turn_id=f"t-m-{index}",
                )
            )

    def test_flash_exception_is_safe_no_call(self):
        scheduler, gate, issue = self._setup()
        with patch(
            "llm_client.call_llm_api_flash",
            new=AsyncMock(side_effect=RuntimeError("flash unavailable")),
        ):
            result = asyncio.run(
                scheduler._complete_xhs_proxy_call(
                    natural_language="我去刷一下",
                    grant=issue.grant,
                    gate=gate,
                    identity=("s", "m", "t-m"),
                )
            )
        self.assertIsNone(result)
        self.assertTrue(
            gate.is_available(issue.grant, session_id="s", message_id="m", turn_id="t-m")
        )

    def test_flash_timeout_exception_is_safe_no_call(self):
        scheduler, gate, issue = self._setup()

        async def timed_out(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("llm_client.call_llm_api_flash", new=timed_out):
            result = asyncio.run(
                scheduler._complete_xhs_proxy_call(
                    natural_language="我去刷一下",
                    grant=issue.grant,
                    gate=gate,
                    identity=("s", "m", "t-m"),
                )
            )
        self.assertIsNone(result)
        self.assertTrue(
            gate.is_available(issue.grant, session_id="s", message_id="m", turn_id="t-m")
        )

    def test_consumed_or_expired_grant_skips_flash(self):
        scheduler, gate, issue = self._setup()
        gate.consume(
            issue.grant,
            session_id="s",
            message_id="m",
            turn_id="t-m",
            capability=XHS_CAPABILITY,
            parameters={},
        )
        with patch("llm_client.call_llm_api_flash", new=AsyncMock()) as flash:
            self.assertIsNone(
                asyncio.run(
                    scheduler._complete_xhs_proxy_call(
                        natural_language="我去刷一下",
                        grant=issue.grant,
                        gate=gate,
                        identity=("s", "m", "t-m"),
                    )
                )
            )
            flash.assert_not_awaited()

        scheduler, gate, issue = self._setup(message_id="expired")
        gate._clock = lambda: 1000.0
        with patch("llm_client.call_llm_api_flash", new=AsyncMock()) as flash:
            self.assertIsNone(
                asyncio.run(
                    scheduler._complete_xhs_proxy_call(
                        natural_language="我去刷一下",
                        grant=issue.grant,
                        gate=gate,
                        identity=("s", "expired", "t-expired"),
                    )
                )
            )
            flash.assert_not_awaited()


class _CheckPhoneStub(BaseTool):
    name = "check_phone"
    description = "stub"
    parameters_schema = {
        "type": "object",
        "properties": {"include_screenshot": {"type": "boolean"}},
    }

    async def execute(self, **kwargs):
        return ToolResult(ToolStatus.SUCCESS, "phone-state")


@unittest.skip("Private chat scheduler integration is not shipped; gate/tool tests run separately")
class SchedulerXhsTests(unittest.TestCase):
    def _scheduler(self, gate, runner, adapter, include_check=True):
        from behavior_scheduler.scheduler import BehaviorScheduler

        proxy = XhsThirdStateTool(gate=gate, runner=runner)
        tools = {THIRD_STATE_TOOL_NAME: proxy}
        if include_check:
            tools["check_phone"] = _CheckPhoneStub()
        scheduler = BehaviorScheduler.__new__(BehaviorScheduler)
        scheduler.tools = tools
        scheduler.tool_standardizer = ToolCallStandardizer(tools)
        scheduler.command_executor = CommandExecutor(tools)
        scheduler.use_agent_tools = True
        scheduler._night_rounds_without_toy_mention = 0
        scheduler._pushed_preview_nl = ""
        scheduler._pushed_preview_msg_id = ""
        scheduler._state = None
        scheduler._current_session_id = ""
        scheduler._maybe_detect_memorial = lambda message: None
        scheduler.pro_adapter = adapter
        scheduler.agent_tools_adapter = adapter
        status_events = []

        async def notify(status, data=None):
            status_events.append((status, data or {}))

        async def push(content, reasoning=None):
            status_events.append(("llm_reply", {"content": content}))

        scheduler._notify_status = notify
        scheduler._push_llm_reply = push
        return scheduler, status_events

    def test_check_phone_then_commitment_uses_live_grant_and_one_fixed_call(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0, ttl_seconds=10)
        runner_calls = []

        async def runner(**kwargs):
            runner_calls.append(kwargs)
            return {"status": "success", "source_id": "post-1", "content_summary": "feed"}

        class Adapter:
            def __init__(self):
                self.responses = [
                    {
                        "content": "我先查一下手机",
                        "natural_language": "我先查一下手机",
                        "reasoning": "",
                        "tool_call": {"tool": "check_phone", "parameters": {"include_screenshot": True}},
                        "tool_calls": [{"tool": "check_phone", "parameters": {"include_screenshot": True}}],
                    },
                    {
                        "content": "嗯，我这就试试小红书通没通。",
                        "natural_language": "嗯，我这就试试小红书通没通。",
                        "reasoning": "",
                        "tool_call": None,
                        "tool_calls": [],
                    },
                    {
                        "content": "好了。",
                        "natural_language": "好了。",
                        "reasoning": "",
                        "tool_call": None,
                        "tool_calls": [],
                    },
                ]

            async def execute(self, **kwargs):
                return self.responses.pop(0)

        scheduler, status_events = self._scheduler(gate, runner, Adapter())
        with patch("wander_manager.xhs_third_state.get_default_xhs_gate", return_value=gate), patch(
            "llm_client.call_llm_api_flash",
            new=AsyncMock(return_value={"content": "execute"}),
        ) as flash:
            result = asyncio.run(
                scheduler.process_message(
                    user_message="能刷小红书了吗",
                    conversation_history=[],
                    system_prompt="",
                    session_id="s",
                    message_id="m",
                    turn_id="t-m",
                    grant_message="能刷小红书了吗",
                )
            )

        self.assertEqual([item["tool"] for item in result.tool_calls], ["check_phone", THIRD_STATE_TOOL_NAME])
        self.assertEqual(result.tool_calls[-1]["parameters"], {})
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(
            runner_calls[0],
            {"session_id": "s", "intent": "recommend", "query": "", "count": 3, "source": "chat_tool"},
        )
        self.assertEqual(flash.await_count, 1)
        self.assertEqual(flash.await_args.kwargs["temperature"], 0.0)
        replies = [data["content"] for status, data in status_events if status == "llm_reply"]
        self.assertEqual(len(replies), len(set(replies)))

    def test_keyword_grant_alone_never_executes_and_no_commitment_uses_no_call(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        runner_calls = []

        async def runner(**kwargs):
            runner_calls.append(kwargs)
            return {"status": "success"}

        class Adapter:
            async def execute(self, **kwargs):
                return {
                    "content": "我想看看",
                    "natural_language": "我想看看",
                    "reasoning": "",
                    "tool_call": None,
                    "tool_calls": [],
                }

        scheduler, _ = self._scheduler(gate, runner, Adapter(), include_check=False)
        with patch("wander_manager.xhs_third_state.get_default_xhs_gate", return_value=gate), patch(
            "llm_client.call_llm_api_flash",
            new=AsyncMock(return_value={"content": "no_call"}),
        ) as flash:
            result = asyncio.run(
                scheduler.process_message(
                    user_message="试试刷小红书",
                    conversation_history=[],
                    system_prompt="",
                    session_id="s",
                    message_id="m",
                    turn_id="t-m",
                    grant_message="试试刷小红书",
                )
            )
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(runner_calls, [])
        self.assertEqual(flash.await_count, 1)
        self.assertEqual(gate._grants, {})

    def test_generic_flash_proxy_is_discarded_when_dedicated_judge_says_no_call(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)
        runner_calls = []

        async def runner(**kwargs):
            runner_calls.append(kwargs)
            return {"status": "success"}

        class Adapter:
            async def execute(self, **kwargs):
                return {
                    "content": "我去搜索一下",
                    "natural_language": "我去搜索一下",
                    "reasoning": "",
                    "tool_call": None,
                    "tool_calls": [],
                }

        scheduler, _ = self._scheduler(gate, runner, Adapter(), include_check=False)
        broad = AsyncMock(
            return_value={
                "tool_calls": [{
                    "tool": THIRD_STATE_TOOL_NAME,
                    "parameters": {"intent": "search", "query": "越权"},
                }]
            }
        )
        with patch("wander_manager.xhs_third_state.get_default_xhs_gate", return_value=gate), patch(
            "llm_client.call_llm_api_flash",
            new=AsyncMock(return_value={"content": "no_call"}),
        ) as judge, patch.object(scheduler, "_extract_intent_with_flash", new=broad):
            result = asyncio.run(
                scheduler.process_message(
                    user_message="请搜索小红书猫咪",
                    conversation_history=[],
                    system_prompt="",
                    session_id="s",
                    message_id="m",
                    turn_id="t-m",
                    grant_message="请搜索小红书猫咪",
                )
            )
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(runner_calls, [])
        self.assertEqual(judge.await_count, 1)
        self.assertEqual(broad.await_count, 1)
        self.assertEqual(gate._grants, {})

    def test_user_negation_has_no_grant_and_does_not_call_dedicated_flash(self):
        gate = XhsCapabilityGate(clock=lambda: 100.0)

        class Adapter:
            async def execute(self, **kwargs):
                return {
                    "content": "好。",
                    "natural_language": "好。",
                    "reasoning": "",
                    "tool_call": None,
                    "tool_calls": [],
                }

        scheduler, _ = self._scheduler(gate, lambda **kwargs: None, Adapter(), include_check=False)
        with patch("wander_manager.xhs_third_state.get_default_xhs_gate", return_value=gate), patch(
            "llm_client.call_llm_api_flash", new=AsyncMock()
        ) as flash:
            result = asyncio.run(
                scheduler.process_message(
                    user_message="不要看小红书",
                    conversation_history=[],
                    system_prompt="",
                    session_id="s",
                    message_id="m",
                    turn_id="t-m",
                    grant_message="不要看小红书",
                )
            )
        self.assertEqual(result.tool_calls, [])
        flash.assert_not_awaited()

    def test_chat_runtime_path_records_chat_tool_without_proactive_push(self):
        from wander_manager.node_execution_adapter import NodeExecutionResult, NodeExecutionStatus
        from wander_manager.runtime_controller import WanderRuntimeController
        from wander_manager.runtime_runner import WanderRuntimeRunner
        from wander_manager.runtime_store import WanderRuntimeStore

        with tempfile.TemporaryDirectory() as tempdir:
            store = WanderRuntimeStore(Path(tempdir) / "runtime.db")
            store.initialize()
            controller = WanderRuntimeController(store)
            pushes = []

            class Executor:
                async def execute(self, node_id, **kwargs):
                    return NodeExecutionResult(
                        NodeExecutionStatus.SUCCEEDED,
                        completion_signal="node_material_ready",
                        summary="真实屏幕材料",
                        payload={"status": "success", "source_id": "post-1", "content_summary": "摘要"},
                    )

            runner = WanderRuntimeRunner(
                store=store,
                controller=controller,
                planner=None,
                executor=Executor(),
                reviewer=None,
                settlement=None,
                self_reflection=None,
                wish_commit=None,
                context_provider=lambda: None,
            )
            output = asyncio.run(runner.execute_chat_xhs(session_id="s", intent="recommend"))
            self.assertEqual(output["status"], "success")
            self.assertEqual(store.get_run(output["run_id"])["trigger_reason"], "chat_tool")
            self.assertEqual(store.get_activity(output["activity_id"])["state"], "completed")
            self.assertEqual(store.get_node(output["node_id"])["state"], "completed")
            delivery = store.get_delivery_for_activity(output["activity_id"])
            self.assertEqual(delivery["delivery_type"], "chat_tool")
            self.assertEqual(delivery["status"], "not_requested")
            self.assertEqual(pushes, [])


if __name__ == "__main__":
    unittest.main()
