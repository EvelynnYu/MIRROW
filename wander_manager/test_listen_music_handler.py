"""Public music callbacks preserve truthful completion and stable evidence."""
import unittest
from unittest.mock import Mock
from wander_manager.event_handlers import ListenMusicHandler
from wander_manager.host_hooks import configure_music


class MusicHostTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        configure_music()

    async def test_unconfigured_playback_is_not_success(self):
        self.assertFalse(await ListenMusicHandler().start_runtime_playback({'title':'test'}))

    async def test_device_failure_never_falls_back(self):
        playback=Mock(return_value=False)
        configure_music(playback=playback)
        self.assertFalse(await ListenMusicHandler().start_runtime_playback({'title':'test'}, device='mobile'))
        playback.assert_called_once()
        self.assertEqual('mobile', playback.call_args.args[0]['device'])

    async def test_experience_uses_stable_node_and_never_overwrites_self_book(self):
        sink=Mock(return_value=True)
        configure_music(record=sink)
        book=Mock()
        handler=ListenMusicHandler(k_self_book=book)
        self.assertTrue(await handler._update_self_book(
            {'title':'test', 'fingerprint':'synthetic-fingerprint', 'cognition_source_id':'node-1'}, 'reflection', 'reason'))
        book.upsert_entry.assert_not_called()
        self.assertEqual('node-1', sink.call_args.kwargs['source_id'])

    async def test_unknown_material_is_not_recorded(self):
        sink=Mock(return_value=True)
        configure_music(record=sink)
        self.assertFalse(await ListenMusicHandler()._update_self_book({}, '', ''))
        sink.assert_not_called()
