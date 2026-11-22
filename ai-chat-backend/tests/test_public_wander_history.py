"""Isolated SQLite and synthetic receipts for the public Wander release."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from context_builder.event_history_adapter import adapt_history_for_context
from context_builder.wander_event_projection import format_wander_activity_for_chat, inner_event_text
from context_builder.text_utils import build_tool_history_fact
from memory_v2.source_projection import memory_source_content
from wander_manager.essays import card_text
from wander_manager.event_types import EventType
from wander_manager.runtime_models import ActivityState, NodeState, WanderActivity, WanderNode, WanderRun
from wander_manager.runtime_share_adapter import RuntimeShareAdapter
from wander_manager.runtime_store import WanderRuntimeStore


class WanderHistoryTests(TestCase):
    def test_reading_and_picture_survive_reopen_without_a_private_message(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wander.db'
            store = WanderRuntimeStore(path)
            store.initialize()
            run = WanderRun(session_id='sample')
            store.save_run(run)
            activity = WanderActivity('browse_bookmarks', run.run_id, state=ActivityState.COMPLETED,
                                      emotion_effect={'changed': False}, summary='这句话让我想再看一眼。')
            store.save_activity(activity)
            work = {'source': 'essays', 'title': '雨后', 'id': 'sample-work', 'depth': 'deep'}
            node = WanderNode(activity.activity_id, 1, state=NodeState.COMPLETED,
                              execution_status='succeeded', source_payload={
                                  'reading_received': True, 'bookshelf_snapshot': {'version': 1, 'items': [work]}},
                              side_effect_refs={'reading_illustration': {
                                  'source': 'essays', 'work_id': work['id'], 'title': work['title'],
                                  'quote': '窗边', 'scene': '窗台', 'start': 0, 'end': 2, 'status': 'ready',
                                  'url': '/static/essay_illustrations/' + '0'*32 + '.png'}})
            store.save_node(node)
            events = []
            def record(payload):
                events.append(deepcopy(payload))
                return True
            def unexpected(*_):
                self.fail('An event projection should not require generation or sharing')
            for reopened in (store, WanderRuntimeStore(path)):
                adapter = RuntimeShareAdapter(reopened, unexpected, unexpected, activity_event_callback=record)
                self.assertTrue(asyncio.run(adapter.record_activity(activity.activity_id)))
            self.assertEqual(len(events), 1)
            row = {'role': 'event', 'event_type': 'wander_activity', 'message_id': 'activity_event_' + activity.activity_id,
                   'content': events[0]['summary'], 'tool_calls': events[0]['tool_calls'], 'timestamp': events[0]['occurred_at']}
            before = deepcopy(row)
            canonical = memory_source_content(row)
            for content in (inner_event_text(row, frozen_only=True),
                            adapt_history_for_context([row], for_provider=True, wander_level='daily')[0]['content']):
                self.assertIn('雨后', content)
                self.assertIn('画了一幅读后插画', content)
            node.source_payload = {}
            store.save_node(node)
            self.assertEqual(memory_source_content(row), canonical)
            self.assertEqual(row, before)

    def test_chat_time_and_tiers(self):
        now = datetime(2026, 1, 2, 12)
        call = {'description':'翻书', 'extra_data': {'emotion_effect': {'changed': True}}}
        def tier(minutes, **options):
            return format_wander_activity_for_chat('留下的感想', [call],
                timestamp=(now-timedelta(minutes=minutes)).isoformat(), now=now, **options)
        recent, normal = tier(5)
        older, _ = tier(122)
        self.assertEqual(normal, 'normal')
        self.assertNotEqual(recent, older)
        call['extra_data']['emotion_effect']['persistence'] = 'lingering'
        self.assertEqual(tier(300)[1], 'strong')
        self.assertNotEqual(tier(300)[0], tier(300, strong_full=False)[0])

    def test_unshared_work_is_an_index_and_shared_work_is_the_original(self):
        article = {'id':'work', 'title':'潮汐', 'keyword':'水', 'content':'这是作品正文。'}
        reading = WanderNode('sample', 1, state=NodeState.COMPLETED, execution_status='succeeded')
        reading.source_payload = {'essay_version':1,'keyword':'水','title':'潮汐','expansion':article['content']}
        calls = RuntimeShareAdapter._tool_calls(
            EventType.KEYWORD_EXPANSION,
            {'activity_type':'keyword_expansion','activity_id':'sample','state':'completed'}, [vars(reading)])
        index = inner_event_text({'content':'', 'tool_calls':calls, 'role':'event','event_type':'wander_activity'})
        self.assertIn('潮汐', index)
        self.assertNotIn(article['content'], index)
        share = {'tool':'essay_card','status':'success','extra_data':{'essay_card':article, 'wander_projection_version':2}}
        self.assertIn(article['content'], build_tool_history_fact('assistant','',[share]))
        self.assertIn(card_text(article), memory_source_content({'role':'assistant','content':'给你看。',
            'active_date':'2026-11-22','tool_calls':[share]}))

    def test_old_and_public_account_headings_keep_exact_source_offsets(self):
        from memory_v2.source_sections import personal_account_start
        from memory_v2.retained_account_candidate import _account_body
        account = '雨停后想出门。'
        event = {'role': 'event', 'event_type': 'wander_activity', 'tool_calls': [
            {'extra_data': {'retained_experience': {'impression': account}}}]}
        for label in ('AI', 'Z'):
            section = f'\n{label} 当时选择留下的感受：{account}'
            text = '已散步。' + section
            self.assertEqual(personal_account_start(event, text), len('已散步。'))
            self.assertEqual(_account_body({'text': section}), account)
        self.assertIsNone(personal_account_start(event, '一段正文：'+account))

    def test_creator_initializes_one_persistent_runner_with_old_settings(self):
        from unittest.mock import patch
        from wander_manager.config import WanderConfig
        from wander_manager.wander_creator import WanderCreator
        config = WanderConfig.from_dict({'event_interval': 73, 'use_activity_engine': True})
        with TemporaryDirectory() as tmp:
            store = WanderRuntimeStore(Path(tmp) / 'runtime.db')
            creator = WanderCreator(event_interval=config.event_interval)
            with patch('wander_manager.runtime_store.WanderRuntimeStore', return_value=store), \
                 patch('wander_manager.wish_store.get_wish_store', return_value=None):
                runner = creator._ensure_runtime_runner()
                runner.initialize()
                self.assertIs(runner, creator._ensure_runtime_runner())
                self.assertIs(runner.store, store)
                self.assertEqual(runner.default_interval_seconds, 73)
                self.assertTrue(store.db_path.is_file())

    def test_public_catalog_matches_bookcase_and_current_planner(self):
        from context_builder.recipes import get_recipe
        from wander_manager.event_catalog import EVENT_CATALOG, render_event_options
        self.assertTrue(all(entry.experience for entry in EVENT_CATALOG.values()))
        options = render_event_options({EventType.KEYWORD_EXPANSION, EventType.BROWSE_BOOKMARKS})
        for name in ('随笔', '收藏'):
            self.assertIn(name, options)
        self.assertNotIn('相册图片', options)
        self.assertIsNotNone(get_recipe('WANDER_V2'))

    def test_restoring_last_reply_and_receiving_a_new_reply_have_distinct_effects(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from wander_manager.manager import WanderManager
        manager = WanderManager.__new__(WanderManager)
        runner = Mock()
        manager._wander_creator = SimpleNamespace(_runtime_runner=runner)
        manager._mode_switch = Mock(is_wander_mode_active=False)
        manager._away_pending = False
        manager._activity_interrupt_snapshot = None
        manager._on_status_change = None
        manager.on_user_reply(timestamp=datetime(2026, 1, 2, 12), capture_away=False)
        runner.request_interrupt.assert_not_called()
        manager.on_user_reply()
        runner.request_interrupt.assert_called_once_with(source='user_message')
