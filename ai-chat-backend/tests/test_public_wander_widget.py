"""The launcher reads a narrow view of the primary chat and the active Wander run."""
import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from chat_turn_guard import ChatTurnGuard
from routers import affect_router, wander_router
from wander_manager.manager import WanderManager


class WidgetProjectionTests(TestCase):
    def test_activity_view_has_no_source_text_or_session_identity(self):
        manager = WanderManager.__new__(WanderManager)
        manager._mode_switch = Mock()
        manager._mode_switch.get_status.return_value = {"mode": "idle", "mode_display": "空闲"}
        manager.get_runtime_status = Mock(return_value={
            "runtime_state": "active", "current_event_type": "browse_bookmarks",
            "shelf_sources": ["essays"], "session_id": "sample", "source_payload": {"text": "example"}})
        guard = ChatTurnGuard()
        sessions = SimpleNamespace(get_primary_session_id=AsyncMock(return_value="sample"))
        with patch('shared_state.get_wander_manager', return_value=manager), \
             patch('chat_turn_guard.get_chat_turn_guard', return_value=guard), \
             patch('session_manager.get_global_session_manager', return_value=sessions):
            other = guard.claim(session_id="other", user_message_id="1", content="")
            response = asyncio.run(wander_router.get_wander_presence())
            self.assertFalse(response["primary_reply_running"])
            self.assertEqual(response["wander"]["shelf_sources"], ["essays"])
            self.assertNotIn("source_payload", response["wander"])
            self.assertNotIn("session_id", response["wander"])
            token = guard.claim(session_id="sample", user_message_id="2", content="")
            self.assertTrue(asyncio.run(wander_router.get_wander_presence())["primary_reply_running"])
            guard.complete(token)
            guard.complete(other)
            self.assertFalse(asyncio.run(wander_router.get_wander_presence())["primary_reply_running"])
            sessions.get_primary_session_id.return_value = ""
            self.assertIsNone(asyncio.run(wander_router.get_wander_presence())["primary_reply_running"])

    def test_widget_mood_uses_only_the_family(self):
        source = {"self": {"dominant": {"family": "安定", "source": "example"}}, "session": "sample"}
        with patch.object(affect_router, "_service"), \
             patch.object(affect_router, "_current_projection", AsyncMock(return_value=source)):
            self.assertEqual(asyncio.run(affect_router.affect_projection(widget=True)), {"family": "安定"})
            self.assertEqual(asyncio.run(affect_router.affect_projection()), source)
