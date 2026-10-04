"""Isolated remote UI identity transport; never contacts a real home."""
import httpx
import pytest
from fastapi import FastAPI
from routers import social_sites_router as routes
from routers.lounge_reception_router import local_ui
from social_feed.remote_sites import RemoteSite


@pytest.mark.asyncio
async def test_remote_person_and_remark_use_human_key_and_remote_scope(monkeypatch):
    calls = []
    site = RemoteSite('fixture', 'Fixture', 'https://home.example.test', 'human-fixture', 'ai-fixture', True, 1, 1)
    monkeypatch.setattr(routes, '_site', lambda _: site)
    class Client:
        async def request(self, remote, actor, method, path, **kwargs):
            calls.append((remote.id, actor, method, path, kwargs.get('payload')))
            if path == '/social/v1/me':
                return {'actor': {'actor_id':'visitor:human'}, 'can_manage_avatar':True}
            return {'person':{'actor_id':'visitor:friend', 'nickname':'Fixture friend',
                              'name':'Fixture friend', 'avatar':'', 'remark':'fixture'}}
    monkeypatch.setattr(routes, 'RemoteSocialClient', Client)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[local_ui] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        read = await client.get('/api/social-sites/fixture/people/visitor:friend')
        assert read.status_code == 200 and read.json()['person']['nickname'] == 'Fixture friend'
        written = await client.put('/api/social-sites/fixture/people/visitor:friend/remark', json={'remark':'fixture'})
        assert written.status_code == 200
        assert calls[1][1:] == ('human', 'GET', '/social/v1/people/visitor%3Afriend', None)
        assert calls[3][1:] == ('human', 'PUT', '/social/v1/people/visitor%3Afriend/remark', {'remark':'fixture'})
        assert 'human-fixture' not in read.text and 'ai-fixture' not in written.text
        count = len(calls)
        invalid = await client.put('/api/social-sites/fixture/people/visitor:friend/remark',
                                   json={'remark':'fixture', 'viewer':'aning'})
        assert invalid.status_code == 422 and len(calls) == count


@pytest.mark.asyncio
async def test_remote_person_rejects_nonhuman_identity_before_read(monkeypatch):
    monkeypatch.setattr(routes, '_site', lambda _: object())
    calls = []
    class Client:
        async def request(self, remote, actor, method, path, **kwargs):
            calls.append(path)
            return {'actor':{'actor_id':'visitor:ai'}, 'can_manage_avatar':False}
    monkeypatch.setattr(routes, 'RemoteSocialClient', Client)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[local_ui] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/social-sites/fixture/people/visitor:friend')
        assert response.status_code == 403
        assert calls == ['/social/v1/me']
