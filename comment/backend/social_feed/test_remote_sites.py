import json

import httpx
import pytest

from social_feed import remote_sites
from social_feed.remote_client import RemoteSocialClient
from social_feed.remote_sites import RemoteSiteError, RemoteSiteStore, normalize_social_origin
from social_feed.remote_visit_store import RemoteSocialVisitStore


def test_remote_registry_is_independent_and_never_serializes_raw_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_sites, 'protect', lambda value: 'dpapi:v1:' + value[::-1])
    monkeypatch.setattr(remote_sites, 'unprotect', lambda value: value[len('dpapi:v1:'):][::-1])
    store = RemoteSiteStore(tmp_path / 'sites.json')
    site = store.save(name='人甲 家', origin='https://social.example.test/',
                      human_key='person-secret', ai_key='machine-secret')
    saved = (tmp_path / 'sites.json').read_text('utf-8')
    assert 'person-secret' not in saved and 'machine-secret' not in saved
    assert site.public_dict()['has_human_key'] and site.public_dict()['has_ai_key']
    assert 'human_key' not in site.public_dict() and 'ai_key' not in site.public_dict()
    assert store.get(site.id).origin == 'https://social.example.test'
    store.save(site_id=site.id, name='人甲 共域', origin=site.origin,
               human_key='person-secret', ai_key='', enabled=True)
    assert store.get(site.id).human_key == 'person-secret'
    assert store.get(site.id).ai_key == ''
    with pytest.raises(RemoteSiteError, match='duplicate'):
        store.save(name='重复', origin=site.origin, human_key='another')
    store.delete(site.id)
    assert store.list() == []


@pytest.mark.parametrize('value', [
    'http://social.example.test', 'https://localhost', 'https://127.0.0.1',
    'https://social.example.test:8443', 'https://user:pw@social.example.test',
    'https://social.example.test/other', 'https://social.example.test?key=secret',
])
def test_remote_origin_rejects_unsafe_forms(value):
    with pytest.raises(RemoteSiteError):
        normalize_social_origin(value)


@pytest.mark.asyncio
async def test_remote_client_pins_public_dns_and_uses_only_selected_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_sites, 'protect', lambda value: 'dpapi:v1:' + value[::-1])
    monkeypatch.setattr(remote_sites, 'unprotect', lambda value: value[len('dpapi:v1:'):][::-1])
    store = RemoteSiteStore(tmp_path / 'sites.json')
    site = store.save(name='朋友家', origin='https://social.example.test',
                      human_key='human-secret', ai_key='ai-secret')
    async def public_address(_url):
        return '203.0.113.10'
    monkeypatch.setattr('social_feed.remote_client.public_destination', public_address)
    seen = []
    def handler(request):
        seen.append((request.headers['Host'], request.headers['Authorization'], str(request.url)))
        return httpx.Response(200, json={'actor': {'actor_id': 'visitor:friend'}})
    client = RemoteSocialClient(httpx.MockTransport(handler))
    result = await client.request(site, 'human', 'GET', '/social/v1/me')
    assert result['actor']['actor_id'] == 'visitor:friend'
    await client.request(site, 'ai', 'GET', '/social/v1/me')
    assert seen[0][0] == 'social.example.test' and seen[0][1] == 'Bearer human-secret'
    assert seen[1][1] == 'Bearer ai-secret'
    assert all('203.0.113.10' in item[2] for item in seen)
    with pytest.raises(RemoteSiteError, match='invalid_social_request'):
        await client.request(site, 'ai', 'GET', '/social/v1/../admin')


@pytest.mark.asyncio
async def test_remote_client_rejects_redirect_and_unauthorized(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_sites, 'protect', lambda value: 'dpapi:v1:' + value[::-1])
    monkeypatch.setattr(remote_sites, 'unprotect', lambda value: value[len('dpapi:v1:'):][::-1])
    site = RemoteSiteStore(tmp_path / 'sites.json').save(
        name='朋友家', origin='https://social.example.test', human_key='human-secret')
    async def public_address(_url):
        return '203.0.113.10'
    monkeypatch.setattr('social_feed.remote_client.public_destination', public_address)
    for code, expected in [(302, 'social_site_unavailable'), (401, 'social_site_key_rejected')]:
        client = RemoteSocialClient(httpx.MockTransport(lambda _request: httpx.Response(
            code, headers={'Location': 'http://127.0.0.1/private'})))
        with pytest.raises(RemoteSiteError, match=expected):
            await client.request(site, 'human', 'GET', '/social/v1/me')


@pytest.mark.asyncio
async def test_projected_avatar_uses_fixed_authenticated_profile_route(tmp_path,monkeypatch):
    monkeypatch.setattr(remote_sites,'protect',lambda value:'dpapi:v1:'+value[::-1])
    monkeypatch.setattr(remote_sites,'unprotect',lambda value:value[len('dpapi:v1:'):][::-1])
    site=RemoteSiteStore(tmp_path/'sites.json').save(name='资料投影家',origin='https://social.example.test',human_key='person')
    async def address(_url):return '203.0.113.10'
    monkeypatch.setattr('social_feed.remote_client.public_destination',address)
    identifier='a'*64+'.png'
    def serve(request):
        assert request.url.path=='/social/v1/profile-links/avatars/'+identifier
        assert request.headers['Authorization']=='Bearer person'
        return httpx.Response(200,content=b'fixture-png')
    client=RemoteSocialClient(httpx.MockTransport(serve))
    assert await client.media(site,'human',identifier,avatar=True,profile_avatar=True)==(b'fixture-png','image/png')
    with pytest.raises(RemoteSiteError,match='invalid_social_request'):
        await client.media(site,'human','../private',avatar=True,profile_avatar=True)


@pytest.mark.asyncio
async def test_k_remote_store_requires_ai_key_and_sends_idempotent_public_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_sites, 'protect', lambda value: 'dpapi:v1:' + value[::-1])
    monkeypatch.setattr(remote_sites, 'unprotect', lambda value: value[len('dpapi:v1:'):][::-1])
    site = RemoteSiteStore(tmp_path / 'sites.json').save(
        name='朋友家', origin='https://social.example.test', human_key='person', ai_key='machine')
    async def public_address(_url): return '203.0.113.10'
    monkeypatch.setattr('social_feed.remote_client.public_destination', public_address)
    seen = []
    def handler(request):
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, request.headers['Authorization'], body))
        if request.url.path.endswith('/me'):
            return httpx.Response(200, json={'actor': {'actor_id': 'visitor:machine'}, 'can_manage_avatar': False})
        if request.method == 'POST' and request.url.path.endswith('/moments'):
            return httpx.Response(200, json={'id': 'post-id'})
        return httpx.Response(200, json={'id': 'comment-id'})
    store = RemoteSocialVisitStore(site, RemoteSocialClient(httpx.MockTransport(handler)))
    assert await store.me() == 'visitor:machine'
    with pytest.raises(ValueError, match='public_only'):
        await store.create_moment('k', '私密', visibility='private', source_key='request-1234')
    assert (await store.create_moment('k', '公开', visibility='public', source_key='request-1234'))['id'] == 'post-id'
    assert (await store.add_comment('post-id', 'k', '你好', reply_to_id=None,
                                    source_key='comment-1234'))['id'] == 'comment-id'
    assert all(call[2] == 'Bearer machine' for call in seen)
    assert seen[1][3] == {'content': '公开', 'request_id': 'request-1234'}
    assert seen[2][3]['request_id'] == 'comment-1234'
