"""Household codes exchange public profiles, not auth Keys or cognition names."""
import base64
import json

import httpx
import pytest

from .profile_family import issue_family, decode_family, accept_family
from .test_profile_registration import wall_fixture, runtime_fixture, HOME, SOURCE, LOCAL, EXISTING
from .public_wall import PublicWallError


def family(wall, origin, choice):
    wall.link_households('h',[('a','私有登记甲'),('b','私有登记乙')],human_name='私有登记人',
        profile_sources={'h':choice,'a':choice,'b':choice},profile_origin=origin)
    if choice == LOCAL:
        for actor, name in [('visitor:h','公开人'),('visitor:a','公开甲'),('visitor:b','公开乙')]:
            wall.set_profile(actor,name)


def grant_fixture(tmp_path):
    source, store, _ = wall_fixture(tmp_path,'source');store.set_enabled(True)
    family(source,SOURCE,LOCAL)
    value=issue_family(source,[('visitor:h','human'),('visitor:a','ai'),('visitor:b','ai')],
                       'visitor:h','fixture-key',HOME,SOURCE)
    return source,store,value


def test_code_only_public_profile_metadata_and_grants_atomic(tmp_path):
    wall,store,code=grant_fixture(tmp_path)
    origin,members=decode_family(code['code'])
    assert origin==SOURCE and len(members)==3
    assert '私有登记' not in json.dumps(members,ensure_ascii=False)
    assert 'fixture-key' not in base64.urlsafe_b64decode(code['code']).decode()
    assert len({m['profile_id'] for m in members})==3
    assert code['count']==3
    # Fail the last member at the grant limit. Earlier new grants roll back.
    for _ in range(39):store.make_grant('visitor:b','ai','visitor:h','fixture-key',HOME,SOURCE)
    before=[len(store.grants(actor)) for actor in ['visitor:h','visitor:a','visitor:b']]
    with pytest.raises(PublicWallError,match='grant_limit'):
        issue_family(wall,[('visitor:h','human'),('visitor:a','ai'),('visitor:b','ai')],
                     'visitor:h','fixture-key',HOME,SOURCE)
    assert before==[len(store.grants(actor)) for actor in ['visitor:h','visitor:a','visitor:b']]


@pytest.mark.asyncio
async def test_multimachine_explicit_mapping_all_verified_in_one_commit(tmp_path):
    source,ss,grant=grant_fixture(tmp_path)
    wall,store,registration=wall_fixture(tmp_path,'target');family(wall,HOME,EXISTING)
    class Client:
        async def request(self,origin,operation,body):
            assert origin==SOURCE and operation=='claim'
            row=ss.claim(body['ticket'],body['audience'],body['consumer'])
            return {'audience':row['audience'],'consumer':row['consumer'],'token':row['token'],
                    'profile':ss.snapshot(row['actor'],row['kind'],SOURCE)}
    # Multi-AI mapping is explicit, not based on display names or index guessing.
    mapping=[{'index':0,'subject':'visitor:h','kind':'human'},
             {'index':1,'subject':'visitor:b','kind':'ai'}, {'index':2,'subject':'visitor:a','kind':'ai'}]
    result=await accept_family(wall,grant['code'],mapping,HOME,client=Client())
    assert result['linked']==3
    assert wall.profile('visitor:b')['nickname']=='公开甲'
    assert wall.profile('visitor:a')['nickname']=='公开乙'
    assert all(registration.state(actor)['state']=='linked' for actor in ['visitor:h','visitor:a','visitor:b'])
    assert wall.registered_name('a')=='私有登记甲' and wall.household_human('a')=='h'
    # Own credential/cognition/registration identity has not been merged by the code.
    with pytest.raises(PublicWallError,match='link_conflict'):
        await accept_family(wall,grant['code'],mapping,HOME,client=Client())


@pytest.mark.asyncio
async def test_family_partial_network_or_bad_snapshot_never_partially_links(tmp_path):
    _,ss,grant=grant_fixture(tmp_path)
    wall,store,registration=wall_fixture(tmp_path,'target');family(wall,HOME,EXISTING)
    class Client:
        n=0
        async def request(self,origin,operation,body):
            self.n+=1
            row=ss.claim(body['ticket'],body['audience'],body['consumer'])
            value={'audience':row['audience'],'consumer':row['consumer'],'token':row['token'],
                   'profile':ss.snapshot(row['actor'],row['kind'],SOURCE)}
            if self.n==3:value['token']='invalid'
            return value
    mapping=[{'index':i,'subject':'visitor:'+vid,'kind':'human' if i==0 else 'ai'} for i,vid in enumerate(['h','a','b'])]
    with pytest.raises(PublicWallError,match='response_invalid'):
        await accept_family(wall,grant['code'],mapping,HOME,client=Client())
    assert not any(store.link('visitor:'+v) for v in ['h','a','b'])
    assert all(registration.state('visitor:'+v)['state']=='pending' for v in ['h','a','b'])
    # Remote single-use consumption cannot be rolled back. Reai_bry requires a
    # fresh family code, not pretending the old code is reusable.
    with pytest.raises(PublicWallError,match='ticket_invalid'):
        await accept_family(wall,grant['code'],mapping,HOME,client=Client())
    new=issue_family(ss.wall,[('visitor:h','human'),('visitor:a','ai'),('visitor:b','ai')],
                     'visitor:h','fixture-key',HOME,SOURCE)
    client=Client();client.n=3
    assert (await accept_family(wall,new['code'],mapping,HOME,client=client))['linked']==3


@pytest.mark.asyncio
async def test_wrong_kind_duplicate_mapping_and_wrong_audience(tmp_path):
    _,ss,grant=grant_fixture(tmp_path)
    wall,store,_=wall_fixture(tmp_path,'target');family(wall,HOME,EXISTING)
    class Never:
        async def request(self,*args):raise AssertionError('must fail before external request')
    for mapping in ([{'index':0,'subject':'visitor:a','kind':'ai'}],
                    [{'index':0,'subject':'visitor:h','kind':'human'},{'index':1,'subject':'visitor:h','kind':'ai'}]):
        with pytest.raises(PublicWallError,match='mapping_invalid'):
            await accept_family(wall,grant['code'],mapping,HOME,client=Never())
    origin,members=decode_family(grant['code'])
    with pytest.raises(PublicWallError,match='ticket_invalid'):
        ss.claim(members[0]['ticket'],'https://wrong.example','visitor:h')
    raw=base64.urlsafe_b64decode(grant['code']);body=json.loads(raw)
    body['members'][1]['profile_id']=body['members'][0]['profile_id']
    with pytest.raises(PublicWallError,match='ticket_invalid'):
        decode_family(base64.urlsafe_b64encode(json.dumps(body).encode()).decode())


@pytest.mark.asyncio
async def test_family_routes_guardian_only_preview_does_not_consume(tmp_path,monkeypatch):
    from .public_gateway import create_public_social_app
    wall,store,registration,runtime=runtime_fixture(tmp_path,monkeypatch);store.set_enabled(True)
    family(wall,HOME,LOCAL)
    app=create_public_social_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=HOME) as client:
        await client.post('/social/v1/login',json={'key':'fixture-secret-only-h'})
        headers={'X-MIRROW-CSRF':client.cookies['mirrow_wall_csrf']}
        response=await client.post('/social/v1/profile-links/family/grant',json={'audience':SOURCE},headers=headers)
        assert response.status_code==200 and response.json()['count']==3
        code=response.json()['code']
        result=await client.post('/social/v1/profile-links/family/preview',json={'code':code},headers=headers)
        assert result.status_code==200 and all('ticket' not in m for m in result.json()['members'])
        assert all(not row['revoked'] for row in store.grants('visitor:h'))
        await client.post('/social/v1/me/switch',json={'target':'visitor:a','key':'fixture-secret-only-a'},headers=headers)
        headers={'X-MIRROW-CSRF':client.cookies['mirrow_wall_csrf']}
        denied=await client.post('/social/v1/profile-links/family/grant',json={'audience':SOURCE},headers=headers)
        assert denied.status_code==403
