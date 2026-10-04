"""Temporary stores only: admission, ownership, proof binding and durable gifts."""
import hashlib
import io
import json
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from social_feed.decor_store import DecorStore, DecorError
from social_feed.decor_models import HomeDesign, PersonalDesign
from social_feed.decor_media import save_media, image_bytes
from social_feed.public_wall import PublicWall
from social_feed.public_gateway import create_public_social_app


def make_home(store):
    home = store.save_home(HomeDesign(exhibits=[{'name':'小杯酒','description':'架上的纪念品','human_note':'欢迎来玩','ai_note':'坐会儿吧'}]))
    return home['exhibits'][0]['id']


def test_gift_receipts_split_deduplicate_and_survive_removal(tmp_path):
    store=DecorStore(tmp_path/'decor.db'); eid=make_home(store)
    store.set_gift('human',eid,'human-round-1'); store.set_gift('ai',eid,'ai-round-1')
    first=store.visit('visitor:h','human','人甲')
    assert len(first)==1
    assert store.collection('visitor:a')==[]
    assert store.visit('visitor:h','human','改网名')[0]['id']==first[0]['id']
    store.seen('visitor:other',first[0]['id'])
    assert len(store.visit('visitor:h','human','人甲'))==1
    store.seen('visitor:h',first[0]['id'])
    assert store.visit('visitor:h','human','人甲')==[]
    assert len(store.visit('visitor:a','ai','机乙'))==1
    store.set_gift('human',eid,'human-round-2')
    assert len(store.visit('visitor:h','human','人甲'))==1
    home=store.home();home['exhibits']=[];store.save_home(HomeDesign.model_validate(home))
    assert store.gifts()==[]
    assert len(store.collection('visitor:h'))==2
    assert store.collection('visitor:a')[0]['snapshot']['name']=='小杯酒'
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==3
    assert store.visit('aning','human','站主')==[]


@pytest.mark.asyncio
async def test_pending_decor_target_disables_and_rejects_all_editor_writes(gateway):
    from social_feed.public_wall import get_public_wall
    from social_feed.profile_registration import ProfileRegistrationStore
    app, store = gateway
    wall = get_public_wall(); registration = ProfileRegistrationStore(wall)
    from social_feed.profile_identity import ProfileIdentityStore
    ProfileIdentityStore(wall).migrate(backup_ready=True)
    registration.migrate(backup_ready=True)
    registration.select('visitor:a', {'choice':'existing','origin':'https://profile.example'}, 'https://social.example.invalid')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        profiles = (await client.get('/social/v1/decor/state')).json()['profiles']
        assert {p['actor']:p['can_edit_decor'] for p in profiles} == {'visitor:h':True,'visitor:a':False}
        for path, method, options in (
            ('/people/visitor:a','PUT',{'json':{'frame':'orbit'}}),
            ('/media/visitor:a','POST',{'content':b'not-an-image'}),
            ('/presets/visitor:a','POST',{'json':{'id':'not-a-preset'}}),
            ('/gif/visitor:a','POST',{'content':b'not-gif-input'}),
        ):
            response = await client.request(method,'/social/v1/decor'+path,**options)
            assert response.status_code == 403, (path,response.text)
            assert response.json()['detail'] == 'profile_verification_pending'
        assert (await client.put('/social/v1/decor/people/visitor:h',json={'frame':'orbit'})).status_code == 200
    assert store.personal('visitor:a')['frame'] == 'none'


@pytest.mark.asyncio
async def test_guardian_comment_projection_and_delete_match_http_permission(gateway):
    from social_feed.public_wall import get_public_wall
    app, _store = gateway; wall = get_public_wall()
    wall.link_household('h','a')
    post = wall.create_moment('aning','隔离测试动态')
    own_ai = wall.add_comment('visitor:a',post['id'],'自家机的评论')
    other = wall.add_comment('visitor:other',post['id'],'另一家的评论')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        item = (await client.get('/social/v1/moments/'+post['id'])).json()
        assert {row['id']:row['can_delete'] for row in item['comments']} == {own_ai['id']:True,other['id']:False}
        assert (await client.delete(f"/social/v1/moments/{post['id']}/comments/{other['id']}")).status_code == 403
        assert (await client.delete(f"/social/v1/moments/{post['id']}/comments/{own_ai['id']}")).status_code == 200


def test_design_revision_and_asset_owner(tmp_path):
    store=DecorStore(tmp_path/'decor.db');make_home(store)
    with pytest.raises(DecorError,match='design_conflict'):
        store.save_home(HomeDesign())
    output=io.BytesIO();Image.new('RGB',(40,40),'red').save(output,format='PNG')
    asset=save_media(store,'visitor:h',output.getvalue())
    store.save_personal('visitor:h',PersonalDesign(frame_asset=asset['id']))
    with pytest.raises(DecorError,match='asset_not_owned'):
        store.save_personal('visitor:other',PersonalDesign(frame_asset=asset['id']))


def test_gif_animation_survives_and_payload_is_not_html():
    raw=io.BytesIO()
    Image.new('RGB',(24,24),'red').save(raw,format='GIF',save_all=True,
        append_images=[Image.new('RGB',(24,24),'blue')],duration=[100,150],loop=0,comment=b'private-note')
    cleaned,ext,mime=image_bytes(raw.getvalue())
    with Image.open(io.BytesIO(cleaned)) as image:
        assert image.n_frames==2 and 'comment' not in image.info
    assert ext=='gif' and mime=='image/gif'
    with pytest.raises(DecorError): image_bytes(b'<svg onload="bad()"></svg>')


@pytest.fixture
def gateway(tmp_path,monkeypatch):
    wall=PublicWall(tmp_path/'wall.db');wall.initialize();wall.note_contact('h');wall.note_contact('a')
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    monkeypatch.setattr('social_feed.public_gateway._identity',lambda runtime,request,write=False,admission=True:request.headers.get('x-test-actor','visitor:h'))
    monkeypatch.setattr('social_feed.public_gateway._display',lambda runtime,actor,viewer:{'actor_id':actor,'name':actor,'avatar':''})
    monkeypatch.setattr('social_feed.public_gateway._managed_profiles',lambda runtime,actor:['aning','k'] if actor=='aning' else ['visitor:h','visitor:a'] if actor=='visitor:h' else [actor])
    runtime=SimpleNamespace(visitor_service=SimpleNamespace(effective_visitor=lambda vid:SimpleNamespace(visitor_kind='human' if vid=='h' else 'external_ai')))
    # Suppress external delivery: test receipts must never enter real main chat.
    async def publish(*args,**kwargs): return 0
    monkeypatch.setattr('social_feed.decor_events.publish_pending',publish)
    from social_feed.decor_store import get_decor_store
    return create_public_social_app(runtime),get_decor_store()


@pytest.mark.asyncio
async def test_public_boundary_personal_permissions_and_signed_sync(gateway,monkeypatch):
    app,store=gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        assert (await client.put('/social/v1/decor/home',json={})).status_code==403
        assert (await client.post('/social/v1/decor/gifts',json={'kind':'human','exhibit_id':'x','request_id':'unique-123'})).status_code==403
        assert (await client.put('/social/v1/decor/people/visitor:a',json={'frame':'orbit'})).status_code==200
        assert (await client.put('/social/v1/decor/people/visitor:a',json={'frame':'lace'},headers={'x-test-actor':'visitor:a'})).status_code==403
        assert (await client.put('/social/v1/decor/people/aning',json={'frame':'lace'})).status_code==403
        state=await client.get('/social/v1/decor/state');assert state.json()['owner'] is False and state.json()['sites']==[]
        proof={'origin':'https://person_a.example','subject':'aning','audience':'https://social.example.invalid','actor':'visitor:h'}
        async def read(*args):return proof
        monkeypatch.setattr('social_feed.decor_federation.read_proof',read)
        body={'origin':'https://person_a.example','subject':'aning','token':'x'*40}
        assert (await client.post('/social/v1/decor/link',json=body)).status_code==200
        proof['actor']='visitor:other'
        assert (await client.post('/social/v1/decor/link',json=body)).status_code==400
        payload={'origin':'https://person_a.example','subject':'aning','design':PersonalDesign(frame='orbit').model_dump(),'frame_data':'','card_data':''}
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        proof={'operation':'decor_sync','actor':'visitor:h','audience':'https://social.example.invalid','digest':digest,'version':1}
        assert (await client.put('/social/v1/decor/sync',json=payload|{'token':'x'*40})).status_code==200
        assert store.personal('visitor:h')['frame']=='orbit'
        payload['design']['frame']='lace'
        assert (await client.put('/social/v1/decor/sync',json=payload|{'token':'x'*40})).status_code==400
        assert (await client.delete('/social/v1/decor/link/visitor:h')).status_code==200
        assert store.link('visitor:h') is None


def test_gift_fact_consumed_by_context_and_cognition():
    from context_builder.notification_projection import build_notification_context_fact
    from cognition.maintenance_sources import sources
    message={'id':'gift-real-receipt','role':'event','event_type':'social_gift','content':'AI 在 人甲 家收到了小杯酒。'}
    assert 'AI 在 人甲' in build_notification_context_fact(message)
    # Day-summary source adapter uses this exact shared projection.
    assert '小杯酒' in sources([message])[0]['text']


def test_gift_race_and_old_sync_cannot_overwrite_latest(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store=DecorStore(tmp_path/'decor.db');eid=make_home(store)
    store.set_gift('human',eid,'shared-round')
    with ThreadPoolExecutor(max_workers=6) as workers:
        results=list(workers.map(lambda _:store.visit('visitor:h','human','人甲'),range(12)))
    assert len({rows[0]['id'] for rows in results})==1
    store.bind('visitor:h','https://person_a.example','aning')
    assert store.save_synced('visitor:h','https://person_a.example',20,PersonalDesign(frame='orbit'))
    assert not store.save_synced('visitor:h','https://person_a.example',19,PersonalDesign(frame='lace'))
    assert store.personal('visitor:h')['frame']=='orbit'
    store.queue_sync('aning','site',True); old=store.sync_jobs('aning')[0]
    store.queue_sync('aning','site',False)
    store.finish_sync('aning','site',old['generation'])
    assert store.sync_jobs('aning')[0]['active']==0
    store.unlink('visitor:h')
    with pytest.raises(DecorError,match='home_link_required'):
        store.save_synced('visitor:h','https://person_a.example',30,PersonalDesign(frame='lace'))


@pytest.mark.asyncio
async def test_uploaded_media_gifts_and_collections_are_scoped(gateway):
    app,store=gateway
    raw=io.BytesIO();Image.new('RGB',(24,24),'red').save(raw,format='JPEG')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        response=await client.post('/social/v1/decor/media/visitor:a',content=raw.getvalue())
        assert response.status_code==200
        assert response.json()['mime']=='image/png'
        assert (await client.get('/social/v1/decor/media/'+response.json()['id'])).status_code==200
        assert (await client.post('/social/v1/decor/media/visitor:a',content=raw.getvalue(),headers={'x-test-actor':'visitor:a'})).status_code==403
        eid=make_home(store);store.set_gift('human',eid,'human-gift');store.set_gift('ai',eid,'ai-gift')
        human=(await client.post('/social/v1/decor/visit')).json()['gifts'][0]
        machine=(await client.post('/social/v1/decor/visit',headers={'x-test-actor':'visitor:a'})).json()['gifts'][0]
        assert human['gift_id']!=machine['gift_id']
        await client.post('/social/v1/decor/seen',json={'id':machine['id']})
        assert store.collection('visitor:a',pending=True)
        assert (await client.get('/social/v1/decor/collection?subject=visitor:h',headers={'x-test-actor':'visitor:a'})).status_code==403


def test_audio_rejects_external_playlist():
    from social_feed.decor_media import audio_bytes
    with pytest.raises(DecorError,match='invalid_decor_audio'):
        audio_bytes(b'#EXTM3U\nfile:///private/audio.wav')


@pytest.mark.asyncio
async def test_optional_gift_failure_does_not_break_remote_visit(monkeypatch):
    from social_feed.remote_visit_store import RemoteSocialVisitStore
    from social_feed.remote_sites import RemoteSiteError
    class Client:
        async def request(self,*args,**kwargs):
            return {'actor':{'actor_id':'visitor:k'},'can_manage_avatar':False,'capabilities':['decor_v1']}
    async def fail(*args):raise RemoteSiteError('social_site_unavailable')
    monkeypatch.setattr('social_feed.decor_store.get_decor_store',lambda:None)
    monkeypatch.setattr('social_feed.decor_federation.receive_remote_gifts',fail)
    store=RemoteSocialVisitStore(SimpleNamespace(),Client())
    assert await store.me()=='visitor:k'
    assert store.gifts==[] and store.gift_status=='delivery_pending'


@pytest.mark.asyncio
async def test_sync_pending_and_scope_withdrawal_retried(tmp_path,monkeypatch):
    from social_feed import decor_federation as f
    from social_feed.decor_models import SyncChoice
    from social_feed.remote_sites import RemoteSiteError
    store=DecorStore(tmp_path/'decor.db')
    site=SimpleNamespace(id='person_a',name='示例家庭',origin='https://person_a.example',human_key='mock',ai_key='mock',enabled=True)
    monkeypatch.setattr(f,'get_remote_site_store',lambda:SimpleNamespace(list=lambda:[site]))
    fail=True;calls=[]
    class Client:
        async def request(self,site,kind,method,path,**kwargs):
            if fail:raise RemoteSiteError('social_site_unavailable')
            if path.endswith('/me'):return {'actor':{'actor_id':'visitor:h'},'can_manage_avatar':True}
            if path.endswith('/sync'):
                calls.append(kwargs['payload']['design']['frame'])
                proof=store.proof(kwargs['payload']['token']);assert proof['operation']=='decor_sync'
            return {'ok':True}
    monkeypatch.setattr(f,'RemoteSocialClient',Client)
    store.save_personal('aning',PersonalDesign(frame='orbit'))
    store.choice('aning',SyncChoice(scope='all'))
    assert (await f.sync_personal(store,'aning','https://home.example'))[0]['status']=='pending'
    fail=False
    assert (await f.sync_personal(store,'aning','https://home.example',enqueue=False))[0]['status']=='success'
    assert calls==['orbit'] and not store.sync_jobs('aning')
    old=store.choice('aning');store.choice('aning',SyncChoice(scope='local'));fail=True
    await f.sync_personal(store,'aning','https://home.example',previous=old)
    fail=False;await f.sync_personal(store,'aning','https://home.example',enqueue=False)
    assert calls==['orbit','none'] and not store.sync_jobs('aning')
