"""Isolated mention-tool ai_brage: fake transport and in-memory action stores only."""
from types import SimpleNamespace
import json

import pytest

from behavior_scheduler.social_feed_tool import ManageSocialFeedTool
from social_feed.remote_sites import RemoteSite
from social_feed.remote_visit_store import RemoteSocialVisitStore
from wander_manager.social_circle_visit import SocialCircleVisit, _compact


class FakeRemoteClient:
    def __init__(self): self.calls = []
    async def request(self, _site, _identity, method, path, *, payload=None, params=None):
        self.calls.append((method, path, payload, params))
        if path.endswith('/me'):
            return {'actor': {'actor_id': 'visitor:k'}, 'can_manage_avatar': False,
                    'capabilities': ['mentions_v1', 'inbox_v1']}
        if path.endswith('/notifications') and method == 'GET':
            return {'items': [{'id':'notice-1','kind':'mention','moment_id':'moment-1',
                               'comment_id':'comment-1','moment_content':'原帖正文','comment_content':'评论正文'}]}
        return {'id': 'created'}


def remote_site():
    return RemoteSite('remote', '朋友家', 'https://social.example.test', '', 'key', True, 0, 0)


@pytest.mark.asyncio
async def test_remote_mentions_and_inbox_use_capability_and_explicit_ids_only():
    client = FakeRemoteClient()
    store = RemoteSocialVisitStore(remote_site(), client)
    assert await store.me() == 'visitor:k'
    await store.create_moment('k', '公开', visibility='public', source_key='post', mention_actor_ids=['visitor:friend'])
    await store.add_comment('moment-1', 'k', '评论', reply_to_id=None, source_key='comment', mention_actor_ids=[])
    notices = await store.unread_notifications('k', limit=10)
    await store.mark_notifications_read('k', ['notice-1'], read_source='fixture')
    assert notices[0]['comment_content'] == '评论正文'
    assert client.calls[1][2]['mention_actor_ids'] == ['visitor:friend']
    assert 'mention_actor_ids' not in client.calls[2][2]
    assert client.calls[-1] == ('POST', '/social/v1/notifications/read', {'notification_ids':['notice-1']}, None)


@pytest.mark.asyncio
async def test_remote_mentions_fail_explicitly_when_old_host_lacks_capability():
    client = FakeRemoteClient()
    store = RemoteSocialVisitStore(remote_site(), client)
    await store.me(); store.capabilities.discard('mentions_v1')
    with pytest.raises(ValueError, match='mentions_unavailable'):
        await store.create_moment('k', '公开', visibility='public', source_key='post', mention_actor_ids=['visitor:friend'])


class ActionStore:
    def __init__(self): self.payload = None
    async def create_moment(self, _actor, _content, **kwargs): self.payload = kwargs; return {'id':'new'}


@pytest.mark.asyncio
async def test_main_tool_forwards_only_stable_mention_ids():
    store = ActionStore()
    tool = ManageSocialFeedTool(store=store)
    result = await tool._direct({'content':'hello','visibility':'private','mention_actor_ids':['aning']}, 'post')
    assert result['status'] == 'success' and store.payload['mention_actor_ids'] == ['aning']
    invalid = await tool._direct({'content':'hello','mention_actor_ids':['@站主']}, 'post')
    assert invalid['status'] == 'invalid_mentions'


@pytest.mark.asyncio
async def test_wander_remote_inbox_exposes_reference_before_explicit_read():
    item = {'id':'moment-1','author':'visitor:friend','content':'原帖正文','visibility':'public','created_at':1,
            'comments':[{'id':'comment-1','author':'visitor:friend','content':'评论正文','created_at':2}],
            'reactions':[], 'people':{'visitor:friend':{'actor_id':'visitor:friend','name':'朋友'}}}
    class Inbox:
        has_inbox = True; capabilities = {'inbox_v1','mentions_v1'}; gifts=[]; gift_status='not_available'
        async def me(self): return 'visitor:k'
        async def unread_notifications(self, *_args, **_kwargs): return [{'id':'notice-1','kind':'mention','moment_id':'moment-1','comment_id':'comment-1','moment_content':'原帖正文','comment_content':'评论正文'}]
        async def get_moment(self, _id): return item
        async def mark_notifications_read(self, _recipient, ids, **_kwargs): self.read = ids; return {'marked':len(ids)}
    inbox = Inbox(); site = SimpleNamespace(id='remote', name='朋友家')
    async def context(_recipe, **kwargs): return SimpleNamespace(system_content=kwargs['wander_runtime_text'])
    async def decide(messages):
        assert '评论正文' in messages[0]['content']
        return {'content':json.dumps({'actions':[],'next':{'kind':'exit'},'reflection':'看到了'})}
    visit = SocialCircleVisit(decide, context_builder=context)
    visit._selected_store = lambda _request: (inbox, site)
    result = await visit.visit_step(request={'kind':'unread','site_id':'remote'}, previous_nodes=[], activity_reason='看看', run_id='r', activity_id='a', node_id='n', persona='AI', session_id='s')
    assert result['read_notification_ids'] == ['notice-1'] and inbox.read == ['notice-1']
    assert _compact(item)['mention_targets'][0]['actor_id'] == 'visitor:friend'


@pytest.mark.asyncio
async def test_wander_old_remote_uses_latest_only_when_inbox_capability_is_absent():
    class OldRemote:
        has_inbox = False
        async def list_moments(self, **_kwargs): return {'items':[{'id':'latest'}], 'next_cursor':''}
    visit = SocialCircleVisit(None)
    items, notices, page = await visit._read({'kind':'unread'}, OldRemote(), SimpleNamespace(id='old', name='旧家'))
    assert items == [{'id':'latest'}] and notices == [] and page['kind'] == 'latest'
