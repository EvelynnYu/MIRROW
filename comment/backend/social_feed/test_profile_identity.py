"""No real home, Key, database or network: two unrelated profile-custody hosts."""
import asyncio
import base64
import io
import json
import secrets
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from .public_wall import PublicWall, PublicWallError
from .profile_identity import ProfileIdentityStore, decode_code
from .profile_transport import ProfileTransport, accept, refresh


def make(tmp_path,name):
    wall=PublicWall(tmp_path/(name+'.db'));wall.initialize()
    store=ProfileIdentityStore(wall);store.migrate(backup_ready=True)
    store.set_enabled(True)
    wall.register_human_name('a','PRIVATE canonical name')
    wall.note_contact('a');wall.set_profile('visitor:a','A公开网名')
    return wall,store


def test_schema_gate_and_read_do_not_migrate(tmp_path):
    wall=PublicWall(tmp_path/'only.db');wall.initialize();store=ProfileIdentityStore(wall)
    assert not store.ready() and not store.enabled() and store.link('k') is None
    assert wall.profile('k')['nickname']==''
    with pytest.raises(PublicWallError,match='profile_backup_required'):store.migrate()
    with wall._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'profile_identity_%'").fetchone()[0]==0
    store.migrate(backup_ready=True);store.migrate(backup_ready=True)
    assert store.ready() and not store.enabled()


def test_grant_audience_consumer_expiry_and_profile_only(tmp_path):
    wall,s=make(tmp_path,'b');b='https://b.example';c='https://c.example'
    with pytest.raises(PublicWallError,match='profile_edit_at_source'):
        # A profile projection cannot issue grants as a second authority.
        s.cache('k','https://original.example','ai','x'*43,{'origin':'https://original.example','kind':'ai','nickname':'机','avatar_data':'','version':1,'profile_id':'1'*32})
        s.make_grant('k','ai','aning','__owner__',c,b)
    code=s.make_grant('visitor:a','human','visitor:a','key-id',c,b)['code']
    origin,ticket=decode_code(code);assert origin==b
    with pytest.raises(PublicWallError,match='ticket_invalid'):s.claim(ticket,'https://other.example','visitor:a')
    row=s.claim(ticket,c,'visitor:cA')
    with pytest.raises(PublicWallError,match='ticket_invalid'):s.claim(ticket,c,'visitor:cA')
    with pytest.raises(PublicWallError,match='ticket_invalid'):s.claim(ticket,c,'visitor:cB')
    snapshot=s.snapshot(row['actor'],row['kind'],b)
    assert set(snapshot)=={'kind','nickname','avatar_data','origin','profile_id','version'}
    assert 'PRIVATE' not in json.dumps(snapshot) and 'key-id' not in json.dumps(snapshot)
    assert s.authorization(row['token'])['actor']=='visitor:a'
    s.revoke('visitor:a',row['id'])
    with pytest.raises(PublicWallError,match='profile_access_revoked'):s.authorization(row['token'])
    with wall._connect() as db:
        db.execute('UPDATE profile_identity_grants SET ticket_expires=0')
    with pytest.raises(PublicWallError,match='ticket_invalid'):s.claim(ticket,c,'visitor:cA')


@pytest.mark.asyncio
async def test_b_and_c_need_no_friendship_sync_offline_and_revoke(tmp_path,monkeypatch):
    b,sb=make(tmp_path,'b');c,sc=make(tmp_path,'c')
    bo,co='https://b.example','https://c.example'
    class Client:
        offline=False
        async def request(self,origin,operation,body):
            assert origin==bo
            if self.offline:raise PublicWallError('profile_source_unavailable')
            row=sb.claim(body['ticket'],body['audience'],body['consumer']) if operation=='claim' else sb.authorization(body['token'])
            value={'audience':row['audience'],'consumer':row['consumer'],'profile':sb.snapshot(row['actor'],row['kind'],bo)}
            if operation=='claim':value['token']=row['token']
            return value
    client=Client()
    code=sb.make_grant('visitor:a','human','visitor:a','b-key',co,bo)['code']
    status=await accept(c,'visitor:a','human',code,co,client)
    assert status['linked'] and status['identity_id'].startswith(bo+'#')
    assert sb.source_identity('visitor:a',bo)['identity_id']==status['identity_id']
    assert sc.status('visitor:a')==status
    # No network friendship, household mapping, post or local registered name changes.
    assert c.registered_name('a')=='PRIVATE canonical name' and c.contacts()[0]['visitor_id']=='a'
    with c._connect() as db:
        encrypted=db.execute('SELECT encrypted_token FROM profile_identity_links').fetchone()[0]
        assert encrypted.startswith('dpapi:')
    with pytest.raises(PublicWallError,match='profile_edit_at_source'):c.set_profile('visitor:a','fork')
    b.set_profile('visitor:a','新的公开网名')
    result=await refresh(c,'visitor:a',co,client)
    assert result['version']>status['version'] and c.profile('visitor:a')['nickname']=='新的公开网名'
    client.offline=True
    assert (await refresh(c,'visitor:a',co,client))['status']=='offline'
    assert c.profile('visitor:a')['nickname']=='新的公开网名'
    client.offline=False
    sb.revoke('visitor:a',sb.grants('visitor:a')[0]['id'])
    assert (await refresh(c,'visitor:a',co,client))['status']=='revoked'
    assert c.profile('visitor:a')['profile_identity']['identity_id']==status['identity_id']
    sc.detach('visitor:a');c.set_profile('visitor:a','本地恢复')


def test_avatar_copy_version_regression_and_duplicate_claim(tmp_path,monkeypatch):
    from . import avatar_images
    monkeypatch.setattr(avatar_images,'AVATAR_DIR',tmp_path/'avatars')
    wall,store=make(tmp_path,'b');origin='https://b.example'
    out=io.BytesIO();Image.new('RGB',(400,400),'pink').save(out,format='JPEG',exif=b'private-camera')
    path=avatar_images.save_avatar('visitor:a',out.getvalue())
    wall.set_profile('visitor:a',avatar=origin+'/social/v1/avatars/'+path.name+'?v=1')
    snap=store.snapshot('visitor:a','human',origin)
    assert b'private-camera' not in base64.b64decode(snap['avatar_data'])
    c,cs=make(tmp_path,'c');token=secrets.token_urlsafe(32)
    cs.cache('visitor:a',origin,'human',token,snap,public_origin='https://c.example')
    assert c.profile('visitor:a')['avatar'].startswith('https://c.example/social/v1/profile-links/avatars/')
    with pytest.raises(PublicWallError,match='profile_identity_already_linked'):cs.cache('visitor:other',origin,'human',token,snap)
    cs.cache('visitor:a',origin,'human',token,snap|{'version':2},public_origin='https://c.example')
    with pytest.raises(PublicWallError,match='profile_version_regressed'):cs.cache('visitor:a',origin,'human',token,snap)
    with pytest.raises(PublicWallError,match='profile_response_invalid'):cs.cache('k',origin,'ai',token,snap)
    wall.set_profile('visitor:a',avatar='https://outside.example/private.png')
    with pytest.raises(PublicWallError,match='profile_uploaded_avatar_required'):store.snapshot('visitor:a','human',origin)


def test_failed_credential_validation_does_not_consume_code(tmp_path):
    _,store=make(tmp_path,'validation')
    _,ticket=decode_code(store.make_grant('visitor:a','human','visitor:a','key-id','https://c.example','https://b.example')['code'])
    def rejected(_row):raise PublicWallError('profile_access_revoked')
    with pytest.raises(PublicWallError,match='profile_access_revoked'):
        store.claim(ticket,'https://c.example','visitor:a',validate=rejected)
    assert store.claim(ticket,'https://c.example','visitor:a')['consumer']=='visitor:a'


@pytest.mark.asyncio
async def test_profile_source_suspended_and_rotated_key_revoke_reads(tmp_path,monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    from fastapi import FastAPI
    from .profile_router import create_profile_router,install_profile_errors
    wall,store=make(tmp_path,'revoked')
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    db_path=tmp_path/'credentials.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE visitor_keys(id TEXT,visitor_id TEXT,revoked_at REAL)')
        db.execute("INSERT INTO visitor_keys VALUES('key-id','a',NULL)")
    @contextmanager
    def connection():
        with sqlite3.connect(db_path) as db:yield db
    record=SimpleNamespace(visitor_kind='human',status='active')
    runtime=SimpleNamespace(database=SimpleNamespace(connection=connection),visitor_service=SimpleNamespace(effective_visitor=lambda vid:record))
    app=FastAPI();app.include_router(create_profile_router(runtime));install_profile_errors(app)
    _,ticket=decode_code(store.make_grant('visitor:a','human','visitor:a','key-id','https://c.example','https://b.example')['code'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://b.example') as client:
        record.status='suspended'
        payload={'ticket':ticket,'audience':'https://c.example','consumer':'visitor:a'}
        assert (await client.post('/social/v1/profile-links/claim',json=payload)).status_code==403
        record.status='active'
        claimed=await client.post('/social/v1/profile-links/claim',json=payload)
        assert claimed.status_code==200
        token=claimed.json()['token']
        assert (await client.post('/social/v1/profile-links/read',json={'token':token})).status_code==200
        record.status='suspended'
        assert (await client.post('/social/v1/profile-links/read',json={'token':token})).status_code==403
        record.status='active'
        with connection() as db:db.execute('UPDATE visitor_keys SET revoked_at=1')
        assert (await client.post('/social/v1/profile-links/read',json={'token':token})).status_code==403


@pytest.mark.asyncio
async def test_profile_network_pins_no_redirect_and_no_visitor_key(monkeypatch):
    from . import profile_transport as t
    seen=[]
    async def destination(url):return SimpleNamespace()
    def pin(request,*args):seen.append(request)
    monkeypatch.setattr(t,'public_destination',destination);monkeypatch.setattr(t,'pin_request',pin)
    def serve(request):
        assert request.url.path=='/social/v1/profile-links/read'
        assert 'authorization' not in request.headers and request.url.query==b''
        return httpx.Response(302,headers={'Location':'http://127.0.0.1/private'})
    client=ProfileTransport(httpx.MockTransport(serve))
    with pytest.raises(PublicWallError,match='profile_source_unavailable'):
        await client.request('https://b.example','read',{'token':'x'*43})
    assert len(seen)==1
    with pytest.raises(PublicWallError):await client.request('http://127.0.0.1','read',{})
    with pytest.raises(PublicWallError):await client.request('https://b.example','post',{})


@pytest.mark.asyncio
async def test_public_routes_owner_consent_machine_management_and_no_wall_login(tmp_path,monkeypatch):
    wall,store=make(tmp_path,'routes')
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    monkeypatch.setattr('social_feed.public_gateway._identity',lambda runtime,request,write=False,admission=True:request.headers.get('x-test-actor','visitor:h'))
    monkeypatch.setattr('social_feed.public_gateway._display',lambda runtime,actor,viewer:{'name':actor})
    monkeypatch.setattr('social_feed.public_gateway._managed_profiles',lambda runtime,actor:['aning','k'] if actor=='aning' else ['visitor:h','visitor:a'] if actor=='visitor:h' else [actor])
    runtime=SimpleNamespace(visitor_service=SimpleNamespace(effective_visitor=lambda vid:SimpleNamespace(visitor_kind='human' if vid=='h' else 'external_ai')))
    from .profile_router import create_profile_router,install_profile_errors
    from fastapi import FastAPI
    app=FastAPI();app.include_router(create_profile_router(runtime));install_profile_errors(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        assert (await client.put('/social/v1/profile-links/policy',json={'enabled':False})).status_code==403
        assert (await client.put('/social/v1/profile-links/policy',json={'enabled':False},headers={'x-test-actor':'aning'})).status_code==200
        assert (await client.post('/social/v1/profile-links/people/visitor:other/link',json={'code':'x'*80})).status_code==403
        assert (await client.post('/social/v1/profile-links/people/visitor:a/link',json={'code':'x'*80},headers={'x-test-actor':'visitor:a'})).status_code==403
        data=(await client.get('/social/v1/profile-links/state')).json()
        assert data['owner'] is False and not data['hosting_enabled']
        assert 'token' not in json.dumps(data) and 'key' not in json.dumps(data)
        # Tokens are not a new login credential or unrestricted profile API.
        assert (await client.post('/social/v1/profile-links/read',json={'token':'x'*43})).status_code==403
