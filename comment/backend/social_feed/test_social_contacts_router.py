import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from lounge_visits.lounge_friends import LoungeFriendStore
from routers import lounge_social_contacts_router as contacts_router
from social_feed.public_wall import PublicWall


def test_owner_only_social_contact_link_and_promotion(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db')
    wall.initialize()
    wall.note_contact('v1')
    wall.note_contact('v2')
    wall.register_human_name('v1', '人己')
    wall.set_profile('visitor:v1', '人甲 的网名', '')
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    friends = LoungeFriendStore(tmp_path / 'friends.json')
    monkeypatch.setattr(contacts_router.storage, 'friends', lambda: friends)
    monkeypatch.setattr(contacts_router.service, '_busy', False)
    monkeypatch.setattr('lounge_visits.cognition_profiles.get', lambda _cid: {'primary_entity_id': ''})
    monkeypatch.setattr('cognition.other_book.entities', lambda: {
        'person_a': {'id': 'person_a', 'name': '人甲', 'type': 'human', 'aliases': ['人己']}})
    reception = tmp_path / 'reception.db'
    with sqlite3.connect(reception) as db:
        db.execute('CREATE TABLE visitors(id TEXT, display_name TEXT, status TEXT, visitor_kind TEXT)')
        db.execute('CREATE TABLE visitor_keys(visitor_id TEXT,revoked_at TEXT)')
        db.execute("INSERT INTO visitors VALUES('v1','新朋友','active','human')")
        db.execute("INSERT INTO visitors VALUES('v2','另一位','active','external_ai')")
        db.executemany('INSERT INTO visitor_keys VALUES(?,NULL)', [('v1',), ('v2',)])

    class Database:
        def connection(self):
            return sqlite3.connect(reception)

    async def runtime():
        def effective_visitor(visitor_id):
            with sqlite3.connect(reception) as db:
                row = db.execute('SELECT status,visitor_kind FROM visitors WHERE id=?', (visitor_id,)).fetchone()
            if not row:
                raise ValueError('missing visitor')
            return SimpleNamespace(status=row[0], visitor_kind=row[1])
        def authenticate_identity(raw_key):
            return {'a' * 24: ('key-v1', 'v1'), 'b' * 24: ('key-v2', 'v2')}.get(raw_key)
        return SimpleNamespace(database=Database(), visitor_service=SimpleNamespace(effective_visitor=effective_visitor),
                               keys=SimpleNamespace(authenticate_identity=authenticate_identity))

    monkeypatch.setattr(contacts_router, 'get_runtime', runtime)
    protected = FastAPI()
    protected.include_router(contacts_router.router)
    app = FastAPI()
    app.include_router(contacts_router.router)
    app.dependency_overrides[contacts_router.local_ui] = lambda: None

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected), base_url='http://127.0.0.1') as outsider:
            assert (await outsider.get('/api/lounge-social-contacts')).status_code == 403
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            listed = (await client.get('/api/lounge-social-contacts')).json()
            assert listed['count'] == 2 and listed['friends'] == []
            v1 = next(item for item in listed['contacts'] if item['visitor_id'] == 'v1')
            assert v1['registered_name'] == '人己' and v1['nickname'] == '人甲 的网名'
            assert [item['id'] for item in v1['recognition_matches']] == ['person_a']
            assert (await client.post('/api/lounge-social-contacts/import-reception',
                                      json={'visitor_id':'v2', 'registered_name':'机甲'})).status_code == 409
            correct_key = await client.post('/api/lounge-social-contacts/identities/v1/confirm-key',
                                            json={'visitor_key': 'a' * 24})
            assert correct_key.status_code == 200 and correct_key.json()['already_bound'] is True
            assert (await client.post('/api/lounge-social-contacts/identities/v1/confirm-key',
                                      json={'visitor_key': 'b' * 24})).status_code == 409
            assert (await client.post('/api/lounge-social-contacts/identities/v1/confirm-key',
                                      json={'visitor_key': 'c' * 24})).status_code == 403
            assert (await client.post('/api/lounge-social-contacts/identities/missing/confirm-key',
                                      json={'visitor_key': 'a' * 24})).status_code == 404
            assert 'a' * 24 not in str((await client.get('/api/lounge-social-contacts')).json())
            incomplete = await client.post('/api/lounge-social-contacts/v1/promote', json={'lounge_url': 'https://friend.test/mcp'})
            assert incomplete.status_code == 400
            payload = {'display_name': '新朋友', 'lounge_url': 'https://friend.test/mcp',
                       'visitor_key': 'secret-remote-key', 'relationship_note': ''}
            created = await client.post('/api/lounge-social-contacts/v1/promote', json=payload)
            assert created.status_code == 200, created.text
            friend_id = created.json()['friend_id']
            assert friends.get_owned('k', friend_id).visitor_key == 'secret-remote-key'
            assert wall.linked_friend('v1') == friend_id
            assert 'secret-remote-key' not in str((await client.get('/api/lounge-social-contacts')).json())
            assert (await client.get('/api/lounge-social-contacts')).json()['count'] == 1
            linked = await client.post('/api/lounge-social-contacts/v2/link', json={'friend_id': friend_id})
            assert linked.status_code == 409  # One friend cannot silently represent two inbound identities.
            assert (await client.post('/api/lounge-social-contacts/v1/promote', json=payload)).status_code == 409
            invalid = await client.post('/api/lounge-social-contacts/household-links',
                                        json={'human_visitor_id': 'v2', 'ai_visitor_id': 'v1'})
            assert invalid.status_code == 400
            paired = await client.post('/api/lounge-social-contacts/household-links',
                                       json={'human_visitor_id': 'v1', 'ai_visitor_id': 'v2'})
            assert paired.status_code == 200 and wall.household_human('v2') == 'v1'
            with sqlite3.connect(reception) as db:
                db.execute("INSERT INTO visitors VALUES('v4','另一位人','active','human')")
                db.execute("INSERT INTO visitor_keys VALUES('v4',NULL)")
            human_only = await client.post('/api/lounge-social-contacts/import-reception',
                                           json={'visitor_id':'v4', 'registered_name':'新朋友许某'})
            assert human_only.status_code == 200
            assert wall.registered_name('v4') == '新朋友许某' and wall.household_human('v4') == ''
            move = {'ai_visitor_id': 'v2', 'previous_human_visitor_id': 'v1',
                    'new_human_visitor_id': 'v4'}
            assert (await client.post('/api/lounge-social-contacts/household-links/rebind',
                                      json={**move, 'unexpected': True})).status_code == 400
            rebound = await client.post('/api/lounge-social-contacts/household-links/rebind', json=move)
            assert rebound.status_code == 200 and wall.household_human('v2') == 'v4'
            assert (await client.post('/api/lounge-social-contacts/household-links/rebind',
                                      json=move)).status_code == 409
            listed = (await client.get('/api/lounge-social-contacts')).json()
            assert listed['contacts'][0]['linked_friend_id'] == ''
            assert any(item['household_human_id'] == 'v4' for item in listed['contacts'])
            with sqlite3.connect(reception) as db:
                db.execute("INSERT INTO visitors VALUES('v3','尚未登录的机','active','external_ai')")
                db.execute("INSERT INTO visitor_keys VALUES('v3',NULL)")
            wall.set_profile('visitor:v3', '新朋友的机', '')
            assert (await client.get('/api/lounge-social-contacts')).json()['contacts'][-1]['registered_only'] is True
            wall.link_household('v1', 'v3', ai_name='新朋友的机')
            listed = (await client.get('/api/lounge-social-contacts')).json()
            assert next(item for item in listed['contacts'] if item['visitor_id'] == 'v3')['registered_only'] is True
            assert not any(item['visitor_id'] == 'v3' for item in wall.contacts())

    asyncio.run(scenario())
