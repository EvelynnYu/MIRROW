"""Offline contracts only: isolated DBs, no provider or real device requests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from music_system.service import MusicService
from music_system.store import MusicStore, now_iso
from music_system.provider import NetEaseProvider, song_view
from music_system.errors import MusicNotFound, PlaybackUnavailable, SessionConflict
from long_text_cards import normalize_long_text_card, card_for_first_turn, card_for_history
A = {'id': '1', 'name': 'A', 'artist': 'Artist', 'album': '', 'cover': '', 'duration': 1000}
B = {**A, 'id': '2', 'name': 'B'}

class PlaybackContracts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MusicStore(Path(self.temp.name) / 'music.db')
        self.song = A
        self.calls = []

        async def adapter(action, device, song=None):
            self.calls.append((action, device))
            self.song = song or self.song
            return {'available': True, 'playing': action in {'play', 'resume'}, 'title': self.song['name'], 'artist': 'Artist', 'position_ms': 0}
        self.adapter = adapter
        self.service = MusicService(store=self.store, adapter=adapter)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_missing_confirmation_is_not_heard(self):

        async def dispatch(*args):
            return {}
        self.service.adapter = dispatch
        s = await self.service.start_song(A, 'mobile', 'single')
        self.assertEqual(s['status'], 'starting')
        self.assertEqual(self.store.activity_days(), [])
        await self.service.observe('mobile', {'available': False})
        self.assertEqual(self.service.status()['session']['status'], 'unknown')

    async def test_delivered_mobile_command_waits_for_real_media_receipt(self):

        async def dispatched(*args):
            return {'dispatched': True, 'available': False, 'playing': False, 'notification_access': True}
        self.service.adapter = dispatched
        session = await self.service.start_song(A, 'mobile', 'single')
        self.assertEqual('starting', session['status'])
        self.assertIsNotNone(session['error'])
        self.assertEqual([], self.service.store.activity_days())
        await self.service.observe('mobile', {'available': False}, session['id'])
        self.assertEqual('starting', self.service.status()['session']['status'])
        await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'A', 'artist': 'Artist', 'position_ms': 120}, session['id'])
        self.assertEqual('playing', self.service.status()['session']['status'])
        self.assertIsNone(self.service.status()['session']['error'])
        self.assertTrue(self.service.store.activity_days())

    async def test_stale_previous_song_does_not_end_new_point_play(self):
        from datetime import datetime, timedelta

        async def dispatched(*args):
            return {'dispatched': True, 'available': False, 'playing': False}
        self.service.adapter = dispatched
        session = await self.service.start_song(A, 'mobile', 'single')
        stale = {'available': True, 'playing': True, 'title': 'Anaconda', 'artist': 'Nicki Minaj', 'position_ms': 22000}
        pending = await self.service.observe('mobile', stale, session['id'])
        self.assertEqual('starting', pending['status'])
        self.assertEqual([], [event for event in self.store.events_for_session(session['id']) if event['event_type'] in {'heard', 'external_takeover'}])
        confirmed = await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'A', 'artist': 'Artist', 'position_ms': 1200}, session['id'])
        self.assertEqual('playing', confirmed['status'])
        self.assertEqual(1, len([event for event in self.store.events_for_session(session['id']) if event['event_type'] == 'heard']))
        self.service._session['commanded_at'] = (datetime.fromisoformat(now_iso()) - timedelta(seconds=31)).isoformat()
        self.assertIsNone(await self.service.observe('mobile', stale, session['id']))
        self.assertIsNone(self.service.status()['session'])

    async def test_unconfirmed_old_song_after_grace_is_not_kept_forever(self):
        from datetime import datetime, timedelta

        async def dispatched(*args):
            return {'dispatched': True, 'available': False, 'playing': False}
        self.service.adapter = dispatched
        session = await self.service.start_song(A, 'mobile', 'single')
        self.service._session['commanded_at'] = (datetime.fromisoformat(now_iso()) - timedelta(seconds=31)).isoformat()
        result = await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'Anaconda', 'artist': 'Nicki Minaj'}, session['id'])
        self.assertIsNone(result)
        self.assertTrue(any((event['event_type'] == 'external_takeover' for event in self.store.events_for_session(session['id']))))

    async def test_playlist_origin_is_projected_with_shared_queue(self):
        session = await self.service.start_song(A, 'mobile', 'loop', [A, B], playlist_id='88')
        self.assertEqual('88', session['playlist_id'])
        self.assertEqual(['1', '2'], [song['id'] for song in session['queue']])
        restored = MusicService(store=MusicStore(self.store.path), adapter=self.adapter)
        self.assertEqual('88', restored.status()['session']['playlist_id'])

    async def test_repeat_receipt_cannot_end_next_track(self):
        s = await self.service.start_song(A, 'mobile', 'loop', [A, B])
        receipt = {'available': True, 'playing': False, 'title': 'A', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'one'}
        await self.service.observe('mobile', receipt, s['id'])
        await self.service.observe('mobile', receipt, s['id'])
        self.assertEqual(self.service.status()['session']['song']['id'], '2')
        heard = [e for e in self.store.events_for_day(now_iso()[:10]) if e['event_type'] == 'heard']
        self.assertEqual(len(heard), 2)
        self.assertEqual(heard[-1]['source'], 'automatic')

    async def test_manual_next_is_skip_not_finished_receipt(self):
        session = await self.service.start_song(A, 'mobile', 'loop', [A, B])
        await self.service.control('next', session['id'])
        events = self.store.events_for_session(session['id'])
        self.assertTrue(any((e['event_type'] == 'skipped' and e['source'] == 'explicit' for e in events)))
        self.assertFalse(any((e['event_type'] == 'track_finished' for e in events)))
        self.assertNotEqual('finished', self.service.playback_receipt(session['id'], '1')['state'])

    async def test_end_receipt_wins_over_preloaded_next_metadata_without_second_pause(self):
        session = await self.service.start_song(A, 'mobile', 'single')
        calls_before = list(self.calls)
        result = await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'B', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'guard-one', 'ended_title': 'A'}, session['id'])
        self.assertIsNone(result)
        self.assertEqual(calls_before, self.calls)
        self.assertEqual('finished', self.service.playback_receipt(session['id'], '1')['state'])

    async def test_explicit_continuous_listening_waits_then_follows_stable_manual_song(self):
        from music_system import context
        session = await self.service.start_song(A, 'mobile', 'single')
        await self.service.update_session(session['id'], follow_external=True)
        ended = await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'B', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'continuous-end'}, session['id'])
        self.assertEqual(ended['status'], 'waiting_next')
        self.assertEqual(self.service.playback_receipt(session['id'], '1')['state'], 'finished')
        await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'B', 'artist': 'Artist'}, session['id'])
        self.assertEqual(self.service.status()['session']['status'], 'waiting_next')
        candidate = {'available': True, 'playing': True, 'title': 'B', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'continuous-end', 'ended_title': 'A', 'duration_ms': 180000, 'position_ms': 2300}
        await self.service.observe('mobile', candidate, session['id'])
        self.assertEqual(self.service.status()['session']['status'], 'waiting_next')
        followed = await self.service.observe('mobile', candidate, session['id'])
        self.assertEqual(followed['song']['name'], 'B')
        self.assertEqual(followed['song']['id'], '')
        self.assertEqual(followed['song']['origin'], 'manual_in_shared_session')
        still_playing = await self.service.observe('mobile', {**candidate, 'position_ms': 5600}, session['id'])
        self.assertEqual(still_playing['status'], 'playing')
        self.assertEqual(still_playing['position_ms'], 5600)
        finished = [event for event in self.store.events_for_session(session['id']) if event['event_type'] in {'finished', 'track_finished'}]
        self.assertEqual(len(finished), 1)
        with patch.object(context, 'get_service', return_value=self.service):
            self.assertIn('网易云手动选择', context.build_context())
            self.assertIn('未核验', context.build_context())
        calls_before = list(self.calls)
        await self.service.control('end', session['id'])
        self.assertEqual(self.calls, calls_before)
        self.assertIsNone(self.service.status()['session'])

    async def test_continuous_listening_survives_restart_and_waiting_timeout(self):
        session = await self.service.start_song(A, 'mobile', 'single')
        await self.service.update_session(session['id'], follow_external=True, pause_timeout_seconds=300)
        await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'A', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'waiting-end'}, session['id'])
        restarted = MusicService(store=MusicStore(self.store.path), adapter=self.adapter)
        self.assertTrue(restarted.status()['session']['follow_external'])
        self.assertEqual(restarted.status()['session']['status'], 'waiting_next')
        paused_at = restarted.status()['session']['pause_since']
        from datetime import datetime, timedelta
        later = (datetime.fromisoformat(paused_at) + timedelta(seconds=301)).isoformat()
        self.assertTrue(await restarted.expire_paused(session['id'], now=later))
        self.assertIsNone(restarted.status()['session'])

    async def test_disabling_continuous_listening_closes_wait_without_touching_player(self):
        session = await self.service.start_song(A, 'mobile', 'single')
        await self.service.update_session(session['id'], follow_external=True)
        await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'A', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'disable-wait'}, session['id'])
        before = list(self.calls)
        closed = await self.service.update_session(session['id'], follow_external=False)
        self.assertEqual(closed['status'], 'ended')
        self.assertIsNone(self.service.status()['session'])
        self.assertEqual(before, self.calls)

    async def test_music_tool_sets_real_session_mode_and_follow_flag(self):
        from behavior_scheduler.base_tool import ToolStatus
        from music_system import tools
        session = await self.service.start_song(A, 'mobile', 'single')
        with patch.object(tools, 'get_service', return_value=self.service), patch('behavior_scheduler.execution_context.get_request_platform', return_value='mobile'):
            mode = await tools.execute('session_mode', mode='one')
            follow = await tools.execute('session_follow', follow_external=True)
            invalid = await tools.execute('session_follow', follow_external='true')
        self.assertEqual(mode.status, ToolStatus.SUCCESS)
        self.assertEqual(follow.status, ToolStatus.SUCCESS)
        self.assertNotEqual(invalid.status, ToolStatus.SUCCESS)
        self.assertEqual(self.service.status()['session']['mode'], 'one')
        self.assertTrue(self.service.status()['session']['follow_external'])
        await self.service.observe('mobile', {'available': True, 'playing': False, 'title': 'A', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'tool-one'}, session['id'])
        self.assertEqual(self.service.status()['session']['status'], 'playing')

    async def test_manual_songs_remain_distinct_in_daily_evidence(self):
        from music_system import daily
        session = await self.service.start_song(A, 'mobile', 'single')
        await self.service.update_session(session['id'], follow_external=True)
        for title in ('B', 'C'):
            metadata = {'available': True, 'playing': True, 'title': title, 'artist': 'Artist', 'duration_ms': 'unknown'}
            await self.service.observe('mobile', metadata, session['id'])
            await self.service.observe('mobile', metadata, session['id'])
        with patch.object(daily, 'get_service', return_value=self.service), patch.object(daily, 'music_records', return_value=[]):
            evidence = daily.evidence_for_day(now_iso()[:10])
        self.assertEqual(3, len(evidence))
        self.assertEqual(2, sum(('没有核验网易云歌曲 ID' in item['identity_basis'] for item in evidence)))

    async def test_manual_change_without_continuous_listening_still_exits(self):
        session = await self.service.start_song(A, 'mobile', 'single')
        await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'B', 'artist': 'Artist'}, session['id'])
        self.assertIsNone(self.service.status()['session'])

    async def test_restart_no_auto_queue_or_duplicate_heard(self):
        await self.service.start_song(A, 'mobile', 'loop', [A, B])
        service = MusicService(store=MusicStore(self.store.path), adapter=self.adapter)
        await service.observe('mobile', {'available': True, 'playing': True, 'title': 'A', 'artist': 'Artist'})
        await service.observe('mobile', {'available': True, 'playing': False, 'title': 'A', 'artist': 'Artist', 'end_of_track': True, 'end_token': 'restart-end'})
        self.assertEqual(service.status()['session']['song']['id'], '1')
        self.assertEqual(len([e for e in self.store.events_for_day(now_iso()[:10]) if e['event_type'] == 'heard']), 1)

    async def test_explicit_other_device_can_replace_unowned_restart_state(self):
        await self.service.start_song(A, 'mobile', 'single')
        restarted = MusicService(store=MusicStore(self.store.path), adapter=self.adapter)
        result = await restarted.start_song(B, 'computer', 'single')
        self.assertEqual(result['device'], 'computer')
        self.assertEqual(result['song']['id'], '2')

    async def test_ended_session_not_resurrected(self):
        s = await self.service.start_song(A, 'mobile', 'single')
        await self.service.control('end', s['id'])
        self.assertIsNone(MusicStore(self.store.path).active_session())

    async def test_cross_device_and_stale_observation(self):
        s = await self.service.start_song(A, 'mobile', 'single')
        with self.assertRaises(SessionConflict):
            await self.service.start_song(B, 'computer', 'single')
        await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'other'}, 'stale-session')
        self.assertEqual(self.service.status()['session']['id'], s['id'])

    async def test_pause_failure_is_unknown_not_completed(self):
        s = await self.service.start_song(A, 'mobile', 'single')

        async def fail(*args):
            return {'playing': True}
        self.service.adapter = fail
        with self.assertRaises(PlaybackUnavailable):
            await self.service.control('end', s['id'])
        self.assertEqual(self.service.status()['session']['status'], 'unknown')

    async def test_same_title_different_artist_releases(self):
        await self.service.start_song(A, 'mobile', 'loop', [A, B])
        await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'A', 'artist': 'Someone else'})
        self.assertIsNone(self.service.status()['session'])

    async def test_quiet_scoped_and_context_objective(self):
        from music_system import context
        s = await self.service.start_song(A, 'mobile', 'single')
        await self.service.update_session(s['id'], quiet=True)
        with patch.object(context, 'get_service', return_value=self.service):
            self.assertTrue(context.quiet_active())
            self.assertIn('设备确认播放中', context.build_context())
            self.assertNotIn('你应该', context.build_context())
            await self.service.observe('mobile', {'available': False})
            self.assertTrue(context.quiet_active())
            await self.service.control('end', s['id'])
            self.assertFalse(context.quiet_active())

    async def test_lyrics_context_is_bounded_and_follows_position(self):
        from music_system import context
        session = await self.service.start_song(A, 'mobile', 'single')
        lyrics = '\n'.join((f'[{i // 60:02d}:{i % 60:02d}.00]line-{i}-' + 'x' * 30 for i in range(200)))
        self.store.save_material('1', {'lyrics': lyrics})
        await self.service.observe('mobile', {'available': True, 'playing': True, 'title': 'A', 'artist': 'Artist', 'position_ms': 120000}, session['id'])
        with patch.object(context, 'get_service', return_value=self.service):
            built = context.build_context()
        self.assertIn('line-120', built)
        self.assertLess(len(built), 4000)

    async def test_active_context_includes_only_cached_melody_material(self):
        from music_system import context
        await self.service.start_song(A, 'mobile', 'single')
        self.store.save_material('1', {'melody_summary': '约 92 BPM；能量较平稳'})
        with patch.object(context, 'get_service', return_value=self.service):
            built = context.build_context()
        self.assertIn('约 92 BPM', built)
        self.assertIn('不是实时听觉回执', built)

    async def test_empty_day_never_calls_llm(self):
        from music_system import daily
        called = []

        async def llm(prompt):
            called.append(prompt)
            return 'summary'
        with patch.object(daily, 'get_service', return_value=self.service), patch.object(daily, 'music_records', return_value=[]), patch.object(daily, '_llm', llm):
            result = await daily.run('2000-01-01')
            self.assertEqual(result['status'], 'no_activity')
            self.assertEqual(called, [])

    async def test_daily_retry_reuses_summary_and_evidence(self):
        from music_system import daily
        from unittest.mock import AsyncMock
        evidence = [{'id': 'e1', 'speaker': 'device', 'text': '使用者与 K 的共同播放：设备确认歌曲开始播放。', 'timestamp': '2000-01-01T12:00:00+08:00', 'identity_basis': '设备回执'}]
        llm = AsyncMock(return_value='今天有一次共同播放记录。')
        world = AsyncMock(side_effect=[RuntimeError('test'), {'status': 'completed'}])
        with patch.object(daily, 'get_service', return_value=self.service), patch.object(daily, 'evidence_for_day', return_value=evidence), patch.object(daily, '_llm', llm), patch('cognition.maintenance.run_evidence', world):
            with self.assertRaises(MusicNotFound):
                await daily.run('2000-01-01')
            self.assertEqual(self.store.daily('2000-01-01')['status'], 'error')
            await daily.run('2000-01-01')
            await daily.run('2000-01-01')
            llm.assert_awaited_once()
            self.assertEqual(world.await_count, 2)
            self.assertEqual(self.store.daily('2000-01-01')['status'], 'completed')

    async def test_k_music_reflection_is_owned_by_interest_lane_not_music_world_lane(self):
        from music_system import daily
        from unittest.mock import AsyncMock
        evidence = [{'id': 'music-note:node-1', 'speaker': 'k', 'text': '歌曲《测试曲》；没外放，就把它存进歌单。', 'timestamp': '2000-01-02T12:00:00+08:00', 'identity_basis': 'K 漫想反思'}]
        world = AsyncMock()
        subjective = AsyncMock(return_value={'status': 'completed'})
        with patch.object(daily, 'get_service', return_value=self.service), patch.object(daily, 'evidence_for_day', return_value=evidence), patch.object(daily, '_llm', AsyncMock(return_value='K 分析了一首歌。')), patch('cognition.maintenance.run_evidence', world), patch('cognition.autonomous.run_evidence', subjective):
            result = await daily.run('2000-01-02')
        self.assertEqual(result['status'], 'completed')
        world.assert_not_awaited()
        subjective.assert_not_awaited()

    async def test_daily_uses_library_delta_and_distinguishes_skip_from_finish(self):
        from music_system import daily
        session = await self.service.start_song(A, 'mobile', 'loop', [A, B])
        await self.service.control('next', session['id'])
        self.store.library_event('tracks_added', 'p1', 'k', {'name': 'K Shelf', 'song_ids': ['2'], 'verified_change': True})
        with patch.object(daily, 'get_service', return_value=self.service), patch.object(daily, 'music_records', return_value=[]):
            evidence = daily.evidence_for_day(now_iso()[:10])
        joined = '\n'.join((item['text'] for item in evidence))
        self.assertIn('明确跳过 1 次', joined)
        self.assertIn('自然到达曲尾 0 次', joined)
        self.assertIn('K Shelf', joined)

    def test_shared_search_matches_beyond_recent_hundred(self):
        self.store.record_song_share({**A, 'id': 'target', 'name': 'Needle Old Song'}, 'message:target', 'owner')
        for index in range(105):
            self.store.record_song_share({**A, 'id': str(1000 + index), 'name': f'Recent {index}'}, f'message:recent-{index}', 'owner')
        self.assertEqual('target', self.store.search_shared_songs('Needle Old')[0]['id'])

    async def test_playlist_uncertain_creation_does_not_create_twice(self):
        from music_system import library
        from unittest.mock import AsyncMock
        self.service.provider.create_playlist = AsyncMock(side_effect=MusicNotFound('uncertain'))
        self.service.provider.playlists = AsyncMock(return_value=[])
        with patch.object(library, 'get_service', return_value=self.service):
            with self.assertRaises(MusicNotFound):
                await library.create('Test', 'k')
            with self.assertRaises(MusicNotFound):
                await library.create('Test', 'k')
            self.service.provider.create_playlist.assert_awaited_once()

    async def test_http_contracts_use_same_service(self):
        import httpx
        from fastapi import FastAPI
        from music_system.router import router
        from unittest.mock import AsyncMock
        app = FastAPI()
        app.include_router(router)
        self.service.provider.song = AsyncMock(return_value=A)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with patch('music_system.router.get_service', return_value=self.service):
                response = await client.post('/api/music/v2/play', json={'song_id': '1', 'device': 'mobile', 'mode': 'single'})
                self.assertEqual(response.status_code, 200)
                sid = response.json()['session']['id']
                response = await client.patch('/api/music/v2/session', json={'session_id': sid, 'quiet': True})
                self.assertTrue(response.json()['session']['quiet'])
                response = await client.patch('/api/music/v2/session', json={'session_id': sid, 'follow_external': True})
                self.assertTrue(response.json()['session']['follow_external'])
                invalid = await client.post('/api/music/v2/control', json={'session_id': 'stale', 'action': 'pause'})
                self.assertEqual(invalid.status_code, 409)

class CardAndProviderContracts(unittest.IsolatedAsyncioTestCase):

    def test_music_persists_fields_without_longtext_setting(self):
        card = normalize_long_text_card({'id': 'c1', 'title': 'A', 'body': '送给你', 'kind': 'music', 'song_id': '1', 'artist': 'Artist', 'duration': '1000'})
        self.assertEqual(card['song_id'], '1')
        self.assertIn('分享资料', card_for_first_turn(card))
        self.assertEqual(card_for_history(card, False), card_for_history(card, True))

    def test_playlist_card_keeps_bounded_queue_preview(self):
        card = normalize_long_text_card({'id': 'playlist-card', 'title': '夜路', 'body': '一起听', 'kind': 'music', 'music_type': 'playlist', 'playlist_id': '88', 'track_count': '12', 'track_preview': 'A—Artist；B—Artist', 'play_mode': 'loop'})
        self.assertEqual('88', card['playlist_id'])
        rendered = card_for_first_turn(card)
        self.assertIn('歌单卡片', rendered)
        self.assertIn('A—Artist', rendered)

    def test_song_card_context_carries_material_receipts(self):
        card = normalize_long_text_card({'id': 'song-card', 'title': 'A', 'body': '一起听', 'kind': 'music', 'song_id': '1', 'artist': 'Artist', 'lyrics_excerpt': '第一句', 'melody_summary': '约 90 BPM；能量平稳', 'lyrics_available': '1'})
        rendered = card_for_first_turn(card)
        self.assertIn('歌词材料：第一句', rendered)
        self.assertIn('旋律分析：约 90 BPM', rendered)

    def test_cover_not_arbitrary_remote_address(self):
        card = normalize_long_text_card({'id': 'c1', 'title': 'A', 'body': 'x', 'kind': 'music', 'song_id': '1', 'cover': 'http://127.0.0.1/private'})
        self.assertEqual(card['cover'], '')
        with self.assertRaises(ValueError):
            normalize_long_text_card({**card, 'song_id': '../bad'})

    def test_raw_track_artist_album_mapping(self):
        song = song_view({'id': 1, 'name': 'A', 'ar': [{'name': 'Artist'}], 'al': {'name': 'Album', 'picUrl': 'https://p1.music.126.net/a'}, 'dt': 1000})
        self.assertEqual(song['artist'], 'Artist')
        self.assertEqual(song['album'], 'Album')

    async def test_resolve_shared_text_without_network(self):

        class Provider(NetEaseProvider):

            async def song(self, sid):
                return {**A, 'id': sid}
        result = await Provider().resolve('分享歌曲《A》 https://music.163.com/#/song?id=1 （来自网易云）')
        self.assertEqual(result['song']['id'], '1')
        for url in ['http://127.0.0.1/song?id=1', 'https://music.163.com.evil/song?id=1', 'https://music.163.com:444/song?id=1', 'https://name@music.163.com/song?id=1']:
            with self.assertRaises(MusicNotFound):
                await Provider().resolve(url)

    async def test_account_session_does_not_touch_global(self):
        import pyncm
        with tempfile.TemporaryDirectory() as tmp:
            provider = NetEaseProvider(Path(tmp) / 'missing.json')
            global_session = pyncm.GetCurrentSession()
            scoped = await provider._call(pyncm.GetCurrentSession)
            self.assertIsNot(scoped, global_session)
            self.assertIs(pyncm.GetCurrentSession(), global_session)
if __name__ == '__main__':
    unittest.main()
