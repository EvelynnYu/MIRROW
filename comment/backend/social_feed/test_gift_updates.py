"""Only temporary databases and mock peers. Never touch the live wall."""
import io
import json
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from .decor_store import DecorStore, DecorError
from .decor_models import HomeDesign
from .decor_gift_updates import migrate, for_actor
from .test_decor import make_home, gateway


def setup(tmp_path):
    store=DecorStore(tmp_path/'gift.db');migrate(store,backup_ready=True)
    eid=make_home(store);store.set_gift('ai',eid,'first-round')
    receipt=store.visit('visitor:k','ai','测试机')[0];store.seen('visitor:k',receipt['id'])
    return store,eid,receipt


def test_opt_in_keeps_original_receipt_and_fact(tmp_path):
    store,eid,receipt=setup(tmp_path)
    home=store.home();home['exhibits'][0]['name']='新名字'
    store.save_home(HomeDesign.model_validate(home))
    assert store.collection('visitor:k')[0]['snapshot']['name']=='小杯酒'
    home=store.home();store.save_home(HomeDesign.model_validate(home),sync_gift_exhibits=[eid])
    updated=store.collection('visitor:k')[0]
    assert updated['snapshot']['name']=='新名字' and updated['presentation_version']>0
    assert (updated['id'],updated['received'],updated['seen'])==(receipt['id'],receipt['received'],1)
    assert store.visit('visitor:k','ai','测试机')==[]
    with store.connect() as db:
        assert json.loads(db.execute('SELECT snapshot FROM collections').fetchone()[0])['name']=='小杯酒'
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
        assert '小杯酒' in db.execute('SELECT fact FROM events').fetchone()[0]
    assert for_actor(store,'visitor:other',[receipt['gift_id']])==[]


def test_not_migrated_save_is_compatible_and_opt_in_rolls_back(tmp_path):
    store=DecorStore(tmp_path/'gift.db');eid=make_home(store);home=store.home()
    store.save_home(HomeDesign.model_validate(home))
    home=store.home();home['exhibits'][0]['name']='不能提交'
    with pytest.raises(DecorError,match='gift_updates_not_ready'):
        store.save_home(HomeDesign.model_validate(home),sync_gift_exhibits=[eid])
    assert store.home()['exhibits'][0]['name']=='小杯酒'
    with pytest.raises(DecorError,match='decor_backup_required'):migrate(store)


def test_import_updates_do_not_reaward_or_regress(tmp_path):
    source,eid,receipt=setup(tmp_path);target=DecorStore(tmp_path/'other'/'gift.db');migrate(target,backup_ready=True)
    target.imported_gift('k','https://source.example',receipt,'朋友家')
    initial=target.collection('k')[0];target.seen('k',initial['id'])
    home=source.home();home['exhibits'][0]['description']='更新说明'
    source.save_home(HomeDesign.model_validate(home),sync_gift_exhibits=[eid])
    new=source.collection('visitor:k')[0]
    target.imported_gift('k','https://source.example',new,'朋友家')
    target.imported_gift('k','https://source.example',receipt,'朋友家')
    result=target.collection('k')[0]
    assert result['snapshot']['description']=='更新说明'
    assert result['id']==initial['id'] and result['received']==initial['received'] and result['seen']==1
    with target.connect() as db: assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1


@pytest.mark.asyncio
async def test_remote_refresh_version_and_failed_media_retry(tmp_path):
    from .decor_federation import refresh_remote_gifts
    from .decor_media import save_media
    source,eid,receipt=setup(tmp_path);target=DecorStore(tmp_path/'other'/'gift.db');migrate(target,backup_ready=True)
    target.imported_gift('k','https://source.example',receipt,'朋友家')
    image=io.BytesIO();Image.new('RGB',(12,12),'red').save(image,format='PNG')
    asset=save_media(source,'aning',image.getvalue())
    home=source.home();home['exhibits'][0]['image']=asset['id'];source.save_home(HomeDesign.model_validate(home),sync_gift_exhibits=[eid])
    class Client:
        fail=True;calls=0
        async def request(self,*args,**kwargs):return {'items':source.collection('visitor:k')}
        async def media(self,*args):
            self.calls+=1
            if self.fail:raise ValueError('offline')
            return image.getvalue(),'image/png'
    client=Client();site=SimpleNamespace(origin='https://source.example',name='朋友家')
    with pytest.raises(ValueError):await refresh_remote_gifts(site,'k',target,client)
    assert target.collection('k')[0]['presentation_version']==0
    client.fail=False
    assert await refresh_remote_gifts(site,'k',target,client)==1
    calls=client.calls
    assert await refresh_remote_gifts(site,'k',target,client)==0 and client.calls==calls


@pytest.mark.asyncio
async def test_routes_are_owner_and_actor_scoped(gateway):
    app,store=gateway;migrate(store,backup_ready=True);eid=make_home(store)
    store.set_gift('ai',eid,'route-round');receipt=store.visit('visitor:a','ai','机')[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        r=await client.post('/social/v1/decor/gift-updates',json={'gift_ids':[receipt['gift_id']]})
        assert r.status_code==200 and r.json()['items']==[]
        r=await client.post('/social/v1/decor/gift-updates',json={'gift_ids':[receipt['gift_id']]},headers={'x-test-actor':'visitor:a'})
        assert r.status_code==200 and len(r.json()['items'])==1
        assert (await client.put('/social/v1/decor/home',json={})).status_code==403
        assert (await client.post('/social/v1/decor/collection/refresh',json={'subject':'visitor:other'})).status_code==403
