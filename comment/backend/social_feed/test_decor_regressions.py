"""Temporary-only regression cases from the October decor acceptance round."""
import json
from types import SimpleNamespace
import pytest
import httpx
from social_feed.decor_models import HomeDesign
from social_feed.decor_context import shelf_facts
from social_feed.public_wall import PublicWall
from social_feed.test_decor import gateway, make_home


@pytest.mark.asyncio
async def test_failed_event_delivery_keeps_successful_gift_response(gateway,monkeypatch):
    app,store = gateway; eid=make_home(store);store.set_gift('ai',eid,'machine-gift')
    async def fail(): raise RuntimeError('delivery unavailable')
    monkeypatch.setattr('social_feed.decor_events.publish_pending',fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        one=await client.post('/social/v1/decor/visit',headers={'x-test-actor':'visitor:a'})
        assert one.status_code==200 and len(one.json()['gifts'])==1
        two=await client.post('/social/v1/decor/visit',headers={'x-test-actor':'visitor:a'})
        assert one.json()['gifts'][0]['id']==two.json()['gifts'][0]['id']
        assert len(store.collection('visitor:a'))==1


def test_login_arrival_issues_machine_gift_before_frontend_mount(tmp_path,monkeypatch):
    from social_feed.public_gateway import _receive_login_gift
    from social_feed.decor_store import get_decor_store
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    monkeypatch.setattr('social_feed.public_gateway._display',lambda *args,**kwargs:{'name':'测试机'})
    runtime=SimpleNamespace(visitor_service=SimpleNamespace(effective_visitor=lambda _id:SimpleNamespace(visitor_kind='external_ai')))
    store=get_decor_store();store.set_gift('ai',make_home(store),'machine-round')
    _receive_login_gift(runtime,'visitor:a'); _receive_login_gift(runtime,'visitor:a')
    assert len(store.collection('visitor:a'))==1 and store.collection('visitor:h')==[]


def test_shelf_projection_includes_facts_not_image_inference():
    fact=shelf_facts({'home':{'exhibits':[{'name':'小酒杯','description':'纪念一场会客','ai_note':'坐下来聊聊','image':'asset-ref'}]}})
    assert fact['exhibits'][0]['ai_note']=='坐下来聊聊'
    assert fact['exhibits'][0]['has_display_image'] and '像素' in fact['image_evidence']
    assert 'asset-ref' not in json.dumps(fact)
    assert HomeDesign.model_validate({}).theme.background_fit=='width'
    assert HomeDesign.model_validate({'theme':{'music_title':'手动设置的曲名'}}).theme.music_title=='手动设置的曲名'


@pytest.mark.asyncio
async def test_delivery_report_owner_only_and_stable_cursor(gateway):
    app,store=gateway; eid=make_home(store)
    store.set_gift('human',eid,'delivery-human');store.set_gift('ai',eid,'delivery-ai')
    store.visit('visitor:h','human','人');store.visit('visitor:a','ai','机')
    report=store.delivery_report(limit=1)
    assert report['total']==2 and report['new_count']==2 and report['has_more']
    second=store.delivery_report(limit=1,before_time=report['next_cursor']['time'],before_id=report['next_cursor']['id'])
    assert second['items'][0]['id'] != report['items'][0]['id'] and not second['has_more']
    assert store.delivery_report(since=report['observed_at'])['new_count']==0
    # Imported gifts are not this domain's outgoing distribution list.
    store.imported_gift('k','https://friend.example',{'gift_id':'remote','snapshot':store.collection('visitor:h')[0]['snapshot']},'朋友家')
    assert store.delivery_report()['total']==2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        assert (await client.get('/social/v1/decor/deliveries')).status_code==403
        result=await client.get('/social/v1/decor/deliveries',headers={'x-test-actor':'aning'})
        assert result.status_code==200 and result.json()['total']==2
        assert all('snapshot' not in row and 'actor' not in row for row in result.json()['items'])


def test_music_choices_only_cached_metadata(monkeypatch):
    from social_feed.decor_music import music_choices
    store=SimpleNamespace(search_shared_songs=lambda *args:[{'id':'123','name':'一首歌','artist':'歌手'}],
        bindings=lambda:[{'id':'p','subject':'shared'}],material=lambda _id:{'songs':[{'id':'456','name':'歌单曲','artist':'另一位'}]})
    monkeypatch.setattr('music_system.service.get_service',lambda:SimpleNamespace(store=store))
    assert [s['id'] for s in music_choices()]==['123','456']


def test_contact_removal_hides_card_and_preserves_posts(tmp_path):
    from routers.lounge_social_contacts_router import _social_contacts
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    wall.register_human_name('h','测试人');wall.register_human_name('a','测试机');wall.link_household('h','a')
    post=wall.create_moment('visitor:h','历史内容',source_key='history-request')
    wall.remove_contact('h')
    assert all(c['visitor_id']!='h' for c in _social_contacts(wall))
    assert wall.get_moment(post['id'])['content']=='历史内容'
    assert wall.registered_name('h')=='测试人' and wall.household_human('a')==''


def test_only_empty_credentials_are_deletable(tmp_path):
    from lounge_reception.runtime import ReceptionRuntime,local_settings
    from visitor_lounge.identity_management import can_delete_unclaimed,delete_unclaimed_keys
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    runtime=ReceptionRuntime(local_settings(tmp_path/'reception'),adapter=object(),prompt=lambda *args:None)
    empty=runtime.visitors.create_unclaimed_visitor('human'); secret=runtime.keys.create(empty).value
    with runtime.database.connection() as db: assert can_delete_unclaimed(db,empty,wall)
    assert delete_unclaimed_keys(runtime,wall,empty)==1
    assert runtime.keys.authenticate_bearer(secret) is None
    registered=runtime.visitors.create_unclaimed_visitor('human');runtime.keys.create(registered)
    wall.register_human_name(registered,'已登记的人')
    with pytest.raises(ValueError): delete_unclaimed_keys(runtime,wall,registered)
    assert runtime.visitors.visitor(registered).status=='active'


@pytest.mark.asyncio
async def test_contact_delete_route_protects_active_visit_and_keeps_key(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from lounge_reception.runtime import ReceptionRuntime,local_settings
    from routers import lounge_social_contacts_router as router
    from social_feed.household_identity import registered_social_visitor
    from social_feed.public_wall import PublicWallError
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    runtime=ReceptionRuntime(local_settings(tmp_path/'reception'),adapter=object(),prompt=lambda *args:None)
    visitor=runtime.visitors.create_unclaimed_visitor('human'); key=runtime.keys.create(visitor).value
    wall.register_human_name(visitor,'测试朋友')
    async def get_runtime(): return runtime
    monkeypatch.setattr(router,'get_runtime',get_runtime)
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[router.local_ui]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        lane=runtime.service.lane(visitor);await lane.acquire()
        try:
            assert (await client.delete('/api/lounge-social-contacts/identities/'+visitor)).status_code==409
            assert runtime.keys.authenticate_bearer(key) is not None
        finally: lane.release()
        result=await client.delete('/api/lounge-social-contacts/identities/'+visitor)
        assert result.status_code==200 and result.json()['deleted']
        assert runtime.keys.authenticate_bearer(key) == visitor
        assert runtime.visitors.visitor(visitor).status == 'active'
        with pytest.raises(PublicWallError): registered_social_visitor(runtime,visitor)


@pytest.mark.asyncio
async def test_pause_resume_keeps_same_key_and_delete_requires_both_rosters(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from lounge_reception.runtime import ReceptionRuntime,local_settings
    from lounge_visits.lounge_friends import LoungeFriendStore
    from lounge_visits import cognition_profiles,storage
    from routers import lounge_reception_router as router
    from social_feed.household_identity import registered_social_visitor
    from social_feed.public_wall import PublicWallError
    from visitor_lounge.identity_management import live_friend_links
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    friends=LoungeFriendStore(tmp_path/'friends.json')
    monkeypatch.setattr(storage,'friends',lambda:friends)
    recognition={}
    monkeypatch.setattr(cognition_profiles,'get',lambda cid:recognition.get(cid,{}))
    runtime=ReceptionRuntime(local_settings(tmp_path/'reception'),adapter=object(),prompt=lambda *args:None)
    visitor=runtime.visitors.create_unclaimed_visitor('human'); key=runtime.keys.create(visitor).value
    wall.register_human_name(visitor,'测试人')
    async def get_runtime(): return runtime
    monkeypatch.setattr(router,'get_runtime',get_runtime)
    app=FastAPI();app.include_router(router.router);app.dependency_overrides[router.local_ui]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/lounge-reception/visitors/'+visitor+'/revoke')).status_code==200
        assert runtime.visitors.visitor(visitor).status=='paused'
        with runtime.database.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM visitor_keys WHERE visitor_id=? AND revoked_at IS NULL',(visitor,)).fetchone()[0]==1
        with pytest.raises(PublicWallError): registered_social_visitor(runtime,visitor)
        assert (await client.post('/api/lounge-reception/visitors/'+visitor+'/resume')).status_code==200
        assert runtime.keys.authenticate_bearer(key)==visitor
        assert registered_social_visitor(runtime,visitor).status=='active'
        displayed=(await client.get('/api/lounge-reception')).json()
        assert not next(row for row in displayed['visitors'] if row['id']==visitor)['can_delete_key']
        assert (await client.delete('/api/lounge-reception/visitors/'+visitor+'/key')).status_code==409
        friend=friends.create(actor_id='k',display_name='测试朋友',lounge_url='https://friend.test/mcp',visitor_key='remote-test-key',
            relationship_note='测试关联',enabled=True,allow_autonomous=False,cooldown_hours=12,max_turns=4)
        wall.note_contact(visitor);wall.link_contact(visitor,friend.id);wall.remove_contact(visitor)
        assert (await client.delete('/api/lounge-reception/visitors/'+visitor+'/key')).status_code==409
        # Explicit cognition binding also protects a roster entry with no social link.
        other=runtime.visitors.create_unclaimed_visitor('human');runtime.keys.create(other)
        recognition.update({'visitor:'+other:{'primary_entity_id':'entity-a'},'friend:'+friend.id:{'primary_entity_id':'entity-a'}})
        assert live_friend_links(wall,other)=={friend.id}
        assert (await client.delete('/api/lounge-reception/visitors/'+other+'/key')).status_code==409
        friends.delete('k',friend.id)
        displayed=(await client.get('/api/lounge-reception')).json()
        assert next(row for row in displayed['visitors'] if row['id']==visitor)['can_delete_key']
        assert (await client.delete('/api/lounge-reception/visitors/'+visitor+'/key')).status_code==200
        assert runtime.keys.authenticate_bearer(key) is None
        assert wall.registered_name(visitor)=='测试人'


@pytest.mark.asyncio
async def test_human_roster_removal_cascades_machines_without_deleting_keys(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from lounge_reception.runtime import ReceptionRuntime,local_settings
    from lounge_visits.lounge_friends import LoungeFriendStore
    from lounge_visits import storage,cognition_profiles
    from routers import lounge_social_contacts_router as contacts
    from routers import lounge_reception_router as reception
    from social_feed.household_identity import registered_social_visitor
    from social_feed.public_wall import PublicWallError
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    monkeypatch.setattr(storage,'friends',lambda:LoungeFriendStore(tmp_path/'friends.json'))
    monkeypatch.setattr(cognition_profiles,'get',lambda _id:{})
    runtime=ReceptionRuntime(local_settings(tmp_path/'reception'),adapter=object(),prompt=lambda *args:None)
    human=runtime.visitors.create_unclaimed_visitor('human')
    machines=[runtime.visitors.create_unclaimed_visitor('external_ai') for _ in range(2)]
    other=runtime.visitors.create_unclaimed_visitor('human')
    keys={identity:runtime.keys.create(identity).value for identity in [human,*machines,other]}
    for identity in keys: wall.register_human_name(identity,'测试身份')
    for machine in machines: wall.link_household(human,machine)
    post=wall.create_moment('visitor:'+machines[0],'保留的旧动态',source_key='history')
    async def get_runtime():return runtime
    monkeypatch.setattr(contacts,'get_runtime',get_runtime);monkeypatch.setattr(reception,'get_runtime',get_runtime)
    app=FastAPI();app.include_router(contacts.router);app.include_router(reception.router)
    app.dependency_overrides[contacts.local_ui]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        lane=runtime.service.lane(machines[0]);await lane.acquire()
        try:
            assert (await client.delete('/api/lounge-social-contacts/identities/'+human)).status_code==409
            assert human not in wall.removed_contacts() and machines[1] not in wall.removed_contacts()
        finally:lane.release()
        result=await client.delete('/api/lounge-social-contacts/identities/'+human)
        assert result.status_code==200 and result.json()['removed_count']==3
        assert wall.household_links()==[] and wall.get_moment(post['id'])['content']=='保留的旧动态'
        assert other not in wall.removed_contacts()
        for identity in [human,*machines]:
            assert runtime.keys.authenticate_bearer(keys[identity])==identity
            assert identity in wall.removed_contacts()
            with pytest.raises(PublicWallError):registered_social_visitor(runtime,identity)
        for identity in [*machines,human]:
            assert (await client.delete('/api/lounge-reception/visitors/'+identity+'/key')).status_code==200
            assert runtime.keys.authenticate_bearer(keys[identity]) is None
        assert runtime.keys.authenticate_bearer(keys[other])==other
