import asyncio
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import unittest.mock

from xhs_actions.direct_messages import DirectMessageStore, conversation_rows, dm_request, message_rows, record_visible_unread


class LeaseDevice:
    def __init__(self): self.device = self
    async def lease_async(self, *args, **kwargs): return nullcontext()


class FakePhone(LeaseDevice):
    def __init__(self):
        super().__init__(); self.sent = 0
    async def scan_direct(self, account, scope): return [{'thread':'p','name':'甲','unread':True}]
    async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
        return {'source':'s','thread':thread,'name':name,'incoming_text':'你好','account':account}
    async def prepare_direct_reply(self, account, source, text): return source
    async def submit_direct_reply(self): self.sent += 1
    async def verify_direct_reply(self, source, text): return True


class DirectMessageTests(unittest.TestCase):
    def approve_previewed(self, store, session, thread, profile_id=None, source=None):
        pending = next(row for row in store.pending(session) if row['thread'] == thread)
        preview = {'source': source or f'preview-{thread}', 'thread': profile_id or thread,
                   'inbox_thread': thread, 'name': '甲', 'incoming_text': '预览内容',
                   'account': {'account_id': 'sample-account'}, 'owner_preview': True}
        store.put_source(session, preview)
        store.freeze_preview(session, thread, pending['batch_token'], preview)
        return store.approve_batch(session, thread, pending['batch_token'])

    def test_unknown_batch_never_enters_model_queue_until_owner_approves(self):
        with TemporaryDirectory() as temp:
            store = DirectMessageStore(Path(temp) / 'dms.db')
            store.mark_pending('session', 'thread', '陌生人', 2)
            self.assertEqual(store.interactive_pending('session'), [])
            self.assertEqual(store.review_pending('session')[0]['name'], '陌生人')
            self.approve_previewed(store, 'session', 'thread')
            self.assertEqual(store.interactive_pending('session')[0]['unread_count'], 2)

    def test_owner_preview_freezes_exact_source_and_stale_token_cannot_approve(self):
        async def run():
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                phone = FakePhone()
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account'}):
                    await dm_request('session', 'scan', device=phone, store=store)
                    old_token = store.review_pending('session')[0]['batch_token']
                    preview = await dm_request('session', 'inspect', thread='p', batch_token=old_token, device=phone, store=store)
                    self.assertEqual(preview['status'], 'previewed')
                    # A later badge must never move the approval button to new material.
                    store.mark_pending('session', 'p', '甲', 1)
                    store.approve_batch('session', 'p', old_token)
                    read = await dm_request('session', 'read', thread='p', device=phone, store=store)
                    await dm_request('session', 'leave', source=read['source'], device=phone, store=store)
                    with self.assertRaisesRegex(ValueError, 'dm_batch_changed'):
                        store.approve_batch('session', 'p', old_token)
                    self.assertEqual(store.interactive_pending('session'), [])
                self.assertEqual(read['source'], preview['source'])
                self.assertEqual(read['recognition']['身份'], '仅本次准入，未确认人物绑定')
        asyncio.run(run())

    def test_contact_binding_and_blocking_are_stable_profile_operations(self):
        with TemporaryDirectory() as temp:
            store = DirectMessageStore(Path(temp) / 'dms.db')
            store.put_source('session', {'source':'verified-source', 'thread':'xhs-profile', 'name':'甲'})
            store.bind_contact('xhs-profile', entity_id='friend', name='甲')
            self.assertFalse(store.is_blocked('xhs-profile'))
            blocked = store.block_contact('xhs-profile', name='甲')
            self.assertEqual(blocked['block_sync'], 'pending')
            store.set_block_sync('xhs-profile', 'verified')
            self.assertTrue(store.is_blocked('xhs-profile'))

    def test_rejected_badge_stays_quiet_until_count_proves_a_later_batch(self):
        with TemporaryDirectory() as temp:
            store = DirectMessageStore(Path(temp) / 'dms.db')
            store.mark_pending('session', 'thread', '陌生人', 1)
            token = store.review_pending('session')[0]['batch_token']
            store.reject_batch('session', 'thread', token)
            store.mark_pending('session', 'thread', '陌生人', 1)
            # Count alone never proves this is the rejected message: it is
            # rechecked against the immutable source by the resolver.
            self.assertEqual(store.review_pending('session')[0]['unread_count'], 1)
            store.mark_pending('session', 'thread', '陌生人', 2)
            self.assertEqual(store.review_pending('session')[0]['unread_count'], 2)

    def test_verified_bound_profile_is_auto_admitted_after_native_read(self):
        async def run():
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                store.put_source('old', {'source':'old', 'thread':'p', 'name':'甲'})
                store.bind_contact('p', entity_id='friend', name='甲')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account'}):
                    result = await dm_request('session', 'scan', device=FakePhone(), store=store)
                self.assertEqual(result['items'][0]['thread'], 'p')
        asyncio.run(run())
    def test_send_targets_message_composer_even_with_second_send_button(self):
        from xhs_actions.direct_messages import DirectMessagesPhone
        phone=DirectMessagesPhone(device=LeaseDevice())
        phone._prepared_text='收到啦'
        def button(bounds):
            return {'class':'android.widget.TextView','text':'发送','enabled':'true','clickable':'true','box':bounds}
        intended=button([906,1368,1020,1464])
        phone.rows=[button([792,1207,936,1278]),intended,
                    {'class':'android.widget.EditText','text':'收到啦','focused':'true','box':[180,1356,786,1476]}]
        self.assertIs(phone.send_button(),intended)

    def test_conversation_parser_excludes_system_rows(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring('''<h><node class="androidx.recyclerview.widget.RecyclerView"><node class="android.view.ViewGroup" clickable="true" bounds="[0,0][100,100]"><node class="android.widget.ImageView" clickable="true" bounds="[0,10][20,30]"/><node class="android.widget.TextView" text="甲" bounds="[30,10][80,30]"/><node class="android.widget.TextView" text="预览" bounds="[30,40][80,60]"/></node><node class="android.widget.RelativeLayout" clickable="true" bounds="[0,100][100,200]"/></node></h>''')
        self.assertEqual(conversation_rows(root)[0]['name'], '甲')

    def test_visible_badge_records_same_provisional_pending_thread_as_inbox_scan(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring('''<h><node class="androidx.recyclerview.widget.RecyclerView"><node class="android.view.ViewGroup" clickable="true" bounds="[0,0][100,100]"><node class="android.widget.ImageView" clickable="true" bounds="[0,10][20,30]"/><node class="android.widget.TextView" text="甲" bounds="[30,10][80,30]"/><node class="android.widget.TextView" text="2" bounds="[0,0][15,10]"/></node></node></h>''')
        with TemporaryDirectory() as temp:
            store = DirectMessageStore(Path(temp) / 'dms.db')
            self.assertEqual(record_visible_unread(root, 'session', {'account_id':'sample-account'}, store=store), 1)
            item = store.pending('session')[0]
            self.assertEqual(item['unread_count'], 2)
            self.assertEqual(item['thread'], __import__('hashlib').sha256(__import__('json').dumps(['dm-inbox','sample-account','甲'], ensure_ascii=False).encode()).hexdigest())

    def test_message_parser_uses_direction_and_rejects_cards(self):
        import xml.etree.ElementTree as ET
        def message(side, body):
            avatar = '[0,0][50,50]' if side=='left' else '[900,0][950,50]'
            return (f'<node class="android.widget.LinearLayout"><node class="android.widget.TextView" text="今天 10:11"/>'
                    f'<node class="android.widget.FrameLayout"><node class="android.widget.RelativeLayout" bounds="{avatar}"/>'
                    + body + '</node></node>')
        root = ET.fromstring('<h><node class="androidx.recyclerview.widget.RecyclerView">'+
            message('left','<node class="android.widget.TextView" text="你好" bounds="[60,0][200,50]"/>')+
            message('right','<node class="android.widget.TextView" text="收到" bounds="[700,0][880,50]"/>')+
            message('left','<node class="android.widget.ImageView" bounds="[200,0][600,300]"/>')+'</node></h>')
        self.assertEqual([x['direction'] for x in message_rows(root)], ['incoming','outgoing','incoming'])
        self.assertEqual(message_rows(root)[-1]['text'],'')

    def test_claim_precedes_send_and_repeat_does_not_send_twice(self):
        async def run():
            phone = FakePhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                store.put_source('session', {'source':'s','thread':'p','name':'甲','incoming_text':'你好','account':{'account_id':'sample-account'},
                    'identity_binding': {'delivered_at':'now'}})
                import unittest.mock
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    first = await dm_request('session','reply',source='s',text='回你',device=phone,store=store)
                    second = await dm_request('session','reply',source='s',text='回你',device=phone,store=store)
                self.assertEqual(first['status'], 'published')
                self.assertEqual(second['status'], 'published')
                self.assertEqual(phone.sent, 1)
        asyncio.run(run())

    def test_unknown_submission_reconciles_without_a_second_send(self):
        class UnknownPhone(FakePhone):
            async def verify_direct_reply(self, source, text): return False
        async def run():
            phone = UnknownPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                store.put_source('session', {'source':'s','thread':'p','name':'甲','incoming_text':'你好','account':{'account_id':'sample-account'},
                    'identity_binding': {'delivered_at':'now'}})
                import unittest.mock
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    first = await dm_request('session','reply',source='s',text='回你',device=phone,store=store)
                    second = await dm_request('session','reply',source='s',text='回你',device=phone,store=store)
                    reconciled = await dm_request('session','reconcile',source='s',device=phone,store=store)
                self.assertEqual(first['status'], 'submitted_unverified')
                self.assertEqual(second['status'], 'submitted_unverified')
                self.assertEqual(reconciled['status'], 'submitted_unverified')
                self.assertEqual(phone.sent, 1)
        asyncio.run(run())

    def test_pending_conversation_survives_badge_clear_until_reply_or_leave(self):
        import unittest.mock
        class PendingPhone(FakePhone):
            def __init__(self):
                super().__init__()
                self.unread = True
            async def scan_direct(self, account, scope):
                return [{'thread':'p','name':'甲','unread':self.unread}]
        async def run():
            phone = PendingPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    first = await dm_request('session', 'scan', device=phone, store=store)
                    self.approve_previewed(store, 'session', 'p', source='s')
                    phone.unread = False
                    second = await dm_request('session', 'scan', device=phone, store=store)
                    incoming = await dm_request('session', 'read', thread='p', device=phone, store=store)
                    await dm_request('session', 'leave', source=incoming['source'], device=phone, store=store)
                    final = await dm_request('session', 'scan', device=phone, store=store)
                self.assertEqual(first['items'], [])
                self.assertEqual(second['items'][0]['name'], '甲')
                self.assertEqual(final['items'], [])
        asyncio.run(run())

    def test_read_material_is_frozen_and_later_badge_survives_old_leave(self):
        class BatchPhone(FakePhone):
            def __init__(self):
                super().__init__()
                self.unread_count = 2
                self.reads = 0
            async def scan_direct(self, account, scope):
                return [{'thread':'p','name':'甲','unread':self.unread_count > 0,
                         'unread_count':self.unread_count}]
            async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
                self.reads += 1
                return {'source':'old','thread':thread,'name':name,'incoming_text':'第二句',
                        'incoming_messages':[{'text':'第一句','clock':'10:00'},{'text':'第二句','clock':'10:01'}],
                        'unread_count':unread_count,'unread_remaining':0,'truncated':False,'account':account}
        async def run():
            phone = BatchPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    await dm_request('session', 'scan', device=phone, store=store)
                    self.approve_previewed(store, 'session', 'p', source='old')
                    first = await dm_request('session', 'read', thread='p', device=phone, store=store)
                    # Red dot has cleared after opening, then one new message arrives
                    # while AI is still deciding how to handle the frozen old batch.
                    phone.unread_count = 1
                    await dm_request('session', 'scan', device=phone, store=store)
                    again = await dm_request('session', 'read', thread='p', device=phone, store=store)
                    await dm_request('session', 'leave', source='old', device=phone, store=store)
                    # A late replay of the old completion may arrive after the
                    # later red badge has already become its own pending batch.
                    await dm_request('session', 'leave', source='old', device=phone, store=store)
                    after = await dm_request('session', 'scan', device=phone, store=store)
                self.assertEqual([row['text'] for row in first['messages']], ['第一句','第二句'])
                self.assertEqual(again['source'], 'old')
                self.assertEqual(phone.reads, 2)
                self.assertEqual(after['items'], [])
                self.assertEqual(store.review_pending('session')[0]['unread_count'], 1)
        asyncio.run(run())

    def test_reply_refuses_when_newest_inbound_batch_changed(self):
        from xhs_actions.direct_messages import DirectMessagesPhone, InteractionBlocked
        class ChangedPhone(DirectMessagesPhone):
            async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
                return {'source':'new','thread':thread,'name':name,'incoming_text':'后来又发了一句','account':account}
        async def run():
            phone = ChangedPhone(device=LeaseDevice())
            with self.assertRaisesRegex(InteractionBlocked, 'dm_latest_inbound_changed'):
                await phone.prepare_direct_reply({'account_id':'sample-account'}, {'source':'old','thread':'p','name':'甲',
                    'incoming_text':'原话','account':{'account_id':'sample-account'}}, '回复')
        asyncio.run(run())

    def test_reply_rechecks_the_same_observed_batch_size(self):
        from xhs_actions.direct_messages import DirectMessagesPhone, InteractionBlocked
        class StablePhone(DirectMessagesPhone):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.seen_count = 0; self.rows = []
            async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
                self.seen_count = unread_count
                return {'source':'batch','thread':thread,'name':name,'incoming_text':'第二句','account':account}
            async def _return_to_tail(self, expected):
                return
        async def run():
            phone = StablePhone(device=LeaseDevice())
            with self.assertRaisesRegex(InteractionBlocked, 'dm_editor_unverified'):
                await phone.prepare_direct_reply({'account_id':'sample-account'}, {'source':'batch','thread':'p','name':'甲',
                    'incoming_text':'第二句','unread_count':2,'account':{'account_id':'sample-account'}}, '回复')
            self.assertEqual(phone.seen_count, 2)
        asyncio.run(run())

    def test_truncated_batch_stays_pending_after_read_without_reply(self):
        class TruncatedPhone(FakePhone):
            def __init__(self):
                super().__init__(); self.unread = True
            async def scan_direct(self, account, scope):
                return [{'thread':'p','name':'甲','unread':self.unread,'unread_count':3 if self.unread else 0}]
            async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
                return {'source':'cut','thread':thread,'name':name,'incoming_text':'屏幕最后一条',
                        'incoming_messages':[{'text':'屏幕最后一条','clock':'10:02'}],
                        'unread_count':unread_count,'unread_remaining':2,'truncated':True,'account':account}
        async def run():
            phone = TruncatedPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    await dm_request('session', 'scan', device=phone, store=store)
                    self.approve_previewed(store, 'session', 'p', source='cut')
                    incoming = await dm_request('session', 'read', thread='p', device=phone, store=store)
                    await dm_request('session', 'leave', source='cut', device=phone, store=store)
                    phone.unread = False
                    pending = await dm_request('session', 'scan', device=phone, store=store)
                self.assertTrue(incoming['truncated'])
                self.assertEqual(incoming['unread_remaining'], 2)
                self.assertEqual(pending['items'], [])
                self.assertEqual(store.review_pending('session')[0]['unread_count'], 2)
        asyncio.run(run())

    def test_provisional_inbox_thread_completes_after_profile_identity_is_verified(self):
        class RealIdentityPhone(FakePhone):
            def __init__(self):
                super().__init__(); self.unread = True
            async def scan_direct(self, account, scope):
                return [{'thread':'inbox-hash','name':'甲','unread':self.unread,
                         'unread_count':1 if self.unread else 0}]
            async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
                self.asserted_thread = thread
                return {'source':'real-source','thread':'profile-id','name':name,'incoming_text':'你好',
                        'incoming_messages':[{'text':'你好','clock':'10:00'}], 'unread_count':1,
                        'unread_remaining':0,'truncated':False,'account':account}
        async def run():
            phone = RealIdentityPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    await dm_request('session', 'scan', device=phone, store=store)
                    self.approve_previewed(store, 'session', 'inbox-hash', 'profile-id', 'real-source')
                    source = await dm_request('session', 'read', thread='inbox-hash', device=phone, store=store)
                    phone.unread = False
                    await dm_request('session', 'leave', source=source['source'], device=phone, store=store)
                    result = await dm_request('session', 'scan', device=phone, store=store)
                self.assertEqual(result['items'], [])
        asyncio.run(run())

    def test_history_scan_keeps_ordinary_threads_out_of_pending_inbox(self):
        class HistoryPhone(FakePhone):
            async def scan_direct(self, account, scope):
                self.scope = scope
                return [{'thread':'old','name':'旧联系人','unread':False,'unread_count':0}]
        async def run():
            phone = HistoryPhone()
            with TemporaryDirectory() as temp:
                store = DirectMessageStore(Path(temp) / 'dms.db')
                with unittest.mock.patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'sample-account','account_name':'AI'}):
                    history = await dm_request('session', 'scan', scope='history', device=phone, store=store)
                    fresh = await dm_request('session', 'scan', device=phone, store=store)
                self.assertEqual(phone.scope, 'new')
                self.assertEqual(history['items'], [])
                self.assertEqual(fresh['items'], [])
        asyncio.run(run())

class PublicIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_uses_admitted_material_and_requires_a_verified_reply(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from wander_manager.event_types import EventType
        from wander_manager.node_execution_adapter import NodeExecutionStatus
        from xhs_actions.wander import social_node

        context = SimpleNamespace(session_id='test-session', persona='test-persona')
        incoming = {'source':'test-source', 'thread':'peer', 'recognition':'示例联系人',
                    'text':'你好', 'name':'示例联系人'}
        for action, reply_state in [('leave', 'left'), ('reply', 'published'), ('reply', 'submitted_unverified')]:
            with self.subTest(action=action, status=reply_state):
                request = AsyncMock(side_effect=[{'items':[{'thread':'peer'}]}, incoming, {'status':reply_state}])
                decision = SimpleNamespace(status='ok', parsed={'action':action,'text':'你好','summary':'互相问好'})
                with (patch('xhs_interactions.native.available', return_value=True),
                      patch('xhs_interactions.native.config', return_value={}),
                      patch('xhs_actions.dm_tool.dm_request', request),
                      patch('wander_manager.xhs_task_context.task_messages', AsyncMock(return_value=[])),
                      patch('wander_manager.flash_structured.call_flash_json_detailed', AsyncMock(return_value=decision))):
                    if reply_state == 'submitted_unverified':
                        with self.assertRaisesRegex(ValueError, 'dm_reply_unverified'):
                            await social_node(EventType.XHS_DIRECT_MESSAGES, None, {}, 'node', context)
                    else:
                        result = await social_node(EventType.XHS_DIRECT_MESSAGES, None, {}, 'node', context)
                        self.assertEqual(result.status, NodeExecutionStatus.SUCCEEDED)
                        self.assertFalse(result.retry_safe)
                self.assertEqual([c.args[1] for c in request.await_args_list], ['scan','read',action])
                self.assertEqual(request.await_args.kwargs['source'], 'test-source')

    async def test_account_switch_hides_frozen_material(self):
        from unittest.mock import patch
        phone = FakePhone()
        with TemporaryDirectory() as temp:
            store = DirectMessageStore(Path(temp) / 'messages.db')
            with patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'first'}):
                await dm_request('session','scan',device=phone,store=store)
                token = store.review_pending('session')[0]['batch_token']
                await dm_request('session','inspect',thread='p',batch_token=token,device=phone,store=store)
                store.approve_batch('session','p',token)
            with patch('xhs_actions.direct_messages.account_config', return_value={'account_id':'second'}):
                self.assertFalse((await dm_request('session','scan',device=phone,store=store))['items'])
                with self.assertRaisesRegex(ValueError, 'dm_account_changed'):
                    await dm_request('session','read',thread='p',device=phone,store=store)
            self.assertEqual(phone.sent, 0)

    async def test_owner_management_route_requires_registered_source(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from unittest.mock import Mock, patch
        from xhs_actions.dm_admin import router
        app = FastAPI()
        app.include_router(router)
        with patch('xhs_actions.dm_admin.DirectMessageStore', return_value=Mock(admin_pending=lambda:[], contacts=lambda:[])):
            async with AsyncClient(transport=ASGITransport(app=app, client=('127.0.0.1',1)),base_url='http://localhost') as client:
                url = '/api/xhs-direct-messages/inbox'
                self.assertEqual((await client.get(url)).status_code, 403)
                headers = {'x-mirrow-xhs-admin':'1','origin':'https://example.invalid'}
                self.assertEqual((await client.get(url, headers=headers)).status_code, 403)
                headers['origin'] = 'http://localhost'
                self.assertEqual((await client.get(url, headers=headers)).json(), {'review':[], 'contacts':[]})
