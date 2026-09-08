"""Deterministic XHS handoff and attachment ownership contracts."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from wander_manager.chat_attachment_boundary import delete_owned_chat_attachments
from wander_manager.xhs_comment_delivery import prepare_comment_delivery


_ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class XhsCommentDeliveryTests(unittest.TestCase):
    def test_draft_is_saved_through_attachment_contract_without_base64_metadata(self):
        calls = []

        def saver(raw, filename, mime_type):
            calls.append((raw, filename, mime_type))
            return {
                "id": "owned-id",
                "url": "/static/chat_images/owned-file.png",
                "filename": filename,
                "size": len(raw),
                "type": mime_type,
            }

        result = asyncio.run(prepare_comment_delivery(
            post_id="post-1",
            comment_draft="这条收纳思路很实用！",
            home_screenshot_base64=_ONE_PIXEL_PNG,
            attachment_saver=saver,
        ))
        self.assertEqual("ready_for_user", result["status"])
        self.assertEqual("这条收纳思路很实用！", result["comment_draft"])
        self.assertEqual(1, len(calls))
        self.assertIsInstance(calls[0][0], bytes)
        self.assertNotIn("data", result["image"])
        self.assertNotIn("base64", result["image"])

    def test_without_draft_no_attachment_is_saved(self):
        called = []
        result = asyncio.run(prepare_comment_delivery(
            post_id="post-1",
            comment_draft="",
            home_screenshot_base64=_ONE_PIXEL_PNG,
            attachment_saver=lambda *args: called.append(args),
        ))
        self.assertEqual("not_requested", result["status"])
        self.assertEqual([], called)

    def test_message_attachment_cleanup_has_directory_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "owned-file.png"
            foreign = root.parent / "foreign-file.png"
            owned.write_bytes(b"owned")
            foreign.write_bytes(b"foreign")
            try:
                deleted = delete_owned_chat_attachments([
                    {"url": "/static/chat_images/owned-file.png"},
                    {"url": "/static/chat_images/../foreign-file.png"},
                    {"url": "C:/not-owned.txt"},
                ], root)
                self.assertEqual(1, deleted)
                self.assertFalse(owned.exists())
                self.assertTrue(foreign.exists())
            finally:
                foreign.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
