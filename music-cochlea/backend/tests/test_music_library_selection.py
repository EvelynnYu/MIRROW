import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from music_system.store import MusicStore
from music_system.provider import NetEaseProvider
from music_system.provider import song_view
from music_system.service import MusicService
from music_system import library
from music_system.router import PlaylistSyncBody
from music_system.errors import MusicNotFound
from pydantic import ValidationError


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_selected_and_subjects_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('3', 'k', 'Existing')
            service = SimpleNamespace(store=store, provider=SimpleNamespace(playlists=AsyncMock(return_value=[
                {'id': '1', 'name': 'One'}, {'id': '2', 'name': 'Two'}])))
            with patch.object(library, 'get_service', return_value=service):
                result = await library.sync_selected([{'id': '1', 'subject': 'shared'}])
                self.assertEqual({p['id']: p['subject'] for p in result}, {'1': 'shared', '3': 'k'})
                with self.assertRaises(MusicNotFound):
                    await library.sync_selected([{'id': '2', 'subject': 'owner'}, {'id': '4', 'subject': 'k'}])
                self.assertEqual({p['id'] for p in store.bindings()}, {'1', '3'})
                with self.assertRaises(MusicNotFound):
                    await library.sync_selected([{'id': '1', 'subject': 'k'}] * 2)

    async def test_logged_in_does_not_issue_qr(self):
        provider = NetEaseProvider()
        provider._account = Mock(return_value=123)
        async def call(fn, *args): return fn(*args)
        provider._call = call
        with patch('pyncm.apis.login.LoginQrcodeUnikey') as generate:
            self.assertEqual(await provider.login_start(), {'status': 'success'})
            self.assertEqual(await provider.login_check(), {'status': 'success'})
            generate.assert_not_called()

    async def test_provider_delete_requires_account_ownership_and_verifies_absence(self):
        provider = NetEaseProvider()
        provider._account = Mock(return_value=7)
        async def call(fn, *args): return fn(*args)
        provider._call = call
        owned = {'id': 3, 'name': 'K Shelf', 'userId': 7, 'trackCount': 1}
        with patch('pyncm.apis.user.GetUserPlaylists', side_effect=[
            {'code': 200, 'playlist': [owned], 'more': False},
            {'code': 200, 'playlist': [], 'more': False},
        ]), patch('pyncm.apis.playlist.SetRemovePlaylist', return_value={'code': 200}) as remove:
            deleted = await provider.delete_playlist('3')
        self.assertTrue(deleted['owned'])
        remove.assert_called_once_with([3])

    def test_old_bulk_sync_and_bad_shelf_rejected(self):
        for value in ({}, {'selections': []}, {'selections': [{'id': '1', 'subject': 'other'}]}):
            with self.assertRaises(ValidationError): PlaylistSyncBody.model_validate(value)

    def test_provider_translation_is_same_song_not_arbitrary_version(self):
        song = song_view({'id': 438462713, 'name': 'My Jinji', 'tns': ['我的金桔'], 'artist': '落日飞车'})
        self.assertTrue(MusicService._matches(song, 'My Jinji (我的金桔)', '落日飞车'))
        self.assertTrue(MusicService._matches(song, 'My Jinji（我的金桔）', '落日飞车'))
        self.assertFalse(MusicService._matches(song, 'My Jinji (Live)', '落日飞车'))
        self.assertFalse(MusicService._matches(song, 'My Jinji', 'Other artist'))

    async def test_rename_updates_remote_and_local_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('3', 'k', 'Old')
            provider = SimpleNamespace(rename_playlist=AsyncMock(return_value={'id':'3','name':'New','songs':[]}))
            service = SimpleNamespace(store=store, provider=provider)
            with patch.object(library, 'get_service', return_value=service):
                renamed = await library.rename('3', 'New')
            self.assertEqual('New', renamed['name'])
            self.assertEqual('New', store.bindings()[0]['name'])
            provider.rename_playlist.assert_awaited_once_with('3', 'New')

    async def test_track_mutation_refreshes_cache_and_wander_uses_explicit_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('3', 'k', 'First')
            store.bind_playlist('4', 'k', 'Chosen')
            updated = {'id':'4','name':'Chosen','songs':[{'id':'9','name':'Nine','artist':'A','duration':1}],
                       '_changed_song_ids':['9']}
            provider = SimpleNamespace(change_tracks=AsyncMock(return_value=updated))
            service = SimpleNamespace(store=store, provider=provider)
            with patch.object(library, 'get_service', return_value=service):
                default = library.set_wander_default('4')
                self.assertTrue(default['wander_default'])
                result = await library.collect('node-one', {'netease_song_id':'9'}, 'K明确想留下')
            self.assertEqual('completed', result['status'])
            self.assertEqual('4', result['playlist_id'])
            self.assertEqual(updated, store.material('playlist:4'))
            provider.change_tracks.assert_awaited_once_with('4', ['9'], 'add')
            events = store.library_events_for_day(store.bindings()[0]['imported_at'][:10])
            self.assertTrue(any(event['event_type'] == 'tracks_added' for event in events))

    async def test_existing_track_does_not_create_a_false_add_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('4', 'k', 'Chosen')
            unchanged = {'id':'4','name':'Chosen','songs':[{'id':'9','name':'Nine','artist':'A'}],
                         '_changed_song_ids':[]}
            service = SimpleNamespace(store=store, provider=SimpleNamespace(
                change_tracks=AsyncMock(return_value=unchanged)))
            with patch.object(library, 'get_service', return_value=service):
                result = await library.change_tracks('4', ['9'], 'add', source='wander',
                                                     reason='再次想到这首歌', node_id='node-one')
            self.assertNotIn('_changed_song_ids', result)
            self.assertFalse(result['change_applied'])
            self.assertEqual([], [event for event in store.library_events_after(0)
                                  if event['event_type'] == 'tracks_added'])
            with patch.object(library, 'get_service', return_value=service):
                receipt = await library.apply_wander_action(
                    'node-one', {'netease_song_id':'9', 'title':'Nine'},
                    {'action':'add_current_song', 'playlist_id':'4'}, '再次想到这首歌',
                )
            self.assertEqual('completed', receipt['status'])
            self.assertFalse(receipt['changed'])

    async def test_provider_reports_noop_before_remote_mutation(self):
        provider = NetEaseProvider()
        provider._account = Mock(return_value=7)
        provider._playlist = Mock(return_value={'id':'4','name':'Chosen',
                                               'songs':[{'id':'9','name':'Nine'}]})
        async def call(fn, *args): return fn(*args)
        provider._call = call
        with patch('pyncm.apis.playlist.SetManipulatePlaylistTracks') as mutate:
            item = await provider.change_tracks('4', ['9'], 'add')
        self.assertEqual([], item['_changed_song_ids'])
        mutate.assert_not_called()

    def test_unbind_is_local_and_default_requires_k_shelf(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('3', 'shared', 'Together')
            with self.assertRaises(ValueError):
                store.set_wander_default('3', True)
            self.assertTrue(store.unbind_playlist('3'))
            self.assertEqual([], store.bindings())

    async def test_delete_is_remote_only_for_k_and_keeps_a_local_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('3', 'shared', 'Together')
            provider = SimpleNamespace(
                playlist=AsyncMock(return_value={'id':'3','name':'Together','songs':[]}),
                delete_playlist=AsyncMock(return_value={'id':'3','name':'Together'}),
            )
            service = SimpleNamespace(
                store=store, provider=provider,
                status=Mock(return_value={'session': None}),
            )
            with patch.object(library, 'get_service', return_value=service):
                with self.assertRaises(MusicNotFound):
                    await library.delete('3')
                store.bind_playlist('3', 'k', 'K Shelf')
                removed = await library.delete('3', source='wander')
            self.assertTrue(removed['deleted'])
            self.assertEqual([], store.bindings())
            self.assertEqual('completed', store.material('playlist-delete:3')['status'])
            provider.delete_playlist.assert_awaited_once_with('3')

    async def test_structured_wander_action_adds_only_the_current_song_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('4', 'k', 'K Shelf')
            updated = {'id':'4','name':'K Shelf','songs':[{'id':'9','name':'Nine','artist':'A'}],
                       '_changed_song_ids':['9']}
            provider = SimpleNamespace(change_tracks=AsyncMock(return_value=updated))
            service = SimpleNamespace(store=store, provider=provider)
            with patch.object(library, 'get_service', return_value=service):
                first = await library.apply_wander_action(
                    'node-9', {'netease_song_id':'9'},
                    {'action':'add_current_song','playlist_id':'4'}, '想留下',
                )
                second = await library.apply_wander_action(
                    'node-9', {'netease_song_id':'10'},
                    {'action':'add_current_song','playlist_id':'4'}, '重试',
                )
            self.assertEqual('completed', first['status'])
            self.assertTrue(first['changed'])
            self.assertEqual(first, second)
            provider.change_tracks.assert_awaited_once_with('4', ['9'], 'add')

    async def test_pending_playlist_delete_reconciles_without_replaying_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MusicStore(Path(directory) / 'music.db')
            store.bind_playlist('5', 'k', 'Old Shelf')
            store.save_material('playlist-delete:5', {
                'status':'pending', 'source':'wander',
                'playlist':{'id':'5','name':'Old Shelf','songs':[{'id':'9'}]},
            })
            provider = SimpleNamespace(
                playlists=AsyncMock(return_value=[]),
                delete_playlist=AsyncMock(side_effect=AssertionError('must not replay')),
            )
            service = SimpleNamespace(
                store=store, provider=provider,
                status=Mock(return_value={'session': None}),
            )
            with patch.object(library, 'get_service', return_value=service):
                result = await library.delete('5', source='wander')
            self.assertTrue(result['deleted'])
            self.assertEqual([], store.bindings())
            provider.delete_playlist.assert_not_awaited()


if __name__ == '__main__': unittest.main()
