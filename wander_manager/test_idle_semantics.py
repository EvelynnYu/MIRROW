"""Standalone host timestamps never masquerade as device idle."""
import unittest
from datetime import datetime, timedelta, timezone
from mirrow_core import shared_state
from wander_manager.wander_creator import _private_message_idle_text
from wander_manager.plan_decision_adapter import RuntimeContext, build_runtime_block


class IdleSemanticTests(unittest.TestCase):
    def setUp(self):
        self.old = shared_state.get_last_private_chat_time()

    def tearDown(self):
        shared_state.set_last_private_chat_time(self.old)

    def test_unconfigured_time_is_unknown(self):
        shared_state.set_last_private_chat_time(None)
        self.assertEqual('未知', _private_message_idle_text())

    def test_private_and_physical_clocks_are_independent(self):
        shared_state.set_last_private_chat_time(datetime.now()-timedelta(minutes=4))
        self.assertEqual('4分钟', _private_message_idle_text())
        block=build_runtime_block(RuntimeContext(persona='AI', private_message_idle='4分钟', physical_idle='7小时'))
        self.assertIn('[距最后私聊]\n4分钟', block)
        self.assertIn('[设备本地物理空闲]\n7小时', block)

    def test_aware_host_timestamp_is_normalized(self):
        shared_state.set_last_private_chat_time(datetime.now(timezone.utc))
        self.assertIsNone(shared_state.get_last_private_chat_time().tzinfo)
        self.assertLess(abs((datetime.now()-shared_state.get_last_private_chat_time()).total_seconds()), 2)
