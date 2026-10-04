"""The common-circle model sees bounded pages; actions stay in the source DB."""

import json
from types import SimpleNamespace

import pytest

from social_feed.hybrid_store import HybridSocialFeedStore
from social_feed.public_wall import PublicWall
from social_feed.store import SocialFeedStore
from wander_manager.social_circle_visit import SocialCircleVisit, activity_summary


@pytest.fixture
def feed(tmp_path):
    return HybridSocialFeedStore(
        SocialFeedStore(tmp_path / 'private.db'), PublicWall(tmp_path / 'public.db'),
    )


async def _context_builder(_recipe, **kwargs):
    return SimpleNamespace(system_content=kwargs['wander_runtime_text'] + '\n最小人格连续性')


@pytest.mark.asyncio
@pytest.mark.parametrize('dynamic_last', ['0', '1'])
async def test_real_builder_keeps_runtime_history_and_persona_after_zone_split(monkeypatch, dynamic_last):
    from context_builder.builder import ContextBuilder
    monkeypatch.setenv('MIRROW_CONTEXT_DYNAMIC_LAST', dynamic_last)

    async def ingredient(builder, section):
        if section.ingredient == 'wander_runtime':
            return builder._kwargs['wander_runtime_text']
        if section.ingredient == 'persona':
            return builder._kwargs['persona']
        if section.ingredient == 'wander_day_history':
            return [{'role': 'assistant', 'content': '先前真实的发言'}]
        return ''

    monkeypatch.setattr(ContextBuilder, '_call_ingredient', ingredient)
    monkeypatch.setattr('prompt_ledger.register_prompt', lambda *a, **k: 'fixture')
    monkeypatch.setattr('context_builder.inspection.record_builder_inspection', lambda *a, **k: None)

    async def build(recipe, **kwargs):
        return await ContextBuilder.build(recipe, **kwargs, period_active=False, affect_context_mode='disabled')

    visit = SocialCircleVisit(None, context_builder=build)
    monkeypatch.setattr(visit, '_site_options', lambda: [])
    messages = await visit._messages(persona='AI 的稳定人格', session_id='fixture',
        activity_reason='想看看共域', page={'kind': 'latest'},
        items=[], notices=[], previous_nodes=[], last_node=False)
    combined = '\n'.join(m['content'] for m in messages)
    assert 'AI 的稳定人格' in combined
    assert '先前真实的发言' in combined
    assert combined.count('[共友圈本次活动]') == 1
    assert combined.index('先前真实的发言') < combined.index('[共友圈本次活动]')
    assert messages[-1]['role'] == 'user' and '只输出 JSON' in messages[-1]['content']


@pytest.mark.asyncio
async def test_full_assembly_still_rejects_absent_runtime(monkeypatch):
    async def missing_runtime(_recipe, **kwargs):
        return SimpleNamespace(system_content=kwargs['persona'],
            dynamic_content='当前时间', tail_messages=[], context_messages=[],
            formatted_messages=[{'role': 'system', 'content': kwargs['persona']}])
    visit = SocialCircleVisit(None, context_builder=missing_runtime)
    monkeypatch.setattr(visit, '_site_options', lambda: [])
    with pytest.raises(ValueError, match='social_context_missing_runtime'):
        await visit._messages(persona='AI', session_id='fixture', activity_reason='',
            page={'kind': 'latest'}, items=[], notices=[], previous_nodes=[], last_node=False)


@pytest.mark.asyncio
async def test_home_delivery_observation_is_bounded_and_available_to_both_triggers(feed):
    from social_feed.decor_store import _store_at
    from social_feed.test_decor import make_home
    await feed.initialize()
    feed.public.register_human_name('h','测试朋友')
    decor=_store_at(feed.public.path.with_name('social_decor.db'))
    decor.set_gift('human',make_home(decor),'home-human')
    decor.visit('visitor:h','human','测试朋友')
    captured=[]
    async def decide(messages):
        captured.append(messages[0]['content'])
        return {'content':json.dumps({'actions':[],'next':{'kind':'exit'},'reflection':'看了收据'})}
    visit=SocialCircleVisit(decide,store=feed,context_builder=_context_builder)
    kwargs=dict(request=None,previous_nodes=[],activity_reason='站主邀请',run_id='r',activity_id='a',node_id='n',persona='AI',session_id='s')
    result=await visit.visit_step(**kwargs)
    assert result['scope']['gift_deliveries']['total']==1 and '测试朋友' in captured[-1]
    previous=result['scope']['gift_deliveries']['observed_at']
    visit.home_observation=lambda:previous
    again=await visit.visit_step(**{**kwargs,'activity_reason':'漫想','node_id':'n2'})
    assert again['scope']['gift_deliveries']['new_count']==0


@pytest.mark.asyncio
async def test_unread_batch_is_idempotent_and_summary_has_grounded_reply(feed):
    moment = await feed.create_moment('aning', '今天新来的小伙伴')
    assert len(await feed.unread_notifications('k')) == 1

    async def decide(messages):
        assert moment['id'] in messages[0]['content']
        return {'content': json.dumps({
            'actions': [
                {'action': 'comment', 'moment_id': moment['id'], 'content': '欢迎认识你'},
                {'action': 'like', 'moment_id': moment['id']},
            ],
            'next': {'kind': 'exit'}, 'reflection': '认识新朋友挺开心', 'share': True,
        }, ensure_ascii=False)}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    kwargs = dict(request=None, previous_nodes=[], activity_reason='站主邀请',
                  run_id='r', activity_id='a', node_id='n', persona='AI 的人格',
                  session_id='session')
    first = await visit.visit_step(**kwargs)
    second = await visit.visit_step(**{**kwargs, 'request': {'kind': 'latest'}})
    assert first['status'] == 'success' and first['exit'] and first['share_intent']
    assert len(first['action_results']) == 2
    assert len((await feed.get_moment(moment['id']))['comments']) == 1
    assert len((await feed.get_moment(moment['id']))['reactions']) == 1
    assert len(second['action_results']) == 2  # Same node keys replay, not duplicate.
    assert await feed.unread_notifications('k') == []
    summary = await activity_summary(feed, [{'source_payload': first}])
    assert '欢迎认识你' in summary and '认识新朋友挺开心' in summary
    assert moment['id'] not in summary
    assert first['action_results'][0]['comment_id'] not in summary


@pytest.mark.asyncio
async def test_latest_and_search_are_bounded_and_targets_must_be_visible(feed):
    older = await feed.create_moment('aning', '遥远的海', created_at=100)
    newest = await feed.create_moment('k', '今天的太阳', created_at=200)
    public = await feed.create_moment('k', '公开的星星', visibility='public', created_at=300)
    assert [row['id'] for row in await feed.search_moments('星星')] == [public['id']]
    seen = []

    async def decide(messages):
        seen.append(messages[0]['content'])
        return {'content': json.dumps({
            'actions': [{'action': 'comment', 'moment_id': older['id'], 'content': '我看到了'}],
            'next': {'kind': 'exit'}, 'reflection': '读完了', 'share': False,
        }, ensure_ascii=False)}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    result = await visit.visit_step(
        request={'kind': 'search', 'query': '太阳'}, previous_nodes=[], activity_reason='看看',
        run_id='r', activity_id='a', node_id='n', persona='AI 的人格', session_id='session',
    )
    assert newest['id'] in seen[0] and older['id'] not in seen[0]
    assert result['action_results'][0]['status'] == 'invalid_target'
    assert (await feed.get_moment(older['id']))['comments'] == []
    latest, _, page = await visit._read({'kind': 'latest'})
    assert len(latest) == 3 and page['kind'] == 'latest'


@pytest.mark.asyncio
async def test_visibility_change_uses_visible_k_post_id_and_keeps_other_posts_untouched(feed):
    own = await feed.create_moment('k', '一条准备收起来的动态', visibility='public')
    other = await feed.create_moment('aning', '站主的动态', visibility='private')

    async def decide(messages):
        assert own['id'] in messages[0]['content']
        return {'content': json.dumps({
            'actions': [
                {'action': 'set_visibility', 'moment_id': own['id'], 'visibility': 'private'},
                {'action': 'set_visibility', 'moment_id': other['id'], 'visibility': 'public'},
                {'action': 'set_visibility', 'moment_id': 'invented-id', 'visibility': 'private'},
            ], 'next': {'kind': 'exit'}, 'reflection': '想先只给站主看',
        }, ensure_ascii=False)}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    result = await visit.visit_step(request={'kind': 'latest'}, previous_nodes=[],
                                    activity_reason='把这条改私密', run_id='visibility',
                                    activity_id='a', node_id='n', persona='AI', session_id='s')
    assert [row['status'] for row in result['action_results']] == [
        'success', 'forbidden_target', 'invalid_target',
    ]
    assert (await feed.get_moment(own['id']))['visibility'] == 'private'
    assert (await feed.get_moment(other['id']))['visibility'] == 'private'
    summary = await activity_summary(feed, [{'source_payload': result}])
    assert '改为私密' in summary and own['id'] not in summary


@pytest.mark.asyncio
async def test_missing_persona_is_not_silently_replaced(feed):
    async def decide(_messages):
        raise AssertionError('model must not be called')
    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    with pytest.raises(ValueError, match='social_context_unavailable'):
        await visit.visit_step(
            request=None, previous_nodes=[], activity_reason='', run_id='r',
            activity_id='a', node_id='n', persona='', session_id='session',
        )


@pytest.mark.asyncio
async def test_unread_is_paged_without_loading_the_whole_inbox(feed):
    for index in range(12):
        await feed.create_moment('aning', f'第 {index} 条', created_at=100 + index)

    async def decide(_messages):
        return {'content': json.dumps({'actions': [], 'next': {'kind': 'unread'},
                                       'reflection': '继续看', 'share': False})}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    first = await visit.visit_step(request=None, previous_nodes=[], activity_reason='',
                                   run_id='r', activity_id='a', node_id='n1',
                                   persona='AI', session_id='session')
    assert len(first['read_notification_ids']) == 10
    assert first['next_request'] == {'kind': 'unread'}
    second = await visit.visit_step(request=first['next_request'], previous_nodes=[{'node_id': 'n1', 'source_payload': first}],
                                    activity_reason='', run_id='r', activity_id='a', node_id='n2',
                                    persona='AI', session_id='session')
    assert len(second['read_notification_ids']) == 2
    assert second['exit']


@pytest.mark.asyncio
async def test_k_can_rename_own_circle_profile_without_changing_avatar(feed, monkeypatch):
    monkeypatch.setattr('social_feed.public_wall._WALL', feed.public)
    feed.public.initialize()
    feed.public.set_profile('k', avatar='https://example.test/k.png')

    async def decide(_messages):
        return {'content': json.dumps({'actions': [{'action': 'nickname', 'nickname': '新圈名'}],
                                       'next': {'kind': 'exit'}, 'reflection': '换了新称呼', 'share': False},
                                      ensure_ascii=False)}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    result = await visit.visit_step(request={'kind': 'latest'}, previous_nodes=[], activity_reason='试试新名字',
                                    run_id='rename', activity_id='a', node_id='n', persona='AI', session_id='s')
    assert result['action_results'][0] == {'action': 'nickname', 'status': 'success', 'nickname': '新圈名'}
    assert feed.public.profile('k')['avatar'] == 'https://example.test/k.png'
    assert '新圈名' in await activity_summary(feed, [{'source_payload': result}])


@pytest.mark.asyncio
async def test_switch_site_keeps_actions_in_one_wall_per_node(feed, monkeypatch):
    home = await feed.create_moment('aning', '本家的动态')
    remote = {'id': 'remote-post', 'author': 'visitor:friend', 'content': '人甲 家的动态',
              'comments': [], 'reactions': [], 'people': {'visitor:friend': {'name': '人甲'}},
              'created_at': 100}
    writes = []

    class Remote:
        async def me(self): return 'visitor:k'
        async def list_moments(self, **_kwargs): return {'items': [remote], 'next_cursor': ''}
        async def add_comment(self, moment_id, _actor, content, **_kwargs):
            writes.append((moment_id, content))
            return {'id': 'remote-comment'}

    site = SimpleNamespace(id='person_a-site', name='人甲 家')
    decisions = iter([
        {'actions': [], 'next': {'kind': 'switch', 'site_id': site.id}, 'reflection': '去 人甲 家看看'},
        {'actions': [{'action': 'comment', 'moment_id': remote['id'], 'content': '看到啦'}],
         'next': {'kind': 'exit'}, 'reflection': '聊得开心'},
    ])

    async def decide(_messages):
        return {'content': json.dumps(next(decisions), ensure_ascii=False)}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    monkeypatch.setattr(visit, '_site_options', lambda: [{'id': site.id, 'name': site.name}])
    monkeypatch.setattr(visit, '_selected_store', lambda request: (Remote(), site) if request.get('site_id') else (feed, None))
    first = await visit.visit_step(request={'kind': 'latest'}, previous_nodes=[], activity_reason='看看',
                                   run_id='r', activity_id='a', node_id='n1', persona='AI', session_id='s')
    assert first['next_request'] == {'kind': 'latest', 'site_id': site.id}
    assert writes == []
    second = await visit.visit_step(request=first['next_request'], previous_nodes=[{'node_id': 'n1', 'source_payload': first}],
                                    activity_reason='看看', run_id='r', activity_id='a', node_id='n2', persona='AI', session_id='s')
    assert writes == [('remote-post', '看到啦')]
    assert (await feed.get_moment(home['id']))['comments'] == []
    assert '人甲 家' in await activity_summary(feed, [{'source_payload': second}])


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,expected', [(None, {'kind':'all'}), ('all', {'kind':'all'}),
                                          ('home', {'kind':'latest','site_id':''})])
async def test_latest_ten_scope_defaults_to_all_without_changing_first_unread(feed, monkeypatch, scope, expected):
    choice={'kind':'latest'}
    if scope is not None:
        choice['read_scope']=scope
    async def decide(_messages):
        return {'content':json.dumps({'actions':[], 'next':choice, 'reflection':''})}
    visit=SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    monkeypatch.setattr(visit,'_site_options',lambda:[])
    result=await visit.visit_step(request=None,previous_nodes=[],activity_reason='',run_id='scope',
        activity_id='a',node_id='n',persona='AI',session_id='s')
    assert result['scope']['kind']=='unread'
    assert result['next_request']==expected


@pytest.mark.asyncio
async def test_switch_then_remote_public_post_does_not_write_home(feed, monkeypatch):
    writes=[]
    class Remote:
        async def me(self): return 'visitor:k'
        async def list_moments(self, **kwargs):
            assert kwargs['limit']==10
            return {'items':[], 'next_cursor':''}
        async def create_moment(self, actor, content, **kwargs):
            writes.append((actor, content, kwargs['visibility']))
            return {'id':'remote-created'}
    site=SimpleNamespace(id='person_a-site',name='人甲 家')
    decisions=iter([{'actions':[], 'next':{'kind':'switch','site_id':site.id}},
        {'actions':[{'action':'post','content':'来这里坐坐','visibility':'public'}], 'next':{'kind':'exit'}}])
    async def decide(_messages): return {'content':json.dumps(next(decisions))}
    visit=SocialCircleVisit(decide,store=feed,context_builder=_context_builder)
    monkeypatch.setattr(visit,'_site_options',lambda:[{'id':site.id,'name':site.name}])
    monkeypatch.setattr(visit,'_selected_store',lambda request:(Remote(),site) if request.get('site_id') else (feed,None))
    first=await visit.visit_step(request=None,previous_nodes=[],activity_reason='',run_id='remote',activity_id='a',node_id='n1',persona='AI',session_id='s')
    second=await visit.visit_step(request=first['next_request'],previous_nodes=[{'node_id':'n1','source_payload':first}],activity_reason='',run_id='remote',activity_id='a',node_id='n2',persona='AI',session_id='s')
    assert writes==[('k','来这里坐坐','public')]
    assert second['action_results'][0]['site_id']==site.id
    assert (await feed.list_moments())['items']==[]


@pytest.mark.asyncio
async def test_remote_private_post_is_rejected_before_network(feed, monkeypatch):
    class Remote:
        async def me(self): return 'visitor:k'
        async def list_moments(self, **_kwargs): return {'items': [], 'next_cursor': ''}
        async def create_moment(self, *_args, **_kwargs): raise AssertionError('private remote write')

    site = SimpleNamespace(id='person_a-site', name='人甲 家')

    async def decide(_messages):
        return {'content': json.dumps({'actions': [{'action': 'post', 'content': '只给站主', 'visibility': 'private'}],
                                       'next': {'kind': 'exit'}, 'reflection': ''})}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    monkeypatch.setattr(visit, '_site_options', lambda: [{'id': site.id, 'name': site.name}])
    monkeypatch.setattr(visit, '_selected_store', lambda _request: (Remote(), site))
    result = await visit.visit_step(request={'kind': 'latest', 'site_id': site.id}, previous_nodes=[],
                                    activity_reason='', run_id='r', activity_id='a', node_id='n', persona='AI', session_id='s')
    assert result['action_results'][0]['status'] == 'remote_public_only'


@pytest.mark.asyncio
async def test_remote_visibility_change_is_rejected_even_for_own_post(feed, monkeypatch):
    remote_post = {'id': 'remote-own-post', 'author': 'visitor:k', 'content': '远端动态',
                   'visibility': 'public', 'comments': [], 'reactions': [], 'people': {}, 'created_at': 100}

    class Remote:
        async def me(self): return 'visitor:k'
        async def list_moments(self, **_kwargs): return {'items': [remote_post], 'next_cursor': ''}
        async def set_moment_visibility(self, *_args, **_kwargs):
            raise AssertionError('remote visibility must not be changed')

    site = SimpleNamespace(id='person_a-site', name='人甲 家')

    async def decide(_messages):
        return {'content': json.dumps({'actions': [{
            'action': 'set_visibility', 'moment_id': remote_post['id'], 'visibility': 'private',
        }], 'next': {'kind': 'exit'}, 'reflection': ''})}

    visit = SocialCircleVisit(decide, store=feed, context_builder=_context_builder)
    monkeypatch.setattr(visit, '_site_options', lambda: [{'id': site.id, 'name': site.name}])
    monkeypatch.setattr(visit, '_selected_store', lambda _request: (Remote(), site))
    result = await visit.visit_step(request={'kind': 'latest', 'site_id': site.id},
                                    previous_nodes=[], activity_reason='', run_id='r',
                                    activity_id='a', node_id='n', persona='AI', session_id='s')
    assert result['action_results'][0]['status'] == 'forbidden_target'
