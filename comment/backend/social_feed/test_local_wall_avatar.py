"""The MIRROW UI and public website edit the same independent wall avatar."""

import asyncio
from io import BytesIO

import httpx
from fastapi import FastAPI
from PIL import Image

from routers.social_feed_router import router
from social_feed.public_wall import PublicWall


def test_local_avatar_upload_uses_wall_profile_and_rejects_other_people(tmp_path, monkeypatch):
    wall = PublicWall(tmp_path / 'wall.db')
    wall.initialize()
    monkeypatch.setattr('social_feed.avatar_images.AVATAR_DIR', tmp_path / 'avatars')
    monkeypatch.setattr('social_feed.public_wall._WALL', wall)
    monkeypatch.setattr('routers.social_feed_router._owner_person',
                        lambda actor: {'actor_id': actor, 'name': actor, 'avatar': wall.profile(actor)['avatar']})
    raw = BytesIO()
    Image.new('RGB', (320, 320), 'coral').save(raw, format='JPEG')
    app = FastAPI()
    app.include_router(router)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            rejected = await client.post('/api/social-feed/people/visitor:friend/avatar',
                                         content=raw.getvalue(), headers={'content-type': 'image/jpeg'})
            assert rejected.status_code == 403
            uploaded = await client.post('/api/social-feed/people/k/avatar',
                                         content=raw.getvalue(), headers={'content-type': 'image/jpeg'})
            assert uploaded.status_code == 200
            local_url = uploaded.json()['person']['avatar']
            assert local_url.startswith('/api/social-feed/avatars/') and '?v=' in local_url
            assert wall.profile('k')['avatar'].startswith('https://social.example.invalid/social/v1/avatars/')
            fetched = await client.get(local_url)
            assert fetched.status_code == 200 and fetched.headers['content-type'] == 'image/png'
            assert (await client.post('/api/social-feed/people/k/avatar', content=b'not an image',
                                      headers={'content-type': 'image/png'})).status_code == 415

    asyncio.run(check())


def test_local_nickname_edits_same_wall_profile_not_cognition(tmp_path,monkeypatch):
    wall=PublicWall(tmp_path/'profile.db');wall.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL',wall)
    monkeypatch.setattr('routers.social_feed_router._owner_person',lambda actor: {'actor_id':actor,**wall.profile(actor)})
    app=FastAPI();app.include_router(router)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.put('/api/social-feed/people/visitor:friend/profile',json={'nickname':'冒充'})).status_code==403
            saved=await client.put('/api/social-feed/people/k/profile',json={'nickname':'共域网名'})
            assert saved.status_code==200 and wall.profile('k')['nickname']=='共域网名'
            assert (await client.put('/api/social-feed/people/k/profile',json={'nickname':'共域网名','canonical_name':'不允许'})).status_code==422
    asyncio.run(check())
