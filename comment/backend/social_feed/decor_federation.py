"""Ownership proofs and opt-in personal decoration push over registered Keys."""
import json
import time
from urllib.parse import urlsplit
import httpx
from lounge_visits.network_guard import pin_request, public_destination
from .remote_sites import normalize_social_origin, get_remote_site_store
from .remote_client import RemoteSocialClient
from .decor_store import DecorError
from .decor_models import PersonalDesign


async def _read_proof(origin, token):
    origin = normalize_social_origin(origin)
    if not isinstance(token,str) or not 32 <= len(token) <= 80 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in token):
        raise DecorError('invalid_link_proof')
    url = origin + '/social/v1/decor/proofs/' + token
    address = await public_destination(url)
    async def pin(request):
        pin_request(request, urlsplit(origin), address)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, event_hooks={'request':[pin]}) as client:
        async with client.stream('GET',url) as response:
            if response.status_code != 200:
                raise DecorError('link_proof_unavailable')
            raw = bytearray()
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw)>4096:
                    raise DecorError('invalid_link_proof')
    result = json.loads(raw)
    if not isinstance(result,dict):
        raise DecorError('invalid_link_proof')
    return result


async def read_proof(origin, token):
    try:
        return await _read_proof(origin,token)
    except DecorError:
        raise
    except (ValueError,OSError,httpx.HTTPError):
        raise DecorError('link_proof_unavailable') from None


async def retry_pending():
    import asyncio
    from .decor_store import get_decor_store
    from .public_gateway import PUBLIC_SOCIAL_ORIGIN
    store=get_decor_store()
    for subject in ('aning','k'):
        if store.sync_jobs(subject):
            try:
                await asyncio.wait_for(sync_personal(store,subject,PUBLIC_SOCIAL_ORIGIN,enqueue=False),timeout=20)
            except (TimeoutError,ValueError,OSError):
                pass


async def link_site(store, site, subject, home_origin, client=None):
    kind = 'human' if subject == 'aning' else 'ai'
    client = client or RemoteSocialClient()
    me = await client.request(site,kind,'GET','/social/v1/me')
    actor = me.get('actor',{}).get('actor_id','')
    if not actor.startswith('visitor:') or me.get('can_manage_avatar') is not (kind=='human'):
        raise DecorError('link_identity_mismatch')
    body = {'origin':home_origin,'subject':subject,'audience':site.origin,'actor':actor}
    token = store.new_proof(body)
    return await client.request(site,kind,'POST','/social/v1/decor/link',payload={'origin':home_origin,'subject':subject,'token':token})


async def sync_personal(store, subject, home_origin, *, previous=None, enqueue=True):
    choice = store.choice(subject)
    results = []
    def selected(setting, identifier):
        return setting and (setting['scope']=='all' or setting['scope']=='selected' and identifier in setting['site_ids'])
    sites = get_remote_site_store().list()
    if enqueue:
        for site in sites:
            if selected(choice,site.id) or selected(previous,site.id):
                store.queue_sync(subject,site.id,bool(selected(choice,site.id)))
    for job in store.sync_jobs(subject):
        site = next((s for s in sites if s.id==job['site_id']),None)
        active = bool(job['active'])
        if site is None:
            store.finish_sync(subject,job['site_id'],job['generation'])
            continue
        kind = 'human' if subject=='aning' else 'ai'
        if not site.enabled or not (site.human_key if kind=='human' else site.ai_key):
            results.append({'site_id':site.id,'name':site.name,'status':'pending'})
            continue
        try:
            await link_site(store,site,subject,home_origin)
            design = store.personal(subject) if active else PersonalDesign().model_dump()
            payload = {'origin':home_origin,'subject':subject,'design':design,'frame_data':'','card_data':''}
            for field,key in [('frame_asset','frame_data'),('card_asset','card_data')]:
                asset=design.get(field)
                if asset:
                    import base64
                    payload[key] = base64.b64encode((store.media_dir/asset).read_bytes()).decode('ascii')
            import hashlib
            me = await RemoteSocialClient().request(site,kind,'GET','/social/v1/me')
            digest = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            payload['token'] = store.new_proof({'operation':'decor_sync','actor':me['actor']['actor_id'],
                'audience':site.origin,'digest':digest,'version':time.time_ns()})
            await RemoteSocialClient().request(site,kind,'PUT','/social/v1/decor/sync',payload=payload)
            store.finish_sync(subject,site.id,job['generation'])
            results.append({'site_id':site.id,'name':site.name,'status':'success'})
        except (ValueError, httpx.HTTPError, OSError):
            results.append({'site_id':site.id,'name':site.name,'status':'pending'})
    return results


async def receive_remote_gifts(site, subject, store, client=None):
    """Enter as exactly one actor. Import receipts before acknowledging delivery."""
    client = client or RemoteSocialClient()
    kind = 'human' if subject=='aning' else 'ai'
    result = await client.request(site,kind,'POST','/social/v1/decor/visit',payload={})
    gifts = result.get('gifts') or []
    for gift in gifts[:50]:
        from .decor_media import save_media
        gift = dict(gift) | {'snapshot':dict(gift['snapshot'])}
        cached = {}
        for field in ('image','gift_image'):
            identifier = gift['snapshot'].get(field)
            if identifier:
                if identifier not in cached:
                    raw, _ = await client.media(site,kind,identifier)
                    cached[identifier] = save_media(store,subject,raw)['id']
                gift['snapshot'][field] = cached[identifier]
        store.imported_gift(subject,site.origin,gift,site.name)
        await client.request(site,kind,'POST','/social/v1/decor/seen',payload={'id':gift['id']})
    # Old receipts use a separate channel: never re-popup an already seen gift.
    try:
        await refresh_remote_gifts(site,subject,store,client=client)
    except (ValueError,OSError,httpx.HTTPError):
        pass  # Legacy/offline hosts keep the last verified collection.
    return gifts[:50]


async def refresh_remote_gifts(site, subject, store, client=None):
    client = client or RemoteSocialClient()
    kind = 'human' if subject=='aning' else 'ai'
    from .decor_gift_updates import present, ready
    with store.connect() as db:
        if not ready(db):
            return 0
        rows = db.execute('SELECT * FROM collections WHERE actor=? AND origin=? ORDER BY id',
                          (subject,site.origin)).fetchall()
        known = {r['gift_id']:present(db,r) for r in rows}
    changed = 0
    ids = list(known)
    for offset in range(0,len(ids),50):
        requested = ids[offset:offset+50]
        response = await client.request(site,kind,'POST','/social/v1/decor/gift-updates',payload={'gift_ids':requested})
        updates = response.get('items',[])
        if not isinstance(updates,list) or len(updates)>50:
            raise DecorError('invalid_gift_update')
        for receipt in updates:
            if not isinstance(receipt,dict) or not isinstance(receipt.get('snapshot'),dict):
                raise DecorError('invalid_gift_update')
            gift_id = receipt.get('gift_id')
            version = receipt.get('presentation_version',0)
            if gift_id not in requested or type(version) is not int or not 0<=version<2**63:
                raise DecorError('invalid_gift_update')
            if version <= known[gift_id]['presentation_version']:
                continue
            from .decor_models import Exhibit
            Exhibit.model_validate(receipt['snapshot'])
            if receipt['snapshot']['id'] != known[gift_id]['snapshot']['id']:
                raise DecorError('invalid_gift_update')
            gift = dict(receipt) | {'snapshot':dict(receipt['snapshot'])}
            cached = {}
            from .decor_media import save_media
            for field in ('image','gift_image'):
                identifier = gift['snapshot'].get(field)
                if identifier:
                    if identifier not in cached:
                        raw,_ = await client.media(site,kind,identifier)
                        cached[identifier] = save_media(store,subject,raw)['id']
                    gift['snapshot'][field] = cached[identifier]
            store.imported_gift(subject,site.origin,gift,site.name)
            changed += 1
    return changed
