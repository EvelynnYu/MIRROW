import asyncio
import base64
import sqlite3
from io import BytesIO
from types import SimpleNamespace

import httpx
from PIL import Image

from social_feed.public_gateway import _display, create_public_social_app
from social_feed.owner_access import issue_owner_ticket
from social_feed.public_mcp import register_public_social_tools
from social_feed.public_wall import PublicWall
from lounge_reception.gateway import GatewayBoundary


def test_identity_card_uses_viewer_remark_not_private_other_book_name(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    wall.note_contact('v1'); wall.note_contact('v2')
    wall.set_profile('visitor:v1', '人乙', '')
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    monkeypatch.setattr('lounge_visits.cognition_profiles.get',
                        lambda _connection: {'primary_entity_id': 'private-person'})
    monkeypatch.setattr('cognition.other_book.entities',
                        lambda: {'private-person': {'name': '人丙'}})
    runtime = SimpleNamespace(visitors=SimpleNamespace(visitor=lambda _vid: SimpleNamespace(display_name='人丙')))
    home = _display(runtime, 'visitor:v1', viewer='aning')
    friend = _display(runtime, 'visitor:v1', viewer='visitor:v2')
    assert home['name'] == '人乙（人丙）'
    assert home['cognition_bound'] is True
    assert home['remark_locked'] is True
    assert friend['name'] == '人乙' and 'cognition_bound' not in friend
    assert friend['canonical_name'] == ''
    wall.set_remark('visitor:v2', 'visitor:v1', '我的朋友')
    assert _display(runtime, 'visitor:v1', viewer='visitor:v2')['name'] == '人乙（我的朋友）'
    assert _display(runtime, 'visitor:v1', viewer='aning')['name'] == '人乙（人丙）'


def test_key_login_without_cognition_binding_revocation_and_public_only(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    monkeypatch.setattr('social_feed.avatar_images.AVATAR_DIR', tmp_path / 'avatars')
    image_buffer = BytesIO()
    Image.new('RGB', (400, 400), 'coral').save(image_buffer, format='JPEG', exif=b'Private camera metadata')
    image_bytes = image_buffer.getvalue()
    owner_avatar = 'data:image/jpeg;base64,' + base64.b64encode(image_bytes).decode()
    monkeypatch.setattr('settings_manager.get_setting', lambda key:
                        owner_avatar if key == 'userAvatar' else
                        {'avatar': owner_avatar} if key == 'aiPersona' else None)
    binding = {'primary_entity_id': ''}
    monkeypatch.setattr('lounge_visits.cognition_profiles.get',
                        lambda _connection: binding)
    monkeypatch.setattr('cognition.other_book.entities',
                        lambda: {'person_a': {'name': '人甲'}})

    db_path = tmp_path / 'reception.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE visitors(id TEXT PRIMARY KEY,status TEXT,visitor_kind TEXT)')
        db.execute('CREATE TABLE visitor_keys(id TEXT PRIMARY KEY,visitor_id TEXT,revoked_at TEXT)')
        db.execute("INSERT INTO visitors VALUES('v1','active','human')")
        db.execute("INSERT INTO visitors VALUES('v2','active','human')")
        db.execute("INSERT INTO visitor_keys VALUES('key1','v1',NULL)")
        db.execute("INSERT INTO visitor_keys VALUES('key2','v2',NULL)")

    class Keys:
        def authenticate_identity(self, value):
            return {'test-only-key-12345678901234567890': ('key1', 'v1'),
                    'second-test-only-key-12345678901234567890': ('key2', 'v2')}.get(value)
        authenticate_bearer_identity = authenticate_identity

    class Database:
        def connection(self):
            return sqlite3.connect(db_path)

    def effective_visitor(_vid):
        with sqlite3.connect(db_path) as db:
            row = db.execute('SELECT status,visitor_kind FROM visitors WHERE id=?', (_vid,)).fetchone()
            return SimpleNamespace(status=row[0], visitor_kind=row[1])

    runtime = SimpleNamespace(
        keys=Keys(), database=Database(),
        visitor_service=SimpleNamespace(effective_visitor=effective_visitor),
        visitors=SimpleNamespace(visitor=lambda _vid: SimpleNamespace(display_name='人甲')),
    )
    app = create_public_social_app(runtime)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as client:
            assert (await client.get('/social/v1/me', headers={'Host':'visitor.example.invalid'})).status_code == 403
            assert (await client.post('/social/v1/login', json={'key':'irrelevant'},
                                      headers={'Origin':'https://attacker.example'})).status_code == 403
            assert (await client.get('/social/v1/moments')).status_code == 401
            assert (await client.get('/social/v1/main-avatar/aning')).status_code == 404
            unbound = await client.post('/social/v1/login', json={'key': 'test-only-key-12345678901234567890'})
            assert unbound.status_code == 403 and unbound.json()['detail']=='identity_registration_required'
            assert wall.contacts()==[]
            unbound = await client.post('/social/v1/human/register',json={'human_name':'测试朋友','human_key':'test-only-key-12345678901234567890'})
            wall.set_profile('visitor:v1','好友','')
            wall.register_human_name('v2','另一位测试朋友'); wall.set_profile('visitor:v2','好友','')
            assert unbound.status_code == 200
            assert unbound.json()['actor']['verified'] is True
            assert unbound.json()['actor']['canonical_name'] == ''
            assert [item['visitor_id'] for item in wall.contacts()] == ['v1']
            # A still-valid pre-upgrade browser session also registers on its next use.
            with wall._connect() as db:
                db.execute('DELETE FROM wall_contacts WHERE visitor_id=?', ('v1',))
            assert (await client.get('/social/v1/moments')).status_code == 200
            assert [item['visitor_id'] for item in wall.contacts()] == ['v1']
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE visitors SET status='suspended' WHERE id='v1'")
            response = await client.post('/social/v1/login', json={'key': 'test-only-key-12345678901234567890'})
            assert response.status_code == 200 and 'mirrow_wall_session' in client.cookies
            binding['primary_entity_id'] = 'person_a'
            csrf = client.cookies['mirrow_wall_csrf']
            assert (await client.post('/social/v1/moments', json={'content':'hello'})).status_code == 403
            created = await client.post('/social/v1/moments', json={'content':'hello', 'request_id':'test-post-001'},
                                        headers={'X-MIRROW-CSRF':csrf})
            assert created.status_code == 200, created.text
            assert created.json()['hosting_mode'] == 'hosted'
            assert 'post' in (await client.get('/social/v1/me')).json()['capabilities']
            identifier = created.json()['id']
            repeated = await client.post('/social/v1/moments',
                                         json={'content':'hello', 'request_id':'test-post-001'},
                                         headers={'X-MIRROW-CSRF':csrf})
            assert repeated.status_code == 200 and repeated.json()['id'] == identifier
            listed = await client.get('/social/v1/moments')
            assert [row['id'] for row in listed.json()['items']] == [identifier]
            matched = await client.get('/social/v1/moments/search', params={'q':'hello'})
            assert matched.status_code == 200 and matched.json()['items'][0]['id'] == identifier
            assert (await client.get('/social/v1/moments/search', params={'q':'missing'})).json()['items'] == []
            assert listed.json()['items'][0]['people']['visitor:v1']['name'] == '好友'
            missing_csrf = await client.post('/social/v1/me/avatar', content=image_bytes,
                                             headers={'Content-Type': 'image/jpeg'})
            assert missing_csrf.status_code == 403
            bad_image = await client.post('/social/v1/me/avatar', content=b'<svg/>',
                                          headers={'Content-Type': 'image/jpeg', 'X-MIRROW-CSRF': csrf})
            assert bad_image.status_code == 415
            upload = await client.post('/social/v1/me/avatar', content=image_bytes,
                                       headers={'Content-Type': 'image/jpeg', 'X-MIRROW-CSRF': csrf})
            assert upload.status_code == 200, upload.text
            uploaded_url = upload.json()['actor']['avatar']
            avatar_image = await client.get(uploaded_url)
            assert avatar_image.status_code == 200
            assert avatar_image.headers['content-type'] == 'image/png'
            assert b'Private camera metadata' not in avatar_image.content
            assert Image.open(BytesIO(avatar_image.content)).size == (256, 256)
            first_comment = await client.post(f'/social/v1/moments/{identifier}/comments',
                                              json={'content': 'first', 'request_id':'test-comment-001'}, headers={'X-MIRROW-CSRF': csrf})
            assert first_comment.status_code == 200
            same_comment = await client.post(f'/social/v1/moments/{identifier}/comments',
                                             json={'content': 'first', 'request_id':'test-comment-001'}, headers={'X-MIRROW-CSRF': csrf})
            assert same_comment.status_code == 200 and same_comment.json()['id'] == first_comment.json()['id']
            reply = await client.post(f'/social/v1/moments/{identifier}/comments',
                                      json={'content': 'reply', 'reply_to_id': first_comment.json()['id']},
                                      headers={'X-MIRROW-CSRF': csrf})
            assert reply.status_code == 200
            assert (await client.get(f'/social/v1/moments/{identifier}')).json()['comments'][-1]['reply_to_id'] == first_comment.json()['id']
            delete_path = f"/social/v1/moments/{identifier}/comments/{first_comment.json()['id']}"
            assert (await client.delete(delete_path)).status_code == 403
            assert (await client.delete(delete_path, headers={'X-MIRROW-CSRF': csrf})).status_code == 200
            assert (await client.get(f'/social/v1/moments/{identifier}')).json()['comments'][0]['reply_to_id'] is None
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE visitors SET status='paused' WHERE id='v1'")
            assert (await client.get('/social/v1/moments')).status_code == 401
            assert (await client.post('/social/v1/login', json={'key': 'test-only-key-12345678901234567890'})).status_code == 403
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE visitors SET status='suspended' WHERE id='v1'")
            binding['primary_entity_id'] = ''
            assert (await client.get('/social/v1/moments')).status_code == 200
            binding['primary_entity_id'] = 'person_a'
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE visitor_keys SET revoked_at='now' WHERE id='key1'")
            assert (await client.get('/social/v1/moments')).status_code == 401
            assert (await client.get(uploaded_url)).status_code == 401

            ticket = issue_owner_ticket()
            owner = await client.post('/social/v1/owner-login', json={'ticket': ticket})
            assert owner.status_code == 200 and owner.json()['actor']['actor_id'] == 'aning'
            assert owner.json()['actor']['avatar'] == ''  # Never inherits main chat/persona settings.
            assert (await client.get('/social/v1/main-avatar/aning')).status_code == 404
            assert (await client.get('/social/v1/me')).json()['actor']['actor_id'] == 'aning'
            assert 'post' in (await client.get('/social/v1/me')).json()['capabilities']
            assert (await client.post('/social/v1/owner-login', json={'ticket': ticket})).status_code == 401
            owner_csrf = client.cookies['mirrow_wall_csrf']
            managed = (await client.get('/social/v1/me/managed-profiles')).json()['profiles']
            assert [item['actor_id'] for item in managed] == ['aning', 'k']
            uploaded = await client.post('/social/v1/people/k/avatar', content=image_bytes,
                                         headers={'Content-Type': 'image/jpeg', 'X-MIRROW-CSRF': owner_csrf})
            assert uploaded.status_code == 200
            assert uploaded.json()['actor']['avatar'].startswith('https://social.example.invalid/social/v1/avatars/')
            assert (await client.put('/social/v1/people/k/profile', json={'nickname': 'AI 的圈名'},
                                     headers={'X-MIRROW-CSRF': owner_csrf})).status_code == 200
            assert wall.profile('k')['avatar'] == uploaded.json()['actor']['avatar']
            assert wall.profile('k')['nickname'] == 'AI 的圈名'
            owner_post = await client.post('/social/v1/moments', json={'content':'站主公开动态'},
                                           headers={'X-MIRROW-CSRF': owner_csrf})
            assert owner_post.status_code == 200 and owner_post.json()['author'] == 'aning'
            note_path = '/social/v1/people/visitor%3Av1/remark'
            assert (await client.put(note_path, json={'remark':'站主给她的备注'})).status_code == 403
            saved_note = await client.put(note_path, json={'remark':'站主给她的备注'},
                                          headers={'X-MIRROW-CSRF': owner_csrf})
            assert saved_note.status_code == 403
            assert saved_note.json()['detail'] == 'remark_locked_by_cognition'
            assert (await client.get('/social/v1/people/visitor%3Av1')).json()['person']['name'] == '好友（人甲）'
            async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as second:
                assert (await second.post('/social/v1/login', json={'key':'second-test-only-key-12345678901234567890'})).status_code == 200
                view = await second.get('/social/v1/people/visitor%3Av1')
                assert view.json()['person']['name'] == '好友'
                assert view.json()['person']['remark'] == ''
                assert 'cognition_bound' not in view.json()['person']
                own_csrf = second.cookies['mirrow_wall_csrf']
                assert (await second.put(note_path, json={'remark':'我自己的备注'},
                                         headers={'X-MIRROW-CSRF':own_csrf})).status_code == 200
                assert (await second.get('/social/v1/people/visitor%3Av1')).json()['person']['name'] == '好友（我自己的备注）'
                assert (await client.get('/social/v1/people/visitor%3Av1')).json()['person']['name'] == '好友（人甲）'
            owner_delete = f"/social/v1/moments/{identifier}/comments/{reply.json()['id']}"
            assert (await client.delete(owner_delete, headers={'X-MIRROW-CSRF': owner_csrf})).status_code == 200
            hosted_view = (await client.get(f'/social/v1/moments/{identifier}')).json()
            assert hosted_view['can_moderate'] is True and hosted_view['can_manage'] is False
            assert (await client.patch(f'/social/v1/moments/{identifier}',
                json={'content':'站主不应冒写', 'revision':hosted_view['revision']},
                headers={'X-MIRROW-CSRF': owner_csrf})).status_code == 403
            assert (await client.delete(f'/social/v1/moments/{identifier}',
                headers={'X-MIRROW-CSRF': owner_csrf})).json()['reason'] == 'host_removed'
            assert (await client.delete(f'/social/v1/moments/{identifier}',
                headers={'X-MIRROW-CSRF': owner_csrf})).json()['reason'] == 'host_removed'
            removal = (await client.get(f'/social/v1/moments/{identifier}/status')).json()
            assert removal['reason'] == 'host_removed' and removal['status'] == 'withdrawn'
            assert wall.removal_status('visitor:v1', identifier)['reason'] == 'host_removed'
            with sqlite3.connect(wall.path) as db:
                assert db.execute('SELECT author,moderator,action FROM wall_moderation_actions WHERE moment_id=?',
                                  (identifier,)).fetchone() == ('visitor:v1', 'aning', 'host_removed')
            assert (await client.delete(f"/social/v1/moments/{owner_post.json()['id']}",
                headers={'X-MIRROW-CSRF': owner_csrf})).json()['reason'] == 'author_withdrawn'

    asyncio.run(scenario())


def test_gateway_host_partition(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    runtime = SimpleNamespace()
    social = create_public_social_app(runtime)

    async def dummy(_scope, _receive, send):
        await send({'type':'http.response.start', 'status':418, 'headers':[]})
        await send({'type':'http.response.body', 'body':b''})

    boundary = GatewayBoundary(dummy, None, social_app=social)

    async def scenario():
        transport = httpx.ASGITransport(app=boundary)
        async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as client:
            assert (await client.get('/')).status_code == 200
            assert (await client.get('/mcp')).status_code == 404
            assert (await client.post('/social/v1/login', content=b'x' * 8193)).status_code == 413
            # The browser shrinks a <=10 MB source to a <=2 MB PNG before uploading.
            # Its 8 KB+ result must reach the social app instead of the outer gateway's small JSON cap.
            upload = await client.post('/social/v1/me/avatar', content=b'x' * 8193,
                                       headers={'Content-Type': 'image/png'})
            assert upload.status_code in {401, 403} and upload.json().get('detail') != 'request_rejected'
            assert (await client.post('/social/v1/me/avatar', content=b'x' * (2 * 1024 * 1024 + 1),
                                      headers={'Content-Type': 'image/png'})).status_code == 413
            assert (await client.post('/social/v1/people/visitor%3Av1/avatar', content=b'x' * 8193,
                                      headers={'Content-Type': 'image/png'})).json().get('detail') != 'request_rejected'
        async with httpx.AsyncClient(transport=transport, base_url='https://visitor.example.invalid') as client:
            assert (await client.get('/')).status_code == 404
            assert (await client.get('/social/v1/moments')).status_code == 404

    asyncio.run(scenario())


def test_public_mcp_key_grants_access_without_cognition_binding(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    class Server:
        tools = {}
        def tool(self):
            def save(function):
                self.tools[function.__name__] = function
                return function
            return save

    binding = {'primary_entity_id': ''}
    monkeypatch.setattr('social_feed.public_mcp.require_visitor_id', lambda: 'v1')
    monkeypatch.setattr('lounge_visits.cognition_profiles.get', lambda _connection: binding)
    monkeypatch.setattr('cognition.other_book.entities', lambda: {'person_a': {'name': '人甲'}})
    server = Server()
    keys_db = tmp_path / 'keys.db'
    with sqlite3.connect(keys_db) as db:
        db.execute('CREATE TABLE visitor_keys(visitor_id TEXT,revoked_at TEXT)')
        db.execute("INSERT INTO visitor_keys VALUES('human-v1',NULL)")
    class Database:
        def connection(self):
            return sqlite3.connect(keys_db)
    runtime = SimpleNamespace(visitor_service=SimpleNamespace(
        effective_visitor=lambda _vid: SimpleNamespace(status='active', visitor_kind='human')),
        database=Database())
    register_public_social_tools(server, runtime)
    assert server.tools['public_social_capabilities']()['reason']=='identity_registration_required'
    wall.register_human_name('v1','测试机')
    assert server.tools['public_social_capabilities']()['status'] == 'ok'
    assert [item['visitor_id'] for item in wall.contacts()] == ['v1']
    binding['primary_entity_id'] = 'person_a'
    assert server.tools['public_social_capabilities']()['status'] == 'ok'
    runtime.visitor_service.effective_visitor = lambda _vid: SimpleNamespace(status='suspended', visitor_kind='human')
    assert server.tools['public_social_capabilities']()['status'] == 'ok'
    runtime.visitor_service.effective_visitor = lambda _vid: SimpleNamespace(status='paused', visitor_kind='human')
    assert server.tools['public_social_capabilities']()['reason'] == 'identity_unavailable'
    runtime.visitor_service.effective_visitor = lambda _vid: SimpleNamespace(status='active', visitor_kind='external_ai')
    assert server.tools['public_social_capabilities']()['reason'] == 'human_binding_required'
    wall.link_household('human-v1', 'v1')
    runtime.visitor_service.effective_visitor = lambda visitor_id: SimpleNamespace(
        status='active', visitor_kind='human' if visitor_id == 'human-v1' else 'external_ai')
    assert server.tools['public_social_capabilities']()['status'] == 'ok'
    assert 'set_nickname' in server.tools['public_social_capabilities']()['capabilities']
    assert 'post' in server.tools['public_social_capabilities']()['capabilities']
    hosted = server.tools['post_public_social_moment']('寄存在本家', request_id='test-hosted-1')
    assert hosted['author'] == 'visitor:v1'
    assert server.tools['post_public_social_moment']('寄存在本家', request_id='test-hosted-1')['idempotent_replay']
    wall.set_profile('visitor:v1', '旧网名', 'https://example.test/avatar.png')
    changed = server.tools['set_public_social_nickname']('新网名')
    assert changed['status'] == 'ok' and changed['actor']['nickname'] == '新网名'
    assert wall.profile('visitor:v1')['avatar'] == 'https://example.test/avatar.png'
    assert server.tools['set_public_social_profile'](nickname='再换一次')['status'] == 'ok'
    assert wall.profile('visitor:v1')['avatar'] == 'https://example.test/avatar.png'
    assert server.tools['set_public_social_profile'](avatar='https://example.test/ai.png')['reason'] == 'avatar_managed_by_human'
    monkeypatch.setattr('social_feed.public_mcp.require_visitor_id', lambda: 'human-v1')
    wall.register_human_name('human-v1','测试人')
    assert server.tools['set_linked_public_social_profile']('visitor:v1', nickname='人给机改名')['status'] == 'ok'
    assert wall.profile('visitor:v1')['nickname'] == '人给机改名'
    monkeypatch.setattr('social_feed.public_mcp.require_visitor_id', lambda: 'v1')
    with sqlite3.connect(keys_db) as db:
        db.execute("UPDATE visitor_keys SET revoked_at='now' WHERE visitor_id='human-v1'")
    assert server.tools['public_social_capabilities']()['reason'] == 'human_binding_required'
    assert server.tools['set_public_social_nickname']('不该生效')['reason'] == 'human_binding_required'


def test_ai_requires_human_link_for_web_and_mcp_and_pair_can_register(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    monkeypatch.setattr('social_feed.avatar_images.AVATAR_DIR', tmp_path / 'avatars')
    image_buffer = BytesIO()
    Image.new('RGB', (400, 400), 'coral').save(image_buffer, format='JPEG')
    image_bytes = image_buffer.getvalue()
    people = {'human': SimpleNamespace(status='active', visitor_kind='human', display_name='人甲'),
              'ai': SimpleNamespace(status='active', visitor_kind='external_ai', display_name='机乙'),
              'ai-two': SimpleNamespace(status='active', visitor_kind='external_ai', display_name='机甲')}
    human_key = 'human-test-key-12345678901234567890'
    ai_key = 'ai-test-key-12345678901234567890123'
    ai_two_key = 'ai-two-test-key-123456789012345678'

    class Keys:
        def authenticate_identity(self, value):
            return {human_key: ('hk', 'human'), ai_key: ('ak', 'ai'), ai_two_key: ('ak2', 'ai-two')}.get(value)
        authenticate_bearer_identity = authenticate_identity

    db_path = tmp_path / 'reception.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE visitors(id TEXT PRIMARY KEY,status TEXT)')
        db.execute('CREATE TABLE visitor_keys(id TEXT PRIMARY KEY,visitor_id TEXT,revoked_at TEXT)')
        db.executemany('INSERT INTO visitors VALUES(?,?)', [('human', 'active'), ('ai', 'active'), ('ai-two', 'active')])
        db.executemany('INSERT INTO visitor_keys VALUES(?,?,NULL)', [('hk', 'human'), ('ak', 'ai'), ('ak2', 'ai-two')])

    class Database:
        def connection(self):
            return sqlite3.connect(db_path)

    runtime = SimpleNamespace(keys=Keys(), database=Database(),
        visitor_service=SimpleNamespace(effective_visitor=lambda visitor_id: people[visitor_id]),
        visitors=SimpleNamespace(visitor=lambda visitor_id: people[visitor_id]))
    app = create_public_social_app(runtime)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as ai_client:
            denied = await ai_client.post('/social/v1/login', json={'key': ai_key})
            assert denied.status_code == 403 and denied.json()['detail'] == 'human_binding_required'
            assert wall.contacts() == []
            wall.issue_session('old-ai-session', 'old-csrf', 'ai', 'ak', 9999999999)
            ai_client.cookies.set('mirrow_wall_session', 'old-ai-session')
            ai_client.cookies.set('mirrow_wall_csrf', 'old-csrf')
            assert (await ai_client.get('/social/v1/moments')).status_code == 403
        async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as human_client:
            human_only = await human_client.post('/social/v1/human/register', json={
                'human_key': human_key, 'human_name': '人甲'})
            # The registration name is private identity evidence, not a public nickname.
            assert human_only.status_code == 200 and human_only.json()['actor']['nickname'] == '未设置网名'
            assert wall.household_human('ai') == ''
            bad = await human_client.post('/social/v1/household/register', json={
                'human_key': ai_key, 'ai_key': human_key, 'human_name': '人甲', 'ai_name': '机乙'})
            assert bad.status_code == 403
            registered = await human_client.post('/social/v1/household/register', json={
                'human_key': human_key, 'ai_key': ai_key, 'human_name': '人甲', 'ai_name': '机乙'})
            assert registered.status_code == 200, registered.text
            assert registered.json()['actor']['actor_id'] == 'visitor:human'
            assert wall.household_human('ai') == 'human'
            multi = await human_client.post('/social/v1/household/register', json={
                'human_key': human_key, 'human_name': '人甲',
                'ais': [{'key': ai_key, 'name': '机乙'}, {'key': ai_two_key, 'name': '机甲'}]})
            assert multi.status_code == 200, multi.text
            assert wall.household_human('ai-two') == 'human'
            duplicate = await human_client.post('/social/v1/household/register', json={
                'human_key': human_key, 'human_name': '人甲',
                'ais': [{'key': ai_key, 'name': '机乙'}, {'key': ai_key, 'name': '机乙'}]})
            assert duplicate.status_code == 403
            assert {c['visitor_id'] for c in wall.contacts()} == {'human'}
            managed = (await human_client.get('/social/v1/me/managed-profiles')).json()['profiles']
            assert {item['actor_id'] for item in managed} == {'visitor:human', 'visitor:ai', 'visitor:ai-two'}
            async with httpx.AsyncClient(transport=transport, base_url='https://social.example.invalid') as linked_ai:
                assert (await linked_ai.post('/social/v1/login', json={'key': ai_key})).status_code == 200
                csrf = linked_ai.cookies['mirrow_wall_csrf']
                assert (await linked_ai.get('/social/v1/me')).json()['can_manage_avatar'] is False
                assert (await linked_ai.post('/social/v1/me/avatar', content=b'fake',
                        headers={'Content-Type': 'image/png', 'X-MIRROW-CSRF': csrf})).status_code == 403
                assert (await linked_ai.put('/social/v1/me/profile', json={'avatar': 'https://example.test/a.png'},
                        headers={'X-MIRROW-CSRF': csrf})).status_code == 403
                assert (await linked_ai.put('/social/v1/me/profile', json={'nickname': '新圈名'},
                        headers={'X-MIRROW-CSRF': csrf})).status_code == 200
                assert (await linked_ai.put('/social/v1/people/visitor%3Aai-two/profile',
                        json={'nickname': '越权改名'}, headers={'X-MIRROW-CSRF': csrf})).status_code == 403
                created = await linked_ai.post('/social/v1/moments', json={'content': 'AI 的动态'},
                                               headers={'X-MIRROW-CSRF': csrf})
                assert created.status_code == 200 and created.json()['hosting_mode'] == 'hosted'
            post_id = created.json()['id']
            listing = await human_client.get('/social/v1/moments')
            assert listing.json()['items'][0]['can_manage'] is True
            human_csrf = human_client.cookies['mirrow_wall_csrf']
            ai_avatar = await human_client.post('/social/v1/people/visitor%3Aai/avatar', content=image_bytes,
                                                headers={'Content-Type': 'image/jpeg', 'X-MIRROW-CSRF': human_csrf})
            assert ai_avatar.status_code == 200
            assert (await human_client.put('/social/v1/people/visitor%3Aai/profile',
                    json={'nickname': '机乙 的新网名'}, headers={'X-MIRROW-CSRF': human_csrf})).status_code == 200
            assert wall.profile('visitor:ai')['avatar'] == ai_avatar.json()['actor']['avatar']
            from social_feed.decor_store import get_decor_store
            from social_feed.test_decor import make_home
            decor = get_decor_store(); exhibit = make_home(decor)
            decor.set_gift('ai',exhibit,'test-switch-gift')
            old_session = human_client.cookies['mirrow_wall_session']
            assert {p['actor_id'] for p in (await human_client.get('/social/v1/me/identities')).json()['identities']} == {
                'visitor:human','visitor:ai','visitor:ai-two'}
            switch_path = '/social/v1/me/switch'
            assert (await human_client.post(switch_path,json={'target':'visitor:ai','key':ai_key})).status_code == 403
            bad_switch = await human_client.post(switch_path,json={'target':'visitor:ai','key':human_key},
                                                headers={'X-MIRROW-CSRF':human_csrf})
            assert bad_switch.status_code == 401
            assert human_client.cookies['mirrow_wall_session'] == old_session
            assert (await human_client.post(switch_path,json={'target':'visitor:outsider','key':ai_key},
                                           headers={'X-MIRROW-CSRF':human_csrf})).status_code == 403
            switched = await human_client.post(switch_path,json={'target':'visitor:ai','key':ai_key},
                                               headers={'X-MIRROW-CSRF':human_csrf})
            assert switched.status_code == 200 and switched.json()['actor']['actor_id']=='visitor:ai'
            assert human_client.cookies['mirrow_wall_session'] != old_session
            assert len(decor.collection('visitor:ai'))==1 and not decor.collection('visitor:human')
            assert (await human_client.get('/social/v1/me')).json()['can_manage_avatar'] is False
            assert (await human_client.get('/social/v1/decor/visit')).status_code == 405
            ai_csrf = human_client.cookies['mirrow_wall_csrf']
            assert (await human_client.post('/social/v1/decor/visit',json={},headers={'X-MIRROW-CSRF':ai_csrf})).json()['gifts']
            assert (await human_client.post(switch_path,json={'target':'visitor:human','key':human_key},
                                           headers={'X-MIRROW-CSRF':ai_csrf})).status_code == 200
            human_csrf = human_client.cookies['mirrow_wall_csrf']
            assert (await human_client.get('/social/v1/me')).json()['actor']['actor_id']=='visitor:human'
            assert (await human_client.put('/social/v1/people/visitor%3Aunknown/profile',
                    json={'nickname': '越权改名'}, headers={'X-MIRROW-CSRF': human_csrf})).status_code == 403
            assert (await human_client.patch(f'/social/v1/moments/{post_id}',
                    json={'content': '人修改机的动态', 'revision': 1},
                    headers={'X-MIRROW-CSRF': human_csrf})).status_code == 200
            assert (await human_client.delete(f'/social/v1/moments/{post_id}',
                    headers={'X-MIRROW-CSRF': human_csrf})).status_code == 200

    asyncio.run(scenario())
