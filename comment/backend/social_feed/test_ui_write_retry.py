"""HTTP retries must not duplicate committed UI writes or flip a like back."""
import httpx
import pytest
from fastapi import FastAPI
from routers import social_feed_router as local, social_sites_router as remote
from social_feed.hybrid_store import HybridSocialFeedStore
from social_feed.store import SocialFeedStore
from social_feed.public_wall import PublicWall


@pytest.mark.asyncio
@pytest.mark.parametrize('visibility', ['private', 'public'])
async def test_local_http_replay_and_explicit_likes(tmp_path, monkeypatch, visibility):
    store = HybridSocialFeedStore(SocialFeedStore(tmp_path/'private.db'), PublicWall(tmp_path/'public.db'))
    monkeypatch.setattr(local, 'get_social_feed_store', lambda: store)
    app = FastAPI(); app.include_router(local.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = '/api/social-feed/moments'
        body = dict(content='retry demo', visibility=visibility, request_id='post-attempt-0001')
        first = await client.post(url, json=body); assert first.status_code == 200, first.text
        again = await client.post(url, json=body); assert again.json()['id'] == first.json()['id']
        moment = first.json()['id']
        comments = url+'/'+moment+'/comments'
        comment = dict(content='reply demo', request_id='comment-attempt-0001')
        a = await client.post(comments, json=comment); b = await client.post(comments, json=comment)
        assert a.status_code == b.status_code == 200
        assert a.json()['id'] == b.json()['id']
        assert len((await store.get_moment(moment))['comments']) == 1
        like = url+'/'+moment+'/like'
        for target in (True, True, False, False):
            result = await client.put(like, json={'liked':target})
            assert result.status_code == 200, result.text
            assert result.json()['active'] == target
            assert len((await store.get_moment(moment))['reactions']) == int(target)
        # The same text after a confirmed successful write is a NEW operation.
        body['request_id'] = 'post-attempt-0002'
        assert (await client.post(url, json=body)).json()['id'] != moment


@pytest.mark.asyncio
async def test_remote_relay_preserves_request_and_desired_state(monkeypatch):
    calls = []
    async def relay(site, method, path, **kwargs):
        calls.append((site, method, path, kwargs['payload']))
        return {'ok':True}
    monkeypatch.setattr(remote, '_remote', relay)
    await remote.post_moment('one', remote.RemotePost(content='demo', request_id='post-0001'))
    await remote.add_comment('one', 'post', remote.RemoteComment(content='reply', request_id='reply-0001'))
    await remote.set_like('one', 'post', remote.RemoteLike(liked=True))
    assert calls[0][3]['request_id'] == 'post-0001'
    assert calls[1][3]['request_id'] == 'reply-0001'
    assert calls[2][1:] == ('PUT', '/social/v1/moments/post/like', {'liked':True})


@pytest.mark.asyncio
async def test_private_archive_like_replay(tmp_path):
    store = HybridSocialFeedStore(SocialFeedStore(tmp_path/'private.db'), PublicWall(tmp_path/'public.db'))
    item = await store.create_moment('aning', 'archive demo', visibility='public')
    await store.set_moment_visibility(item['id'], 'aning', 'private')
    for target in (True, True, False, False):
        assert (await store.toggle_like(item['id'], 'aning', liked=target))['active'] == target
        assert len((await store.get_moment(item['id']))['reactions']) == int(target)
