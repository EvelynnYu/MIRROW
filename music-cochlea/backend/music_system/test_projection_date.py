import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from .projection_date import event_active_date, TZ


class ProjectionDateTests(unittest.TestCase):
    def test_cross_midnight_and_historical_queue_use_occurrence(self):
        chronicle = Mock()
        chronicle.get_message_by_msg_id.return_value = None
        chronicle.get_topic_state.return_value = {'is_active': True, 'start_time': '2026-09-25T22:00:00+08:00'}
        payload = {'event_id': 'music_library_event_1', 'occurred_at': '2026-09-26T00:10:00+08:00'}
        with patch('scene_manager.scene.get_scene_for_ts', return_value=None):
            self.assertEqual(('2026-09-25', 'active_topic_at_occurrence'), event_active_date(
                payload, 'main', chronicle, now=datetime(2026, 9, 26, 1, tzinfo=TZ)))
            payload['occurred_at'] = '2026-09-23T00:10:00+08:00'
            self.assertEqual(('2026-09-23', 'occurrence_calendar_fallback'), event_active_date(payload, 'main', chronicle))
        with patch('scene_manager.scene.get_scene_for_ts', return_value={'active_date': '2026-09-22'}):
            self.assertEqual(('2026-09-22', 'persisted_scene'), event_active_date(payload, 'main', chronicle))

    def test_existing_event_keeps_its_day_on_retry(self):
        chronicle = Mock()
        chronicle.get_message_by_msg_id.return_value = {'session_id': 'main', 'active_date': '2026-09-20'}
        self.assertEqual(('2026-09-20', 'persisted_event'), event_active_date(
            {'event_id': 'music_library_event_1'}, 'main', chronicle))
