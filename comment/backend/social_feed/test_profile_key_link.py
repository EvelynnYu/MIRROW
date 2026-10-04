"""Two temporary walls, fake credentials and an in-process callback, never real homes."""
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from .profile_identity import home_grant
from .profile_key_link import accept_key_profiles, sync_home_profiles
from .public_wall import PublicWallError
from .test_profile_registration import wall_fixture, runtime_fixture, HOME, SOURCE, LOCAL, EXISTING


class ClaimClient:
    def __init__(self, source, store):
        self.source, self.store, self.calls = source, store, 0
        self.invalid_last = False
        self.after_claim = None

    async def request(self, origin, operation, body):
        assert origin == SOURCE and operation == 'claim'
        self.calls += 1
        row = self.store.claim(body['ticket'], body['audience'], body['consumer'])
        result = {'token': row['token'], 'audience': row['audience'], 'consumer': row['consumer'],
                  'home_profile': home_grant(row),
                  'profile': self.store.snapshot(row['actor'], row['kind'], SOURCE)}
        if self.invalid_last and self.calls == 2: result['profile']['version'] = 'wrong'
        if self.after_claim: self.after_claim()
        return result


def setup(tmp_path, monkeypatch):
    target, ts, tr, runtime = runtime_fixture(tmp_path, monkeypatch)
    target.link_households('h', [('a', '私有机名')], human_name='私有人名')
    source, ss, sr = wall_fixture(tmp_path, 'source')
    source.set_profile('aning', '公开人名')
    source.set_profile('k', '公开机名')
    callback = ClaimClient(source, ss)
    monkeypatch.setattr('social_feed.profile_key_link.ProfileTransport', lambda: callback)
    from .public_gateway import create_public_social_app
    app = create_public_social_app(runtime)
    return source, ss, target, ts, tr, runtime, app, callback


def payload(store, ai_key='fixture-secret-only-a'):
    members = []
    for actor, kind in [('aning', 'human')] + ([('k', 'ai')] if ai_key else []):
        snap = store.snapshot(actor, kind, SOURCE)
        code = store.make_grant(actor, kind, 'aning', '__owner__', HOME, SOURCE)['code']
        members.append({'code': code, 'kind': kind, 'profile_id': snap['profile_id']})
    return {'members': members, 'ai_key': ai_key}


class RemoteClient:
    def __init__(self, http): self.http = http
    async def request(self, site, role, method, path, *, payload=None):
        from .remote_sites import RemoteSiteError
        key = site.human_key if role == 'human' else site.ai_key
        response = await self.http.request(method, path, json=payload, headers={'Authorization': 'Bearer ' + key})
        if response.status_code in {401, 403}: raise RemoteSiteError('social_site_key_rejected')
        if response.status_code != 200: raise RemoteSiteError('social_site_unavailable')
        return response.json()


def site(human='fixture-secret-only-h', ai='fixture-secret-only-a'):
    return SimpleNamespace(origin=HOME, human_key=human, ai_key=ai, enabled=True)


@pytest.mark.asyncio
async def test_human_guardian_pair_auto_link_idempotent_updates_and_rotation(tmp_path, monkeypatch):
    source, ss, target, ts, tr, runtime, app, callback = setup(tmp_path, monkeypatch)
    assert not ss.enabled()  # Only owner grants are allowed, guest custody stays closed.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        remote = RemoteClient(http)
        result = await sync_home_profiles(source, site(), SOURCE, client=remote)
        assert result == {'human': {'status': 'linked'}, 'ai': {'status': 'linked'}}
        assert target.profile('visitor:h')['nickname'] == '公开人名'
        assert target.profile('visitor:a')['nickname'] == '公开机名'
        assert tr.state('visitor:h')['state'] == tr.state('visitor:a')['state'] == 'linked'
        assert target.registered_name('h') == '私有人名' and target.household_human('a') == 'h'
        before = [len(ss.grants(a)) for a in ('aning', 'k')]
        assert await sync_home_profiles(source, site(), SOURCE, client=remote) == result
        assert before == [len(ss.grants(a)) for a in ('aning', 'k')] and callback.calls == 2
        old_ids = [ts.status(a)['identity_id'] for a in ('visitor:h', 'visitor:a')]
        source.set_profile('k', '机的新公开网名')
        assert await sync_home_profiles(source, site(), SOURCE, client=remote) == result
        assert target.profile('visitor:a')['nickname'] == '机的新公开网名'
        assert all(len([g for g in ss.grants(a) if g['revoked'] is None]) == 1 for a in ('aning', 'k'))
        original_auth = runtime.keys.authenticate_bearer_identity
        with runtime.database.connection() as db:
            db.execute("UPDATE visitor_keys SET revoked_at='now' WHERE id='key-h'")
            db.execute("INSERT INTO visitor_keys VALUES('rotated-h','h',NULL)")
        runtime.keys.authenticate_bearer_identity = lambda key: ('rotated-h', 'h') if key == 'rotated-human-key' else original_auth(key)
        failed = await sync_home_profiles(source, site(), SOURCE, client=remote)
        assert failed['human']['status'] == failed['ai']['status'] == 'pending'
        assert await sync_home_profiles(source, site(human='rotated-human-key'), SOURCE, client=remote) == result
        assert old_ids == [ts.status(a)['identity_id'] for a in ('visitor:h', 'visitor:a')]
        me = await http.get('/social/v1/me', headers={'Authorization': 'Bearer rotated-human-key'})
        assert 'post' in me.json()['capabilities']
        assert not any(key in json.dumps(result) for key in ['fixture-secret', 'rotated-human-key', '私有'])


@pytest.mark.asyncio
async def test_only_human_allowed_only_ai_requires_human_key(tmp_path, monkeypatch):
    source, ss, target, ts, _, _, app, callback = setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        remote = RemoteClient(http)
        pending = await sync_home_profiles(source, site(human=''), SOURCE, client=remote)
        assert pending['ai']['reason'] == 'profile_human_key_required' and callback.calls == 0
        result = await sync_home_profiles(source, site(ai=''), SOURCE, client=remote)
        assert result['human']['status'] == 'linked' and result['ai']['status'] == 'not_configured'
        assert not ts.link('visitor:a')


@pytest.mark.asyncio
async def test_target_exact_keys_same_household_type_and_cookie_guards(tmp_path, monkeypatch):
    _, ss, target, ts, _, runtime, app, callback = setup(tmp_path, monkeypatch)
    target.link_households('other-human', [('b', '另一家的机')], human_name='另一人')
    with runtime.database.connection() as db:
        db.execute("INSERT INTO visitors VALUES('other-human','active','human')")
        db.execute("INSERT INTO visitor_keys VALUES('other-human-key','other-human',NULL)")
    body = payload(ss)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        path = '/social/v1/profile-links/key-profile'
        assert (await http.post(path, json=body, headers={'Authorization':'Bearer fixture-secret-only-a'})).status_code == 403
        response = await http.post(path, json=body | {'ai_key':'fixture-secret-only-b'}, headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 403 and response.json()['detail'] == 'profile_household_mismatch'
        assert (await http.post(path, json=body | {'ai_key':'fixture-secret-only-h'}, headers={'Authorization':'Bearer fixture-secret-only-h'})).status_code == 403
        assert (await http.get(path)).status_code == 401
        response = await http.post(path, json=body | {'actor':'visitor:other-human'}, headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 422 and 'fixture-secret' not in response.text
        response = await http.get(path, headers={'Authorization':'Bearer fixture-secret-only-h',
                                                'Cookie':'mirrow_wall_session=fake-session'})
        assert response.status_code == 401
        with runtime.database.connection() as db: db.execute("UPDATE visitor_keys SET revoked_at='now' WHERE id='key-a'")
        response = await http.post(path, json=body, headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 401
        assert callback.calls == 0 and not ts.link('visitor:h')


@pytest.mark.asyncio
async def test_failed_second_snapshot_rolls_back_both_source_choices_and_links(tmp_path, monkeypatch):
    _, ss, target, ts, tr, _, app, callback = setup(tmp_path, monkeypatch)
    callback.invalid_last = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        response = await http.post('/social/v1/profile-links/key-profile', json=payload(ss),
                                   headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 400
        assert tr.state('visitor:h')['state'] == tr.state('visitor:a')['state'] == 'unconfirmed'
        assert not ts.link('visitor:h') and not ts.link('visitor:a')
        callback.invalid_last = False
        response = await http.post('/social/v1/profile-links/key-profile', json=payload(ss),
                                   headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 200 and len(response.json()['profiles']) == 2


@pytest.mark.asyncio
async def test_explicit_local_upgrade_but_other_source_or_outgoing_grants_block(tmp_path, monkeypatch):
    _, ss, target, ts, tr, _, app, callback = setup(tmp_path, monkeypatch)
    tr.select('visitor:h', LOCAL, HOME); tr.select('visitor:a', LOCAL, HOME)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        response = await http.post('/social/v1/profile-links/key-profile', json=payload(ss),
                                   headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 200 and tr.state('visitor:h')['state'] == 'linked'
    # A separate wall choosing another source is never silently rehomed.
    wall, store, reg = wall_fixture(tmp_path, 'other')
    wall.register_human_name('h', '人', profile_source={'choice':'existing','origin':'https://another.example'}, profile_origin=HOME)
    body = payload(ss, '')['members'][0] | {'actor':'visitor:h'}
    before = callback.calls
    with pytest.raises(PublicWallError, match='source_locked'):
        await accept_key_profiles(wall, [body], HOME, client=callback)
    assert callback.calls == before
    wall2, store2, reg2 = wall_fixture(tmp_path, 'grant-owner')
    wall2.register_human_name('h', '人'); wall2.set_profile('visitor:h', '本人资料'); store2.set_enabled(True)
    store2.make_grant('visitor:h','human','visitor:h','fixture-id','https://third.example',HOME)
    with pytest.raises(PublicWallError, match='outgoing_grants'):
        await accept_key_profiles(wall2, [body], HOME, client=callback)
    assert callback.calls == before


@pytest.mark.asyncio
async def test_credential_revoked_during_callback_no_commit(tmp_path, monkeypatch):
    _, ss, target, ts, tr, runtime, app, callback = setup(tmp_path, monkeypatch)
    def revoke():
        with runtime.database.connection() as db: db.execute("UPDATE visitor_keys SET revoked_at='now' WHERE id='key-h'")
    callback.after_claim = revoke
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HOME) as http:
        response = await http.post('/social/v1/profile-links/key-profile', json=payload(ss),
                                   headers={'Authorization':'Bearer fixture-secret-only-h'})
        assert response.status_code == 401
        assert not ts.link('visitor:h') and not ts.link('visitor:a')
        assert tr.state('visitor:h')['state'] == 'unconfirmed'


def test_home_delegation_does_not_enable_guest_hosting(tmp_path):
    wall, store, _ = wall_fixture(tmp_path)
    wall.register_human_name('h', '人'); wall.set_profile('visitor:h', '访客公开名')
    with pytest.raises(PublicWallError, match='hosting_disabled'):
        store.make_grant('visitor:h','human','visitor:h','key',SOURCE,HOME)
    wall.set_profile('aning', '本家公开名')
    store.make_grant('aning','human','aning','__owner__',SOURCE,HOME)
    assert store.enabled() is False


@pytest.mark.asyncio
async def test_fake_remote_success_without_callback_never_completes_or_accumulates(tmp_path):
    wall, store, _ = wall_fixture(tmp_path)
    wall.set_profile('aning', '本人公开名'); wall.set_profile('k', '机公开名')
    class FakeRemote:
        async def request(self, site, role, method, path, *, payload=None):
            if method == 'GET':
                return {'actor_id':'visitor:'+role, 'kind':role, 'human_actor_id':'visitor:human', 'link':{}}
            return {'profiles':[{'linked':True,'status':'verified','identity_id':SOURCE+'#'+m['profile_id']}
                                for m in payload['members']]}
    for _ in range(3):
        result = await sync_home_profiles(wall, site(), SOURCE, client=FakeRemote())
        assert result['human']['status'] == result['ai']['status'] == 'pending'
    assert all(not [g for g in store.grants(actor) if g['revoked'] is None] for actor in ('aning','k'))
