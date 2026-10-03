import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from music_system.store import MusicStore
from music_system import sharing
from behavior_scheduler.base_tool import ToolResult, ToolStatus
from behavior_scheduler.cloud_music_tool import CloudMusicTool

class SharingTests(unittest.IsolatedAsyncioTestCase):

    async def test_playlist_mode_survives_async_tool_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'm.db')
            resolve = AsyncMock(return_value=ToolResult(ToolStatus.SUCCESS, '', extra_data={'music_card': {'id': '88', 'kind': 'playlist'}}))
            with patch.object(sharing, 'get_service', return_value=SimpleNamespace(store=store)), patch.object(sharing, '_fill', AsyncMock(return_value=True)), patch('music_system.tools.execute', resolve):
                for mode in ('list', 'one', 'loop'):
                    result = await CloudMusicTool().execute('playlist_share', playlist_id='88', mode=mode, _session_id='test')
                    gid = result.extra_data['generation_id']
                    await sharing._tasks[gid]
                    self.assertEqual(resolve.await_args.kwargs['mode'], mode)
                    self.assertEqual(resolve.await_args.args[0], 'playlist_share')
                result = await CloudMusicTool().execute('playlist_share', query='My playlist', mode='list', _session_id='test')
                await sharing._tasks[result.extra_data['generation_id']]
                self.assertEqual(resolve.await_args.args, ('playlist_share', 'My playlist'))
                self.assertEqual(resolve.await_args.kwargs['mode'], 'list')

    async def test_pending_ready_and_no_inline_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'm.db')
            resolve = AsyncMock(return_value=ToolResult(ToolStatus.SUCCESS, '', extra_data={'music_card': {'id': '1', 'name': 'Song'}}))
            with patch.object(sharing, 'get_service', return_value=SimpleNamespace(store=store)), patch.object(sharing, '_fill', AsyncMock(return_value=True)), patch('music_system.tools.execute', resolve):
                result = await sharing.enqueue('Song', session_id='test')
                self.assertEqual(result.delivery, 'ui_only')
                self.assertEqual(result.extra_data['attachment_status'], 'pending')
                resolve.assert_not_awaited()
                gid = result.extra_data['generation_id']
                await sharing._tasks[gid]
                card = (await sharing.status(gid))['music_card']
                self.assertEqual(card['id'], '1')
                self.assertNotIn('share_number', card)

    async def test_failure_and_restart_not_permanent_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'm.db')
            with patch.object(sharing, 'get_service', return_value=SimpleNamespace(store=store)), patch.object(sharing, '_fill', AsyncMock(return_value=True)), patch('music_system.tools.execute', AsyncMock(side_effect=RuntimeError('private error'))):
                result = await sharing.enqueue('Song', session_id='test')
                gid = result.extra_data['generation_id']
                await sharing._tasks[gid]
                status = await sharing.status(gid)
                self.assertEqual(status['attachment_status'], 'failed')
                self.assertNotIn('private', status['attachment_error'])
                store.save_material('share:' + 'a' * 32, {'generation_id': 'a' * 32, 'session_id': 'test', 'attachment_kind': 'music', 'attachment_status': 'pending'})
                self.assertEqual((await sharing.status('a' * 32))['attachment_status'], 'failed')

    async def test_terminal_success_only_is_ui_only(self):
        tool = CloudMusicTool()
        with patch('music_system.tools.execute', AsyncMock(return_value=ToolResult(ToolStatus.SUCCESS, 'done'))):
            self.assertEqual((await tool.execute('playlist_add', song_id='1', playlist_id='2')).delivery, 'ui_only')
        with patch('music_system.tools.execute', AsyncMock(return_value=ToolResult(ToolStatus.SUCCESS, 'id'))):
            self.assertEqual((await tool.execute('playlist_create', query='Test', continue_after=True)).delivery, 'model')
        with patch('music_system.tools.execute', AsyncMock(return_value=ToolResult(ToolStatus.ERROR, '', error='failed'))):
            self.assertEqual((await tool.execute('playlist_add')).delivery, 'model')
            self.assertEqual(tool.get_result_delivery(action='playlist_add'), 'ui_only')
            self.assertEqual(tool.get_result_delivery(action='playlist_create', continue_after=True), 'model')
            self.assertEqual(tool.get_result_delivery(action='share'), 'ui_only')
            self.assertEqual(tool.get_result_delivery(action='playlist_add', _result=ToolResult(ToolStatus.ERROR, '')), 'model')
            self.assertEqual(tool.get_result_delivery(action='playlist_add', _execution_failed=True), 'model')

    async def test_fill_updates_both_sources_once_and_never_creates_deleted_message(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'm.db')
            gid = 'b' * 32
            call = {'tool': 'cloud_music', 'extra_data': {'generation_id': gid, 'attachment_kind': 'music', 'attachment_status': 'pending'}}
            rows = [{'message_id': 'original', 'tool_calls': json.dumps([call])}]
            chronicle = SimpleNamespace(find_tool_attachment_messages=Mock(return_value=rows), update_message_tool_calls=Mock(return_value=True))
            manager = SimpleNamespace(update_message_tool_calls=AsyncMock(return_value=True))
            job = {'generation_id': gid, 'session_id': 'session', 'attachment_kind': 'music', 'attachment_status': 'ready', 'music_card': {'id': '1'}}
            with patch.object(sharing, 'get_service', return_value=SimpleNamespace(store=store)), patch('event_chronicle.get_global_chronicle', return_value=chronicle), patch('session_manager.get_global_session_manager', return_value=manager), patch.object(sharing, '_push_reload', AsyncMock()):
                self.assertTrue(await sharing._fill(job))
                self.assertEqual(manager.update_message_tool_calls.await_count, 2)
                self.assertTrue(job['share_recorded'])
                self.assertEqual(job['music_card']['share_number'], 1)
                rows[0]['tool_calls'] = chronicle.update_message_tool_calls.call_args.args[1]
                self.assertTrue(await sharing._fill(job))
                self.assertEqual(chronicle.update_message_tool_calls.call_count, 2)
                chronicle.find_tool_attachment_messages.return_value = []
                self.assertFalse(await sharing._fill(job))
                self.assertEqual(chronicle.update_message_tool_calls.call_count, 2)

    async def test_fill_retries_session_projection_after_sqlite_success(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'm.db')
            gid = 'c' * 32
            job = {'generation_id': gid, 'session_id': 'session', 'attachment_kind': 'music', 'attachment_status': 'ready', 'music_card': {'id': '1'}}
            calls = [{'extra_data': sharing.projection(job)}]
            rows = [{'message_id': 'bad', 'tool_calls': 'broken'}, {'message_id': 'original', 'tool_calls': json.dumps(calls)}]
            chronicle = SimpleNamespace(find_tool_attachment_messages=Mock(return_value=rows), update_message_tool_calls=Mock(return_value=True))
            manager = SimpleNamespace(update_message_tool_calls=AsyncMock(side_effect=[False, True, True]))
            with patch.object(sharing, 'get_service', return_value=SimpleNamespace(store=store)), patch('event_chronicle.get_global_chronicle', return_value=chronicle), patch('session_manager.get_global_session_manager', return_value=manager):
                self.assertFalse(await sharing._fill(job))
                self.assertTrue(await sharing._fill(job))
                self.assertEqual(manager.update_message_tool_calls.await_count, 3)
                chronicle.update_message_tool_calls.assert_called_once()
if __name__ == '__main__':
    unittest.main()
