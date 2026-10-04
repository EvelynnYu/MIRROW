"""Temporary decor stores only; human notice migration is always explicit."""
from social_feed.decor_models import HomeDesign
from social_feed.decor_store import DecorStore, DecorError
import pytest


def _gift(store, kind='ai', request_id='gift-notice-round'):
    home = store.save_home(HomeDesign(exhibits=[{'name': '玻璃月亮', 'description': '一份远来礼物'}]))
    store.set_gift(kind, home['exhibits'][0]['id'], request_id)


def test_human_notice_needs_explicit_backup_gated_migration(tmp_path):
    store = DecorStore(tmp_path / 'decor.db')
    _gift(store)
    receipt = store.visit('visitor:machine', 'ai', '小机')[0]
    assert store.human_gift_notices('visitor:human', ['visitor:machine']) == []
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='human_gift_notice_seen'").fetchone()
    try:
        store.migrate_human_gift_notices()
    except DecorError as exc:
        assert str(exc) == 'decor_backup_required'
    else:
        raise AssertionError('backup gate must be required')
    store.migrate_human_gift_notices(backup_ready=True)
    notices = store.human_gift_notices('visitor:human', ['visitor:machine'])
    assert [row['id'] for row in notices] == [receipt['id']]
    assert store.see_human_gift_notices('visitor:human', [receipt['id']], ['visitor:machine']) == 1
    assert store.human_gift_notices('visitor:human', ['visitor:machine']) == []
    assert store.collection('visitor:machine', pending=True)[0]['id'] == receipt['id']


def test_imported_machine_receipt_is_scoped_to_its_gifting_home(tmp_path):
    store = DecorStore(tmp_path / 'decor.db')
    _gift(store)
    snapshot = store.visit('visitor:source', 'ai', '原机')[0]['snapshot']
    store.migrate_human_gift_notices(backup_ready=True)
    store.imported_gift('k', 'https://gift.example', {'gift_id': 'remote-gift', 'snapshot': snapshot}, '赠礼家')
    store.imported_gift('k', 'https://other.example', {'gift_id': 'other-gift', 'snapshot': snapshot}, '别家')
    assert len(store.human_gift_notices('aning', ['k'], origin='https://gift.example')) == 1
    assert len(store.human_gift_notices('aning', ['k'], origin='https://other.example')) == 1
    first = store.human_gift_notices('aning', ['k'], origin='https://gift.example')[0]
    store.see_human_gift_notices('aning', [first['id']], ['k'])
    assert store.human_gift_notices('aning', ['k'], origin='https://gift.example') == []
    assert len(store.human_gift_notices('aning', ['k'], origin='https://other.example')) == 1


def test_human_cannot_mark_an_unmanaged_machine_receipt(tmp_path):
    store = DecorStore(tmp_path / 'decor.db')
    _gift(store)
    receipt = store.visit('visitor:other-machine', 'ai', '别人的机')[0]
    store.migrate_human_gift_notices(backup_ready=True)
    assert store.see_human_gift_notices('visitor:human', [receipt['id']], ['visitor:my-machine']) == 0
    assert store.human_gift_notices('visitor:human', ['visitor:other-machine'])[0]['id'] == receipt['id']


def test_notice_marker_is_per_human_not_a_machine_receipt_mutation(tmp_path):
    store = DecorStore(tmp_path / 'decor.db')
    _gift(store)
    receipt = store.visit('visitor:machine', 'ai', 'AI')[0]
    store.migrate_human_gift_notices(backup_ready=True)
    store.see_human_gift_notices('visitor:human', [receipt['id']], ['visitor:machine'])
    assert store.human_gift_notices('visitor:human', ['visitor:machine']) == []
    assert store.human_gift_notices('visitor:other-human', ['visitor:machine'])[0]['id'] == receipt['id']
    assert store.collection('visitor:machine', pending=True)[0]['id'] == receipt['id']


@pytest.mark.asyncio
async def test_human_notice_routes_require_write_auth_and_household_scope(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI,HTTPException
    from social_feed.decor_router import create_decor_router
    store=DecorStore(tmp_path/'routes.db');_gift(store);store.migrate_human_gift_notices(backup_ready=True)
    receipt=store.visit('visitor:machine','ai','机')[0]
    calls=[]
    def identity(runtime,request,write=False,admission=True):
        calls.append(write)
        if write and request.headers.get('x-test-write')!='allowed':raise HTTPException(403,'csrf_required')
        return request.headers.get('x-test-actor','visitor:human')
    monkeypatch.setattr('social_feed.public_gateway._identity',identity)
    monkeypatch.setattr('social_feed.public_gateway._managed_profiles',lambda runtime,actor:[actor,'visitor:machine'] if actor=='visitor:human' else [actor])
    monkeypatch.setattr('social_feed.public_gateway._display',lambda runtime,actor,viewer:{'name':'机'})
    monkeypatch.setattr('social_feed.decor_router.get_decor_store',lambda:store)
    runtime=SimpleNamespace(visitor_service=SimpleNamespace(effective_visitor=lambda vid:SimpleNamespace(visitor_kind='human' if vid in ('human','other') else 'external_ai')))
    app=FastAPI();app.include_router(create_decor_router(runtime))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.example') as client:
        assert len((await client.get('/social/v1/decor/human-gift-notices')).json()['items'])==1
        for actor in ('visitor:other','visitor:machine'):
            assert (await client.get('/social/v1/decor/human-gift-notices',headers={'x-test-actor':actor})).json()['items']==[]
        assert (await client.post('/social/v1/decor/human-gift-notices/seen',json={'receipt_ids':[receipt['id']]})).status_code==403
        assert calls[-1] is True
        response=await client.post('/social/v1/decor/human-gift-notices/seen',json={'receipt_ids':[receipt['id']]},headers={'x-test-write':'allowed'})
        assert response.json()['marked']==1
        assert store.collection('visitor:machine',pending=True)[0]['id']==receipt['id']
