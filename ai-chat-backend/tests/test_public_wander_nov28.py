"""Offline regression checks for the November public Wander update."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from wander_manager.bookshelf_actions import observed_shelf_sources
from wander_manager.runtime_models import ActivityState, NodeState, RunState, WanderActivity, WanderNode, WanderRun
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.xhs_request import validate_task


class PublicWanderNovemberTests(TestCase):
    def test_runtime_status_exposes_active_activity_and_public_shelf_only(self):
        with TemporaryDirectory() as tmp:
            store = WanderRuntimeStore(Path(tmp) / 'wander.db')
            store.initialize()
            run = WanderRun(session_id='sample', state=RunState.ACTIVE_WAIT)
            store.save_run(run)
            activity = WanderActivity(
                'browse_bookmarks', run.run_id, state=ActivityState.ACTIVE_WAIT,
                next_node_at='2026-11-28T12:00:00', timer_ends_at='2026-11-28T12:30:00')
            store.save_activity(activity)
            store.save_node(WanderNode(
                activity.activity_id, 1, state=NodeState.COMPLETED, execution_status='succeeded',
                source_payload={'bookshelf_snapshot': {'items': [
                    {'source': 'essays'}, {'source': 'bookmarks'}, {'source': 'gallery'}]}}))
            status = store.get_runtime_status(session_id='sample')
        self.assertEqual(status['current_activity_id'], activity.activity_id)
        self.assertEqual(status['current_activity_state'], 'active_wait')
        self.assertEqual(status['timer_ends_at'], '2026-11-28T12:30:00')
        self.assertEqual(status['shelf_sources'], ['essays', 'bookmarks'])

    def test_profile_target_is_generic_or_configured_self_only(self):
        self.assertEqual(validate_task({'operation': 'profile', 'query': '公开账号', 'count': 1})['query'], '公开账号')
        self.assertEqual(validate_task({'operation': 'profile', 'query': '', 'count': 1, 'profile_target': 'self'})['profile_target'], 'self')
        with self.assertRaises(ValueError):
            validate_task({'operation': 'profile', 'query': '', 'count': 1, 'profile_target': 'unsupported_target'})

    def test_shelf_source_projection_never_reveals_nonpublic_kinds(self):
        self.assertEqual(observed_shelf_sources({'bookshelf_snapshot': {'items': [
            {'source': 'gallery'}, {'source': 'theater'}]}}), [])

    def test_choice_correction_is_bounded_by_the_natural_node(self):
        import asyncio
        from wander_manager.node_retry import choose, current, window
        calls = []

        async def call(prompt):
            calls.append(prompt)
            return '{"value": 1}' if len(calls) == 1 else '{"value": 2}'

        with window(lambda: False):
            retry = current()
            result = asyncio.run(choose(call, 'choose', lambda value: __import__('json').loads(value),
                                        lambda value: '' if value['value'] == 2 else 'choose again', 'test'))
        self.assertEqual(retry.receipt['first_reason'], 'choose again')
        self.assertEqual(result['value'], 2)
        self.assertEqual(len(calls), 2)

    def test_direct_message_event_has_a_public_runtime_contract(self):
        from wander_manager.capability_catalog import get_capability_records
        from wander_manager.event_catalog import EVENT_CATALOG
        from wander_manager.event_types import EventType
        from wander_manager.runtime_models import GoalMode
        strategy = EVENT_CATALOG[EventType.XHS_DIRECT_MESSAGES]
        self.assertEqual(strategy.default_goal_mode, GoalMode.SINGLE)
        self.assertEqual(strategy.default_goal_value, 1)
        self.assertTrue(any(record['capability'] == '自主查看个人私信'
                            for record in get_capability_records()))

    def test_gallery_images_use_managed_route_without_text_fallback(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from wander_manager.flash_structured import FlashJsonResult, call_flash_json_detailed
        managed = AsyncMock(return_value=FlashJsonResult(status='ok', parsed={'ok': True}))
        with patch('api_sources.managed.call_managed', managed):
            result = asyncio.run(call_flash_json_detailed(
                [{'role': 'user', 'content': 'read'}], route_label='wander_node_review',
                gallery_images=['data:image/png;base64,AA==']))
        self.assertEqual(result.status, 'ok')
        self.assertEqual(managed.call_args.kwargs['image_batch'], ['data:image/png;base64,AA=='])

    def test_gallery_passes_once_to_review_and_persists_only_page_observations(self):
        import asyncio
        from unittest.mock import AsyncMock
        from wander_manager.flash_structured import FlashJsonResult
        from wander_manager.runtime_decision_adapters import DecisionContext, NodeReviewAdapter
        from wander_manager.xiaohongshu_handler import BrowseXiaohongshuHandler

        device = AsyncMock()
        device.fetch_task.return_value = {
            'status':'success','source_id':'sample','title':'示例图集','gallery_capture':{'declared_total':2,'captured_pages':[1,2]},
            'frames':[{'label':'first','page':1,'screenshot_base64':'AA=='},
                      {'label':'second','page':2,'screenshot_base64':'AQ=='}]}
        pages = []
        handler = BrowseXiaohongshuHandler(device_source=device)
        payload = asyncio.run(handler.fetch_one_post(task={'operation':'recommend','count':1}, frames_out=pages))
        self.assertEqual(payload['coverage']['image_analysis'], 'pending')
        self.assertEqual([image for _, image in pages], ['data:image/png;base64,AA==','data:image/png;base64,AQ=='])
        self.assertNotIn('frames', payload)
        with TemporaryDirectory() as temp:
            store = WanderRuntimeStore(Path(temp)/'wander.db')
            store.initialize()
            run = WanderRun(session_id='sample',state=RunState.ACTIVE_WAIT)
            store.save_run(run)
            activity = WanderActivity('browse_xiaohongshu', run.run_id)
            store.save_activity(activity)
            node = WanderNode(activity.activity_id, 1, state=NodeState.RUNNING, execution_status='succeeded',source_payload=payload)
            store.save_node(node)
            llm = AsyncMock(return_value=FlashJsonResult(status='ok', parsed={
                'continue_activity':False, 'reflection':'看完了。', 'image_observations':['第一页','第二页']}))
            reviewer = NodeReviewAdapter(store, llm_caller=llm)
            reviewer._messages = AsyncMock(return_value=[{'role':'system','content':'test context'}])
            context = DecisionContext('sample','sample')
            self.assertIsNone(asyncio.run(reviewer.review(node.node_id, context, gallery_pages=pages[:1])))
            llm.assert_not_awaited()
            asyncio.run(reviewer.review(node.node_id, context, gallery_pages=pages))
            self.assertEqual(llm.await_count, 1)
            self.assertEqual(llm.await_args.kwargs['gallery_images'],[image for _,image in pages])
            saved = store.get_node(node.node_id)['source_payload']
            self.assertEqual([item['observation'] for item in saved['image_observations']], ['第一页','第二页'])
            self.assertNotIn('base64',str(saved))

    def test_browse_adapter_resolves_public_readers_without_device(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from wander_manager.xhs_readable_post import open_readable_post
        from wander_manager.xhs_feed_navigation import enter_recommend
        from wander_manager.xiaohongshu_adb import AdbXiaohongshuSource
        from xhs_actions.navigation_vision import select_read_navigation
        self.assertTrue(callable(open_readable_post) and callable(enter_recommend))
        device = AdbXiaohongshuSource()
        with patch('xhs_actions.favorites.fetch_saved_post', AsyncMock(return_value={'status':'no_new_feed_cards'})) as read:
            self.assertEqual(asyncio.run(device.fetch_task(task={'operation':'favorites','query':'示例'}))['status'], 'no_new_feed_cards')
            read.assert_awaited_once_with(device,'示例',set())
        choose = AsyncMock(return_value={'content':'{"candidate_id":"1"}'})
        entry = {'text':'返回','box':[0,0,100,100]}
        self.assertEqual(asyncio.run(select_read_navigation([entry],b'sample',vision=choose))['label'],'返回')
        choose.reset_mock()
        self.assertIsNone(asyncio.run(select_read_navigation([entry,{'text':'请输入验证码'}],b'sample',vision=choose)))
        choose.assert_not_awaited()

    def test_gallery_reaches_the_provider_wire_in_order(self):
        import asyncio
        import json
        from unittest.mock import patch
        import httpx
        from api_sources.runner import run_structured
        pages = ['data:image/png;base64,AA==','data:image/png;base64,AQ==']
        source = {'station':{'id':'example','name':'fixture','base_url':'https://example.invalid/v1','protocol':'openai-chat'},
                  'model':{'id':'example','model':'fixture','label':'fixture','profile':'generic','json_mode':'json_object',
                           'thinking':'off','system_role':'system','sampling':True,'enabled':True},
                  'ordinal':1,'api_key':'fixture'}
        dispatches = []
        def respond(request):
            wire = json.loads(request.content)
            dispatches.append([part['image_url']['url'] for part in wire['messages'][-1]['content']])
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"reflection":"已读"}'},'finish_reason':'stop'}]})
        async def run(directory):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with patch('llm_client.http_client',client):
                    return await run_structured([{'role':'user','content':'read'}], route='wander_node_review',
                        temperature=0,max_tokens=300,enable_thinking=False,sources=[source],image_batch=pages,
                        audit_path=Path(directory)/'audit.db')
        with TemporaryDirectory() as directory:
            result = asyncio.run(run(directory))
            self.assertEqual(result.status, 'ok', result.error)
            self.assertNotIn('data:image', str(result.request_messages))
        self.assertEqual(dispatches,[pages])
