from types import SimpleNamespace
import pytest
from fastapi import FastAPI
import httpx
from routers import social_inbox_router as module
from routers.lounge_reception_router import local_ui


@pytest.mark.asyncio
async def test_read_claims_are_source_and_human_bound_and_detail_does_not_ack(monkeypatch):
    calls=[]
    site=SimpleNamespace(id='foreign',name='Foreign home',origin='https://example.test/comment',enabled=True)
    monkeypatch.setattr(module,'get_remote_site_store',lambda:SimpleNamespace(get=lambda id:site))
    class Client:
        async def request(self,s,role,method,path,**kwargs):
            calls.append((s.id,role,method,path,kwargs))
            if path.endswith('/me'):return {'actor':{'actor_id':'visitor:human'},'can_manage_avatar':True}
            if method=='POST':return {'marked':1}
            return {'id':'post','visibility':'public','content':'Body','comments':[],'reactions':[]}
    monkeypatch.setattr(module,'RemoteSocialClient',Client)
    class Local:
        async def mark_notifications_read(self,actor,ids,**kwargs):calls.append(('local',actor,ids));return 1
    monkeypatch.setattr(module,'get_social_feed_store',Local)
    app=FastAPI();app.include_router(module.router);app.dependency_overrides[local_ui]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.post('/inbox/read',json={'site_id':'foreign','notification_ids':[]})).json()['marked']==0
        assert calls==[]
        assert (await c.get('/inbox/moment?site_id=foreign&moment_id=post')).json()['viewer_actor']=='visitor:human'
        assert all(call[2]=='GET' for call in calls)
        calls.clear()
        assert (await c.post('/inbox/read',json={'site_id':'foreign','notification_ids':['same']})).json()['marked']==1
        assert all(call[1]=='human' for call in calls)
        assert calls[-1][4]['payload']=={'notification_ids':['same']}
        calls.clear()
        await c.post('/inbox/read',json={'notification_ids':['same']})
        assert calls==[('local','aning',['same'])]


@pytest.mark.asyncio
async def test_metadata_poll_does_not_generate_arrival_gifts(tmp_path,monkeypatch):
    from social_feed import public_gateway
    from social_feed.public_wall import PublicWall
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    monkeypatch.setattr(public_gateway,'get_public_wall',lambda:wall)
    monkeypatch.setattr(public_gateway,'_identity',lambda *a,**kw:'aning')
    monkeypatch.setattr(public_gateway,'_display',lambda *a,**kw:{'actor_id':'aning'})
    gifts=[]
    monkeypatch.setattr(public_gateway,'_receive_login_gift',lambda *a:gifts.append(True))
    app=public_gateway.create_public_social_app(SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=public_gateway.PUBLIC_SOCIAL_ORIGIN) as c:
        passive=await c.get('/social/v1/me?arrival=false')
        assert passive.status_code==200 and gifts==[]
        assert 'visitor_interactions_v1' in passive.json()['capabilities']
        assert (await c.get('/social/v1/me')).status_code==200
        assert gifts==[True]
