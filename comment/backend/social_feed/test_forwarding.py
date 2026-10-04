import asyncio

import pytest

from social_feed.hybrid_store import HybridSocialFeedStore
from social_feed.public_wall import PublicWall, PublicWallError
from social_feed.store import SocialFeedStore


def test_same_home_forward_is_dynamic_and_never_copies_source(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db')
    wall.initialize()
    source = wall.create_moment('aning', '原文')
    forwarded = wall.create_moment('k', '转发了一条动态', forward_ref={'origin': '', 'moment_id': source['id']})
    with wall._connect() as db:
        stored = db.execute('SELECT content,forward_json FROM wall_moments WHERE id=?', (forwarded['id'],)).fetchone()
    assert stored['content'] == '转发了一条动态'
    assert '原文' not in stored['forward_json']
    assert wall.get_moment(forwarded['id'])['forward']['source']['content'] == '原文'
    wall.edit_moment('aning', source['id'], '已编辑', 1)
    assert wall.get_moment(forwarded['id'])['forward']['source']['content'] == '已编辑'
    wall.withdraw('aning', source['id'])
    assert wall.get_moment(forwarded['id'])['forward']['status'] == 'unavailable'


def test_forward_rejects_recursive_and_invalid_origin(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    source = wall.create_moment('aning', '源')
    forwarded = wall.create_moment('k', '转发', forward_ref={'origin': '', 'moment_id': source['id']})
    with pytest.raises(PublicWallError, match='forward_source_unavailable'):
        wall.create_moment('aning', '再转', forward_ref={'origin': '', 'moment_id': forwarded['id']})
    with pytest.raises(ValueError):
        wall.create_moment('aning', '坏源', forward_ref={'origin': 'https://localhost', 'moment_id': source['id']})


def test_external_forward_is_link_only(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    item = wall.create_moment('aning', '转发', forward_ref={'origin': 'https://Example.COM', 'moment_id': 'sw_remote'})
    forward = wall.get_moment(item['id'])['forward']
    assert forward['status'] == 'link_only'
    assert forward['reference'] == {'origin': 'https://example.com', 'moment_id': 'sw_remote'}
    assert 'source' not in forward


def test_private_public_round_trip_keeps_reference(tmp_path):
    async def run():
        feed = HybridSocialFeedStore(SocialFeedStore(tmp_path / 'private.db'), PublicWall(tmp_path / 'public.db'))
        source = await feed.create_moment('aning', '公开源', visibility='public')
        forwarded = await feed.create_moment('k', '转发', visibility='private',
                                             forward_ref={'origin': '', 'moment_id': source['id']})
        assert (await feed.get_moment(forwarded['id']))['forward']['status'] == 'available'
        await feed.set_moment_visibility(forwarded['id'], 'k', 'public')
        assert (await feed.get_moment(forwarded['id']))['forward']['status'] == 'available'
        await feed.set_moment_visibility(forwarded['id'], 'k', 'private')
        assert (await feed.get_moment(forwarded['id']))['forward']['status'] == 'available'
        await feed.set_moment_visibility(source['id'], 'aning', 'private')
        assert (await feed.get_moment(forwarded['id']))['forward']['status'] == 'unavailable'
        with pytest.raises(Exception, match='forward_source_unavailable'):
            await feed.create_moment('k', '不能转私密源', visibility='private',
                                     forward_ref={'origin': '', 'moment_id': source['id']})
    asyncio.run(run())


def test_source_made_private_and_private_source_cannot_forward(tmp_path):
    async def run():
        feed = HybridSocialFeedStore(SocialFeedStore(tmp_path/'private.db'),PublicWall(tmp_path/'public.db'))
        source = await feed.create_moment('aning','隐私正文',visibility='public')
        copy = await feed.create_moment('k','附言',visibility='public',forward_ref={'origin':'','moment_id':source['id']})
        await feed.set_moment_visibility(source['id'],'aning','private')
        projection = await feed.get_moment(copy['id'])
        assert projection['forward']['status']=='unavailable'
        assert '隐私正文' not in str(projection)
        with pytest.raises(ValueError,match='forward_source_unavailable'):
            await feed.create_moment('k','非法转发',forward_ref={'origin':'','moment_id':source['id']})
    asyncio.run(run())


@pytest.mark.asyncio
async def test_authenticated_cross_home_forward_sends_reference_not_body():
    from types import SimpleNamespace
    from social_feed.forward_action import forward_moment
    class Registry:
        def get(self,id):return SimpleNamespace(id=id,origin='https://'+id+'.example.test',enabled=True,human_key='fake',ai_key='fake')
    class Transport:
        def __init__(self):self.calls=[]
        async def request(self,site,kind,method,path,**kwargs):
            self.calls.append((site.id,kind,method,path,kwargs))
            if path.endswith('/me'):return {'can_manage_avatar':True,'capabilities':['forwarding_v1']}
            if method=='GET':return {'id':'same-id','visibility':'public','content':'源文不入目的站','author':'visitor:friend'}
            return {'id':'created'}
    transport=Transport()
    result=await forward_moment(actor='aning',moment_id='same-id',source_site_id='a',site_id='b',visibility='public',source_key='request',sites=Registry(),client=transport,local_store=object())
    payload=transport.calls[-1][-1]['payload']
    assert payload['forward_ref']=={'origin':'https://a.example.test','moment_id':'same-id'}
    assert '源文不入目的站' not in str(payload)
    assert result['id']=='created'


@pytest.mark.asyncio
async def test_same_home_ids_and_remote_capability_are_checked():
    from types import SimpleNamespace
    from social_feed.forward_action import forward_moment
    class Registry:
        def get(self,id):return SimpleNamespace(id=id,origin='https://'+id+'.example.test',enabled=True,human_key='fake',ai_key='fake')
    class Transport:
        capability=True;private=False;calls=[]
        async def request(self,site,kind,method,path,**kwargs):
            self.calls.append((method,path,kwargs))
            if path.endswith('/me'):return {'can_manage_avatar':False,'capabilities':['forwarding_v1'] if self.capability else []}
            if method=='GET':return {'id':'source','visibility':'private' if self.private else 'public','content':'正文'}
            return {'id':'created'}
    transport=Transport()
    await forward_moment(actor='k',moment_id='source',source_site_id='a',site_id='a',visibility='public',sites=Registry(),client=transport,local_store=object())
    assert transport.calls[-1][-1]['payload']['forward_ref']['origin']==''
    transport.capability=False
    with pytest.raises(ValueError,match='forwarding_unavailable'):
        await forward_moment(actor='k',moment_id='source',source_site_id='a',site_id='a',visibility='public',sites=Registry(),client=transport,local_store=object())
    transport.private=True
    with pytest.raises(ValueError,match='forward_source_unavailable'):
        await forward_moment(actor='k',moment_id='source',source_site_id='a',site_id='b',visibility='public',sites=Registry(),client=transport,local_store=object())


@pytest.mark.asyncio
async def test_main_tool_uses_k_actor_and_separate_source_destination(monkeypatch):
    from behavior_scheduler.social_feed_tool import ManageSocialFeedTool
    recorded = {}
    async def forward(**kwargs):
        recorded.update(kwargs)
        return {'id':'forwarded'}
    monkeypatch.setattr('social_feed.forward_action.forward_moment', forward)
    store = object()
    tool = ManageSocialFeedTool(store=store)
    result = await tool._direct({'moment_id':'source-id', 'source_site_id':'source-home',
                                 'content':'自己的附言','visibility':'private'}, 'forward')
    assert result['status']=='success' and result['moment_id']=='forwarded'
    assert recorded['actor']=='k' and recorded['source_site_id']=='source-home'
    assert recorded['site_id']=='' and recorded['local_store'] is store
    assert recorded['content']=='自己的附言'
    assert tool.ui_only_result is True


@pytest.mark.asyncio
async def test_wander_forward_is_grounded_and_replays_without_duplicate(tmp_path):
    import json
    from types import SimpleNamespace
    from wander_manager.social_circle_visit import SocialCircleVisit, activity_summary
    feed = HybridSocialFeedStore(SocialFeedStore(tmp_path/'private.db'),PublicWall(tmp_path/'public.db'))
    source = await feed.create_moment('aning','公开来源正文',visibility='public')
    async def context(_recipe, **kwargs):
        return SimpleNamespace(system_content=kwargs['wander_runtime_text'])
    async def decide(_messages):
        return {'content':json.dumps({'actions':[{'action':'forward','moment_id':source['id'],
            'content':'喜欢这一刻','visibility':'private'}],'next':{'kind':'exit'},'reflection':'记下来了'})}
    visit = SocialCircleVisit(decide,store=feed,context_builder=context)
    visit._site_options = lambda: []
    kwargs = dict(request={'kind':'latest'}, previous_nodes=[],activity_reason='看看',run_id='r',
                  activity_id='a',node_id='n',persona='AI',session_id='s')
    first = await visit.visit_step(**kwargs)
    again = await visit.visit_step(**kwargs)
    result = first['action_results'][0]
    assert result['status']=='success', result
    assert again['action_results'][0]['moment_id']==result['moment_id']
    saved = await feed.get_moment(result['moment_id'])
    assert saved['forward']['reference']['moment_id']==source['id']
    assert saved['content']=='喜欢这一刻'
    summary = await activity_summary(feed,[first])
    assert '引用转发' in summary and '喜欢这一刻' in summary and '记下来了' in summary
    assert '公开来源正文' not in summary


@pytest.mark.asyncio
async def test_public_forward_api_requires_session_csrf_and_current_source(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from social_feed.public_gateway import create_public_social_app
    wall = PublicWall(tmp_path/'public.db'); wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    source = wall.create_moment('aning','公开原文')
    app = create_public_social_app(SimpleNamespace())
    payload = {'content':'自己的附言','forward_ref':{'origin':'','moment_id':source['id']},'request_id':'fixture-forward-001'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        assert (await client.post('/social/v1/moments',json=payload)).status_code==403
        wall.issue_session('fixture-session','fixture-csrf','__owner__','__owner__',9999999999)
        client.cookies.set('mirrow_wall_session','fixture-session')
        client.cookies.set('mirrow_wall_csrf','fixture-csrf')
        assert (await client.post('/social/v1/moments',json=payload)).status_code==403
        headers={'X-MIRROW-CSRF':'fixture-csrf'}
        created = await client.post('/social/v1/moments',json=payload,headers=headers)
        assert created.status_code==200, created.text
        assert created.json()['forward']['source']['content']=='公开原文'
        repeat = await client.post('/social/v1/moments',json=payload,headers=headers)
        assert repeat.json()['id']==created.json()['id']
        assert len(wall.list_moments()['items'])==2
        wall.withdraw('aning',source['id'])
        response = await client.get('/social/v1/moments/'+created.json()['id'])
        assert response.json()['forward']['status']=='unavailable'
        assert '公开原文' not in response.text
        rejected = await client.post('/social/v1/moments',json={**payload,'request_id':'fixture-forward-002'},headers=headers)
        assert rejected.status_code==400


def test_transfer_carries_only_durable_ref_and_keeps_original_home(tmp_path,monkeypatch):
    from social_feed import migration
    old,new = PublicWall(tmp_path/'old.db'),PublicWall(tmp_path/'new.db')
    for wall in (old,new):
        wall.initialize();wall.migrate_transfers()
    monkeypatch.setattr('social_feed.public_gateway.PUBLIC_SOCIAL_ORIGIN','https://old.example.test')
    source = old.create_moment('aning','不应进入搬迁快照的原文')
    forwarded = old.create_moment('visitor:friend','自己的附言',forward_ref={'origin':'','moment_id':source['id']})
    prepared = migration.prepare(old,'visitor:friend',forwarded['id'],'https://new.example.test','aning')
    assert '不应进入搬迁快照的原文' not in str(prepared['snapshot'])
    staged = migration.stage(new,transfer_id=prepared['transfer_id'],source_origin='https://old.example.test',
        source_actor='visitor:friend',target_actor='aning',snapshot=prepared['snapshot'],expected_digest=prepared['digest'])
    committed = migration.commit(old,'visitor:friend',prepared['transfer_id'],staged['destination_moment_id'])
    activated = migration.activate(new,prepared['transfer_id'],committed)
    projection = new.get_moment(activated['id'])['forward']
    assert projection['status']=='link_only'
    assert projection['reference']=={'origin':'https://old.example.test','moment_id':source['id']}
