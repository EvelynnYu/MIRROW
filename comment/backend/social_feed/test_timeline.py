import json
import asyncio
from types import SimpleNamespace

import pytest

from social_feed.timeline import SocialTimeline, TimelineError
from social_feed.store import _decode_feed_cursor


def row(identifier, stamp, visibility='public'):
    return {'id':identifier,'created_at':stamp,'visibility':visibility,'author':'k',
            'content':identifier,'comments':[],'reactions':[]}


class Local:
    def __init__(self, rows): self.rows=rows
    async def list_moments(self, *, limit, visibility, cursor):
        before=_decode_feed_cursor(cursor)
        rows=[r for r in self.rows if (r['created_at'],r['id'])<before and (not visibility or r['visibility']==visibility)]
        rows.sort(key=lambda r:(r['created_at'],r['id']),reverse=True)
        return {'items':rows[:limit],'has_more':len(rows)>limit}


def site(identifier, **kwargs):
    return SimpleNamespace(id=identifier,name=identifier,origin='https://example.test',
        **{'enabled':True,'human_key':'human-test','ai_key':'ai-test',**kwargs})


def reader(rows, calls):
    async def read(site, params):
        calls.append(site.id)
        before=(params['before_time'],params['before_id'])
        selected=sorted([r for r in rows[site.id] if (r['created_at'],r['id'])<before],key=lambda r:(r['created_at'],r['id']),reverse=True)
        return {'items':selected[:params['limit']],'has_more':len(selected)>params['limit'],'viewer_actor':'visitor:self'}
    return read


@pytest.mark.asyncio
async def test_progress_reaches_owner_before_slow_home_but_k_waits_for_complete():
    release=asyncio.Event()
    async def remote(s,p):
        await release.wait()
        return {'items':[row('remote',200)],'has_more':False}
    svc=SocialTimeline(Local([row('home',100)]),[site('slow')],remote_read=remote)
    events=svc.events(limit=1)
    assert (await anext(events))['type']=='begin'
    early=await asyncio.wait_for(anext(events),.5)
    assert early['type']=='source' and early['items'][0]['id']=='home'
    assert 'next_cursor' not in early
    k_read=asyncio.create_task(SocialTimeline(svc.local,svc.sites,actor='k',remote_read=remote).read(limit=1))
    await asyncio.sleep(0)
    assert not k_read.done()
    release.set()
    later=await anext(events)
    assert later['type']=='source' and 'next_cursor' not in later
    complete=await anext(events)
    assert complete['type']=='complete' and complete['page']['items'][0]['id']=='remote'
    assert complete['page']['next_cursor']
    assert (await k_read)['items'][0]['id']=='remote'
    await events.aclose()


@pytest.mark.asyncio
async def test_owner_leaving_stream_cancels_outstanding_reads():
    started=asyncio.Event();cancelled=asyncio.Event()
    async def remote(s,p):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    svc=SocialTimeline(Local([row('home',1)]),[site('slow')],remote_read=remote)
    events=svc.events()
    await anext(events);await anext(events);await asyncio.wait_for(started.wait(),.5)
    await events.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_stream_complete_matches_full_page_with_fixed_snapshot(monkeypatch):
    monkeypatch.setattr('social_feed.timeline.time.time',lambda:1000)
    svc=SocialTimeline(Local([row('home',100)]),[site('a')],remote_read=reader({'a':[row('other',200)]},[]))
    events=[event async for event in svc.events(limit=1)]
    assert events[-1]['page']==await svc.read(limit=1)
    assert all('next_cursor' not in event for event in events[:-1])


@pytest.mark.asyncio
async def test_private_never_connects_to_remote_and_filter_is_real():
    calls=[]
    svc=SocialTimeline(Local([row('hidden',100,'private'),row('pub',200)]),[site('other')],remote_read=reader({},calls))
    result=await svc.read(visibility='private')
    assert [r['id'] for r in result['items']]==['hidden']
    assert calls==[] and len(result['sources'])==1


@pytest.mark.asyncio
async def test_merged_pagination_does_not_drop_unconsumed_rows_or_same_ids():
    calls=[]
    local=[row('same',200),*[row('h'+str(i),100-i) for i in range(6)]]
    remotes={'a':[row('same',200),*[row('a'+str(i),102-i) for i in range(7)]],
             'b':[row('same',200),*[row('b'+str(i),104-i) for i in range(8)]]}
    svc=SocialTimeline(Local(local),[site('a'),site('b')],remote_read=reader(remotes,calls))
    cursor=None; found=[]
    for _ in range(20):
        result=await svc.read(limit=3,cursor=cursor)
        assert not result['unavailable']
        found.extend((r['source']['site_id'],r['id']) for r in result['items'])
        cursor=result['next_cursor']
        if not cursor:break
    expected={('',r['id']) for r in local}|{(s,r['id']) for s,rows in remotes.items() for r in rows}
    assert len(found)==len(set(found))==len(expected)
    assert set(found)==expected


@pytest.mark.asyncio
async def test_keys_are_actor_specific_and_offline_is_partial():
    calls=[]
    async def remote(s,p):
        calls.append(s.id)
        raise RuntimeError('DO-NOT-EXPOSE-SECRET')
    svc=SocialTimeline(Local([row('home',10)]),[site('human-only',ai_key=''),site('disabled',enabled=False),site('both')],actor='k',remote_read=remote)
    page=await svc.read()
    assert calls==['both']
    assert page['items'][0]['id']=='home' and len(page['unavailable'])==1
    assert 'SECRET' not in json.dumps(page)


@pytest.mark.asyncio
async def test_explicit_foreign_private_payload_is_rejected():
    calls=[]
    svc=SocialTimeline(Local([]),[site('a')],remote_read=reader({'a':[row('private',1,'private')]},calls))
    page=await svc.read()
    assert page['items']==[] and page['unavailable']


@pytest.mark.asyncio
async def test_cursor_is_bound_to_actor_and_filter():
    svc=SocialTimeline(Local([row('a',1),row('b',2)]),[])
    page=await svc.read(limit=1)
    for changed in ('public','private'):
        with pytest.raises(TimelineError):await svc.read(visibility=changed,cursor=page['next_cursor'])
    with pytest.raises(TimelineError):await SocialTimeline(svc.local,[],actor='k').read(cursor=page['next_cursor'])
    with pytest.raises(TimelineError):await svc.read(cursor='broken')


@pytest.mark.asyncio
async def test_nonadvancing_remote_page_is_not_an_endless_next_page():
    async def remote(s,p):return {'items':[row('newer',p['before_time']+1)],'has_more':True}
    page=await SocialTimeline(Local([]),[site('a')],remote_read=remote).read()
    assert not page['has_more'] and page['unavailable']


@pytest.mark.asyncio
async def test_private_timeline_route_requires_owner_ui_and_is_absent_on_public_gateway(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from routers import social_sites_router
    from social_feed import public_gateway
    from lounge_reception import runtime
    local = Local([row('private', 10, 'private'), row('public', 20)])
    monkeypatch.setattr('social_feed.get_social_feed_store', lambda: local)
    monkeypatch.setattr(social_sites_router, 'get_remote_site_store', lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(runtime, 'current_runtime', lambda: None)
    app = FastAPI()
    app.include_router(social_sites_router.router)
    transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8005') as client:
        assert (await client.get('/api/social-sites/timeline')).status_code == 403
        headers = {'X-MIRROW-Lounge-Admin': '1'}
        assert (await client.get('/api/social-sites/timeline/stream')).status_code == 403
        streamed=await client.get('/api/social-sites/timeline/stream?visibility_filter=private',headers=headers)
        assert streamed.status_code==200 and streamed.headers['cache-control']=='no-store'
        events=[json.loads(line) for line in streamed.text.splitlines()]
        assert events[-1]['page']['items'][0]['id']=='private'
        assert (await client.get('/api/social-sites/timeline/stream?cursor=invalid',headers=headers)).status_code==400
        response = await client.get('/api/social-sites/timeline?visibility_filter=private', headers=headers)
        assert response.status_code == 200
        assert [item['id'] for item in response.json()['items']] == ['private']
        assert (await client.get('/api/social-sites/timeline?visibility_filter=invalid', headers=headers)).status_code == 422
        assert (await client.get('/api/social-sites/timeline', headers={**headers, 'Origin': 'https://attacker.test'})).status_code == 403
    monkeypatch.setattr(public_gateway, 'get_public_wall', lambda: pytest.fail('public path must not open any database'))
    public_app = public_gateway.create_public_social_app(SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url=public_gateway.PUBLIC_SOCIAL_ORIGIN) as client:
        assert (await client.get('/api/social-sites/timeline', headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_single_card_refresh_uses_local_authority_without_consuming_notices(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from routers import social_feed_router
    from lounge_reception import runtime
    class Store:
        async def get_moment(self, identifier):
            return row('found', 10, 'private') if identifier == 'found' else None
    monkeypatch.setattr(social_feed_router, 'get_social_feed_store', lambda: Store())
    monkeypatch.setattr(runtime, 'current_runtime', lambda: None)
    app = FastAPI()
    app.include_router(social_feed_router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/social-feed/moments/found')
        assert response.status_code == 200 and response.json()['visibility'] == 'private'
        assert (await client.get('/api/social-feed/moments/missing')).status_code == 404


@pytest.mark.asyncio
async def test_chat_review_forwards_filters_and_rejects_global_writes():
    from behavior_scheduler.social_feed_tool import ManageSocialFeedTool
    calls = []
    async def run(**kwargs):
        calls.append(kwargs)
        return {'status': 'success'}
    tool = ManageSocialFeedTool()
    tool.set_dependencies(run_visit=run)
    await tool._review({'session_id': 'isolated', 'read_scope': 'all', 'visibility_filter': 'public'})
    assert calls[-1]['initial_request'] == {'kind': 'all', 'visibility_filter': 'public'}
    await tool._review({'session_id': 'isolated'})
    assert 'initial_request' not in calls[-1]
    assert (await tool._review({'read_scope': 'invalid'}))['status'] == 'invalid_request'
    assert (await tool._direct({'read_scope': 'all'}, 'post'))['error'] == 'all_scope_is_read_only'


@pytest.mark.asyncio
async def test_aggregate_k_node_is_read_only_and_focus_is_source_bound(monkeypatch):
    from wander_manager.social_circle_visit import SocialCircleVisit
    from social_feed import remote_sites
    calls=[]
    class Registry:
        def list(self):return [site('a'),site('b')]
    monkeypatch.setattr(remote_sites,'get_remote_site_store',lambda:Registry())
    monkeypatch.setattr(SocialTimeline,'_remote',lambda self,s,p:reader({'a':[row('same',10)],'b':[row('same',10)]},calls)(s,p))
    async def context(_recipe,**kw):return SimpleNamespace(system_content=kw['wander_runtime_text'])
    async def decide(messages):
        assert 'moment_ref' in messages[0]['content'] and '仅浏览' in messages[0]['content']
        return {'content':json.dumps({'actions':[{'action':'like','moment_id':'same'}],
            'next':{'kind':'switch','site_id':'b','moment_id':'same'},'reflection':'选了 b 家'})}
    local=Local([])
    local.gift_delivery_facts=lambda *_:pytest.fail('aggregate must not visit gift state')
    result=await SocialCircleVisit(decide,store=local,context_builder=context).visit_step(
        request={'kind':'all'},previous_nodes=[],activity_reason='看看',run_id='r',activity_id='a',node_id='n',persona='AI',session_id='s')
    assert result['action_results']==[{'action':'like','status':'requires_single_site_node'}]
    assert result['next_request']['kind']=='focus' and result['next_request']['site_id']=='b'
    assert len(result['viewed_moment_refs'])==2
    assert not result['read_notification_ids']
