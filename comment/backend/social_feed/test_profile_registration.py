"""All accounts, Keys, walls and profile sources here are isolated test fixtures."""
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from .public_wall import PublicWall, PublicWallError
from .profile_identity import ProfileIdentityStore
from .profile_registration import ProfileRegistrationStore
from .profile_transport import accept, refresh


HOME = 'https://social.example.invalid'
SOURCE = 'https://profile.example'
LOCAL = {'choice': 'local'}
EXISTING = {'choice': 'existing', 'origin': SOURCE}


def wall_fixture(tmp_path, name='wall'):
    wall = PublicWall(tmp_path / (name + '.db'))
    wall.initialize()
    identity = ProfileIdentityStore(wall)
    identity.migrate(backup_ready=True)
    registration = ProfileRegistrationStore(wall)
    registration.migrate(backup_ready=True)
    return wall, identity, registration


def snapshot(kind='human'):
    return {'origin': SOURCE, 'profile_id': 'a' * 32, 'kind': kind,
            'nickname': '同一个公开网名', 'avatar_data': '', 'version': 1}


def test_explicit_migration_no_backfill(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    wall.register_human_name('legacy', '登记名')
    store = ProfileRegistrationStore(wall)
    assert not store.ready() and store.state('visitor:legacy')['state'] == 'unconfirmed'
    with pytest.raises(PublicWallError, match='backup_required'): store.migrate()
    ProfileIdentityStore(wall).migrate(backup_ready=True)
    store.migrate(backup_ready=True); store.migrate(backup_ready=True)
    with wall._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM profile_registration_sources').fetchone()[0] == 0
    wall.set_profile('visitor:legacy', '旧网名')
    wall.create_moment('visitor:legacy', '旧账号仍可使用')
    assert wall.registered_name('legacy') == '登记名'


def test_atomic_human_enrollment_and_locked_source(tmp_path):
    wall, identity, store = wall_fixture(tmp_path)
    with pytest.raises(PublicWallError, match='source_required'):
        wall.register_human_name('h', '人', profile_origin=HOME)
    assert not wall.registered_name('h')
    wall.register_human_name('h', '人', profile_source=EXISTING, profile_origin=HOME)
    assert store.state('visitor:h')['state'] == 'pending'
    # Retry/add-AI registration never promotes an already registered identity.
    wall.register_human_name('h', '人', profile_source=LOCAL, profile_origin=HOME)
    assert store.state('visitor:h')['state'] == 'pending'
    with pytest.raises(PublicWallError, match='source_locked'):
        store.select('visitor:h', LOCAL, HOME)
    for operation in (lambda: wall.set_profile('visitor:h', '第二个网名'),
                      lambda: wall.create_moment('visitor:h', '发帖'),
                      lambda: identity.snapshot('visitor:h', 'human', HOME)):
        with pytest.raises(PublicWallError, match='verification_pending'): operation()
    with wall._connect() as db:
        assert not db.execute('SELECT 1 FROM profile_identity_ids WHERE actor=?', ('visitor:h',)).fetchone()
        assert not db.execute('SELECT 1 FROM profile_identity_links').fetchone()


def test_multimachine_all_or_nothing_each_source_separate(tmp_path):
    wall, _, store = wall_fixture(tmp_path)
    with pytest.raises(PublicWallError, match='source_required'):
        wall.link_households('h', [('a', '甲'), ('b', '乙')], human_name='人',
            profile_sources={'h': LOCAL, 'a': EXISTING}, profile_origin=HOME)
    assert not wall.household_links() and not wall.registered_name('h')
    with wall._connect() as db:
        assert not db.execute('SELECT 1 FROM profile_registration_sources').fetchone()
    wall.link_households('h', [('a', '甲'), ('b', '乙')], human_name='人',
        profile_sources={'h': LOCAL, 'a': EXISTING, 'b': LOCAL}, profile_origin=HOME)
    assert store.state('visitor:h')['state'] == store.state('visitor:b')['state'] == 'local'
    assert store.state('visitor:a')['state'] == 'pending'


def test_pending_guards_all_public_mutations_and_guardian(tmp_path):
    wall, _, store = wall_fixture(tmp_path)
    wall.link_households('h', [('a', '机')], human_name='人',
        profile_sources={'h': LOCAL, 'a': EXISTING}, profile_origin=HOME)
    moment = wall.create_moment('k', '可浏览')
    for operation in (lambda: wall.add_comment('visitor:a', moment['id'], '评论'),
                      lambda: wall.toggle_like('visitor:a', moment['id']),
                      lambda: wall.ensure_like('visitor:a', moment['id']),
                      lambda: wall.set_like('visitor:a', moment['id'], True)):
        with pytest.raises(PublicWallError, match='verification_pending'): operation()
    # A guardian cannot edit the pending machine's formal profile either.
    with pytest.raises(PublicWallError, match='verification_pending'):
        wall.set_profile('visitor:a', '人代改也不行')
    assert wall.get_moment(moment['id'])['content'] == '可浏览'
    assert store.state('aning')['can_interact'] and store.state('k')['can_interact']


@pytest.mark.asyncio
async def test_pending_offline_then_verified_cache_offline_and_detach(tmp_path):
    wall, identity, store = wall_fixture(tmp_path)
    wall.register_human_name('h', '人', profile_source=EXISTING, profile_origin=HOME)
    source, si, _ = wall_fixture(tmp_path, 'source')
    source.register_human_name('h', '人', profile_source=LOCAL, profile_origin=SOURCE)
    si.set_enabled(True); source.set_profile('visitor:h', '源站网名')
    class Client:
        offline = True
        async def request(self, origin, operation, body):
            assert origin == SOURCE
            if self.offline: raise PublicWallError('profile_source_unavailable')
            row = si.claim(body['ticket'], body['audience'], body['consumer']) if operation == 'claim' else si.authorization(body['token'])
            return {'audience': row['audience'], 'consumer': row['consumer'],
                    'token': row['token'] if operation == 'claim' else body['token'],
                    'profile': si.snapshot(row['actor'], row['kind'], SOURCE)}
    client = Client()
    code = si.make_grant('visitor:h', 'human', 'visitor:h', 'fixture-key', HOME, SOURCE)['code']
    with pytest.raises(PublicWallError, match='source_unavailable'):
        await accept(wall, 'visitor:h', 'human', code, HOME, client)
    assert store.state('visitor:h')['state'] == 'pending'
    client.offline = False
    await accept(wall, 'visitor:h', 'human', code, HOME, client)
    assert store.state('visitor:h')['state'] == 'linked'
    assert wall.profile('visitor:h')['nickname'] == '源站网名'
    client.offline = True
    assert (await refresh(wall, 'visitor:h', HOME, client))['status'] == 'offline'
    wall.create_moment('visitor:h', '缓存仍可互动')
    assert store.state('visitor:h')['can_interact']
    with pytest.raises(PublicWallError, match='edit_at_source'):
        wall.set_profile('visitor:h', '缓存不能变独立资料')
    identity.detach('visitor:h')
    assert store.state('visitor:h')['state'] == 'pending'
    assert store.state('visitor:h')['origin'] == SOURCE


def test_expected_source_and_active_authority_guard(tmp_path):
    wall, identity, store = wall_fixture(tmp_path)
    wall.register_human_name('h', '人', profile_source=EXISTING, profile_origin=HOME)
    with pytest.raises(PublicWallError, match='source_locked'):
        identity.cache('visitor:h', 'https://other.example', 'human', 'x'*43,
                       snapshot() | {'origin': 'https://other.example'})
    wall.register_human_name('source', '源', profile_source=LOCAL, profile_origin=HOME)
    with pytest.raises(PublicWallError, match='source_locked'):
        identity.cache('visitor:source', SOURCE, 'human', 'x'*43, snapshot())
    for origin in ('http://profile.example', 'https://127.0.0.1', HOME):
        with pytest.raises(PublicWallError):
            store.select('visitor:other', {'choice': 'existing', 'origin': origin}, HOME)


def runtime_fixture(tmp_path, monkeypatch):
    wall, identity, registration = wall_fixture(tmp_path)
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    monkeypatch.setattr('social_feed.public_gateway._receive_login_gift', lambda *_: None)
    monkeypatch.setattr('lounge_visits.cognition_profiles.get', lambda _: {})
    monkeypatch.setattr('cognition.other_book.entities', lambda: {})
    path = tmp_path/'keys.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE visitors(id TEXT PRIMARY KEY,status TEXT,visitor_kind TEXT)')
        db.execute('CREATE TABLE visitor_keys(id TEXT PRIMARY KEY,visitor_id TEXT,revoked_at TEXT)')
        for vid, kind in [('h','human'),('a','external_ai'),('b','external_ai')]:
            db.execute('INSERT INTO visitors VALUES(?,?,?)', (vid, 'active', kind))
            db.execute('INSERT INTO visitor_keys VALUES(?,?,NULL)', ('key-'+vid,vid))
    class Keys:
        def authenticate_identity(self, key):
            return next((('key-'+vid,vid) for vid in ('h','a','b') if key == 'fixture-secret-only-'+vid), None)
        authenticate_bearer_identity = authenticate_identity
    def visitor(vid):
        with sqlite3.connect(path) as db:
            row = db.execute('SELECT status,visitor_kind FROM visitors WHERE id=?', (vid,)).fetchone()
        return SimpleNamespace(status=row[0], visitor_kind=row[1],display_name='测试身份')
    runtime = SimpleNamespace(keys=Keys(), database=SimpleNamespace(connection=lambda: sqlite3.connect(path)),
        visitor_service=SimpleNamespace(effective_visitor=visitor),visitors=SimpleNamespace(visitor=visitor))
    return wall, identity, registration, runtime


@pytest.mark.asyncio
async def test_http_registration_pending_csrf_profile_transition_and_mcp(tmp_path, monkeypatch):
    from .public_gateway import create_public_social_app
    from .public_mcp import register_public_social_tools
    wall, identity, registration, runtime = runtime_fixture(tmp_path, monkeypatch)
    from .decor_store import get_decor_store
    decor = get_decor_store()  # Derived from the already isolated temporary PublicWall.
    async def no_delivery(*args,**kwargs):return 0
    monkeypatch.setattr('social_feed.decor_events.publish_pending',no_delivery)
    app = create_public_social_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=HOME) as client:
        payload = {'human_name':'人', 'human_key':'fixture-secret-only-h'}
        assert (await client.post('/social/v1/human/register',json=payload)).status_code == 400
        result = await client.post('/social/v1/human/register',json=payload | {'profile_source':EXISTING})
        assert result.status_code == 200
        assert result.json()['actor']['name'] == '资料待验证'
        assert (await client.get('/social/v1/moments')).status_code == 200
        assert 'post' not in (await client.get('/social/v1/me')).json()['capabilities']
        headers = {'X-MIRROW-CSRF': client.cookies['mirrow_wall_csrf']}
        # Automatic arrival/collection are passive exceptions, not a way to post
        # or edit formal profiles. Their CSRF and household authorization remain.
        assert (await client.post('/social/v1/decor/visit',json={})).json()['detail']=='csrf_required'
        assert (await client.post('/social/v1/decor/visit',json={},headers=headers)).status_code==200
        assert (await client.get('/social/v1/decor/collection')).status_code==200
        assert (await client.put('/social/v1/decor/people/visitor:h',json={'frame':'orbit'},headers=headers)).status_code==403
        assert (await client.post('/social/v1/moments',json={'content':'禁止独立发帖'},headers=headers)).status_code == 403
        assert (await client.put('/social/v1/me/profile',json={'nickname':'另一个'},headers=headers)).status_code == 403
        source_path='/social/v1/profile-links/people/visitor:h/source'
        assert (await client.put(source_path,json=EXISTING)).json()['detail'] == 'csrf_required'
        assert (await client.put(source_path,json=EXISTING,headers=headers)).status_code == 200
        assert (await client.post('/social/v1/profile-links/people/visitor:h/link',json={'code':'x'*80},headers=headers)).json()['detail'] == 'profile_ticket_invalid'
        assert (await client.get('/social/v1/profile-links/state')).status_code == 200
        assert (await client.get('/social/v1/profile-links/ui.js')).headers['cache-control'] == 'no-store'
        # A pending human can still register/switch a properly linked machine.
        household = {'human_name':'人','human_key':'fixture-secret-only-h','ais':[
            {'name':'机','key':'fixture-secret-only-a','profile_source':LOCAL}]}
        assert (await client.post('/social/v1/household/register',json=household)).status_code == 200
        headers = {'X-MIRROW-CSRF': client.cookies['mirrow_wall_csrf']}
        assert (await client.post('/social/v1/me/switch',json={'target':'visitor:a','key':'fixture-secret-only-a'},headers=headers)).status_code == 200
    class Server:
        def __init__(self): self.tools = {}
        def tool(self):
            def register(fn): self.tools[fn.__name__] = fn; return fn
            return register
    monkeypatch.setattr('social_feed.public_mcp.require_visitor_id', lambda: 'h')
    server = Server(); register_public_social_tools(server, runtime)
    assert 'post' not in server.tools['public_social_capabilities']()['capabilities']
    assert server.tools['get_public_social_profile']()['status'] == 'ok'
    assert server.tools['set_public_social_nickname']('不能另建')['reason'] == 'profile_verification_pending'


def test_shared_ui_registration_contracts():
    from pathlib import Path
    base = Path(__file__).parent
    ui = (base/'profile_link_ui.js').read_text(encoding='utf-8')
    page = (base/'public_wall.js').read_text(encoding='utf-8')
    assert '新共域可以先登记和使用本站资料' not in ui
    assert 'registration.state === \'pending\'' in ui
    assert "'/source','PUT'" in ui
    assert 'human_source: registrationSource' in page
    assert 'profile_source: registrationSource' in page
    root = base.parent.parent
    assert 'SocialProfileLinkPanel' not in (root/'ai-chat-frontend/src/pages/SocialFeedPage.tsx').read_text(encoding='utf-8')
    assert 'SocialProfileLinkPanel' in (root/'ai-chat-frontend/src/components/SocialIdentityManager.tsx').read_text(encoding='utf-8')
