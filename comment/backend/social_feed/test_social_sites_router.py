from fastapi import FastAPI
import httpx
import pytest

from routers.lounge_reception_router import local_ui
from routers import social_sites_router
from social_feed.remote_sites import RemoteSiteStore


@pytest.mark.asyncio
async def test_site_registry_masks_keys_tests_each_identity_and_keeps_lounge_separate(tmp_path, monkeypatch):
    store = RemoteSiteStore(tmp_path / 'sites.json')
    from social_feed.decor_store import DecorStore
    decor = DecorStore(tmp_path / 'decor.db')
    monkeypatch.setattr('social_feed.decor_store.get_decor_store', lambda: decor)
    monkeypatch.setattr('social_feed.remote_sites.protect', lambda value: 'dpapi:v1:' + value[::-1])
    monkeypatch.setattr('social_feed.remote_sites.unprotect', lambda value: value[len('dpapi:v1:'):][::-1])
    monkeypatch.setattr(social_sites_router, 'get_remote_site_store', lambda: store)
    async def public_address(_url):
        return '203.0.113.10'
    monkeypatch.setattr(social_sites_router, 'public_destination', public_address)

    calls = []
    class Client:
        async def request(self, site, actor, method, path, **_kwargs):
            calls.append((site.origin, actor, method, path))
            if path == '/social/v1/moments':
                return {'items': [{'id': 'only-this-home'}], 'has_more': False, 'next_cursor': None}
            return {'actor': {'actor_id': 'visitor:' + actor, 'name': actor},
                    'can_manage_avatar': actor == 'human'}
    monkeypatch.setattr(social_sites_router, 'RemoteSocialClient', Client)
    app = FastAPI()
    app.include_router(social_sites_router.router)
    app.dependency_overrides[local_ui] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        created = await client.post('/api/social-sites', json={
            'name': '人甲 家', 'origin': 'https://social.example.test',
            'human_key': 'human-secret', 'ai_key': 'ai-secret'})
        assert created.status_code == 200, created.text
        site_id = created.json()['site']['id']
        listed = await client.get('/api/social-sites')
        assert listed.json()['sites'][0]['has_human_key']
        assert 'human-secret' not in listed.text and 'ai-secret' not in listed.text
        tested = await client.post(f'/api/social-sites/{site_id}/test')
        assert tested.json()['identities']['human']['status'] == 'connected'
        assert tested.json()['identities']['ai']['status'] == 'connected'
        assert calls[0][1:] == ('human', 'GET', '/social/v1/me')
        assert calls[1][1:] == ('ai', 'GET', '/social/v1/me')
        page = await client.get(f'/api/social-sites/{site_id}/moments')
        assert page.status_code == 200 and page.json()['items'][0]['id'] == 'only-this-home'
        assert calls[2][1:] == ('human', 'GET', '/social/v1/me')
        assert calls[3][1:] == ('human', 'GET', '/social/v1/moments')
        updated = await client.put(f'/api/social-sites/{site_id}', json={
            'name': '人甲 家', 'origin': 'https://social.example.test', 'ai_key': ''})
        assert updated.status_code == 200
        assert store.get(site_id).human_key == 'human-secret'
        assert store.get(site_id).ai_key == ''
        assert (await client.delete(f'/api/social-sites/{site_id}')).json()['ok']
