import unittest
from wander_manager.checkup_receipts import (
    is_checkup_request_text,
    tracking_items, tracking_conclusion, scheduled_conclusion,
    sensory_tool_items, visible_chat_tool_calls,
)

class CheckupReceiptTests(unittest.TestCase):
    def test_spoken_immediate_checkup_variants(self):
        for text in ('查岗', '查个岗来。', '查一下岗', '看看我睡了没'):
            self.assertTrue(is_checkup_request_text(text), text)
        self.assertFalse(is_checkup_request_text('一分钟后提醒我'))

    def test_tracking_sources_and_error_is_uncertain(self):
        d={'screen_attempted':True,'screen_available':False,'error':'capture failed'}
        self.assertEqual(tracking_items(d, 'x')[0]['status'], 'failure')
        self.assertEqual(tracking_conclusion(d, True), 'uncertain')
    def test_away_is_passive_not_fake_screen(self):
        items=tracking_items({'route':'away'}, '')
        self.assertEqual(items[0]['tool'], 'behavior_state')
    def test_empty_scheduled_receipts_uncertain(self):
        self.assertEqual(scheduled_conclusion([]), 'uncertain')
    def test_eyes_sources_are_visible_individually(self):
        items = sensory_tool_items([{
            'tool': 'eyes', 'description': '看了一眼', 'result': '综合结果',
            'extra_data': {
                'sources': [{'source': '手机后摄', 'analysis': {'summary': '天花板'}}],
                'errors': ['手机前摄: camera busy'],
            },
        }])
        self.assertEqual(['手机后摄', '手机前摄'], [item['tool'] for item in items])
        self.assertEqual(['success', 'failure'], [item['status'] for item in items])

    def test_immediate_checkup_sensory_tools_live_only_on_receipt(self):
        calls = [{'tool': 'eyes'}, {'tool': 'check_phone'}, {'tool': 'send_voice'}]
        self.assertEqual(
            ['send_voice'],
            [item['tool'] for item in visible_chat_tool_calls(calls, hide_sensory=True)],
        )

if __name__ == '__main__': unittest.main()
