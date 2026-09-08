"""Regression tests for the shared Flash structured-output parser."""

from __future__ import annotations

import json
import asyncio
import unittest
from unittest.mock import patch

from wander_manager.flash_structured import call_flash_json_detailed, parse_json_object


class FlashStructuredParserTests(unittest.TestCase):
    def test_accepts_object_and_fenced_object(self):
        self.assertEqual({"has_topic": True}, parse_json_object('{"has_topic": true}'))
        self.assertEqual(
            {"has_topic": False},
            parse_json_object("```json\n{\"has_topic\": false}\n```"),
        )

    def test_unwraps_json_string_containing_object(self):
        payload = {"has_topic": True, "topic": "连续性"}
        encoded = json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)
        self.assertEqual(payload, parse_json_object(encoded))
        self.assertEqual(payload, parse_json_object(f"```\n{encoded}\n```"))

    def test_rejects_non_object_and_invalid_json(self):
        self.assertIsNone(parse_json_object('[{"has_topic": true}]'))
        self.assertIsNone(parse_json_object('"just text"'))
        self.assertIsNone(parse_json_object('{"has_topic":'))
        self.assertIsNone(parse_json_object(""))

    def test_bad_json_gets_one_structured_retry_and_can_recover(self):
        class Response:
            status_code = 200
            def __init__(self, content): self.content = content
            def raise_for_status(self): return None
            def json(self): return {"choices": [{"message": {"content": self.content}}]}

        class Client:
            def __init__(self): self.calls = 0
            async def post(self, *_args, **_kwargs):
                self.calls += 1
                return Response("not-json" if self.calls == 1 else '{"ok":true}')

        client = Client()
        from mirrow_core import llm_runtime as llm_client
        with patch.object(llm_client, "http_client", client), \
             patch.object(llm_client, "DEEPSEEK_FLASH_API_KEY", "test-key"), \
             patch.object(llm_client, "DEEPSEEK_FLASH_API_URL", "https://example.invalid/v1"), \
             patch.object(llm_client, "DEEPSEEK_FLASH_MODEL", "fake-flash"):
            result = asyncio.run(call_flash_json_detailed([{"role": "user", "content": "x"}]))
        self.assertEqual(2, client.calls)
        self.assertEqual("ok", result.status)
        self.assertEqual({"ok": True}, result.parsed)
        self.assertNotIn("test-key", repr(result))

    def test_two_bad_json_responses_are_terminal_after_two_requests(self):
        class Response:
            status_code = 200
            def raise_for_status(self): return None
            def json(self): return {"choices": [{"message": {"content": "still-bad"}}]}

        class Client:
            def __init__(self): self.calls = 0
            async def post(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        client = Client()
        from mirrow_core import llm_runtime as llm_client
        with patch.object(llm_client, "http_client", client), \
             patch.object(llm_client, "DEEPSEEK_FLASH_API_KEY", "test-key"), \
             patch.object(llm_client, "DEEPSEEK_FLASH_API_URL", "https://example.invalid/v1"), \
             patch.object(llm_client, "DEEPSEEK_FLASH_MODEL", "fake-flash"):
            result = asyncio.run(call_flash_json_detailed([{"role": "user", "content": "x"}]))
        self.assertEqual(2, client.calls)
        self.assertEqual("json_parse_error", result.status)
        self.assertIsNone(result.parsed)


if __name__ == "__main__":
    unittest.main()
