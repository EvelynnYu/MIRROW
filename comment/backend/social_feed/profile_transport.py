"""Profile-only HTTPS exchange: no Visitor Key, redirect or arbitrary route."""
import asyncio
import json
import weakref
from urllib.parse import urlsplit

import httpx
from lounge_visits.network_guard import pin_request, public_destination
from lounge_visits.secret_vault import unprotect
from .public_wall import PublicWallError
from .remote_sites import normalize_social_origin
from .profile_identity import ProfileIdentityStore, decode_code


class ProfileTransport:
    def __init__(self, transport=None):
        self.transport=transport

    async def request(self, origin, operation, body):
        if operation not in {'claim','read'}:
            raise PublicWallError('profile_request_invalid')
        try:
            origin=normalize_social_origin(origin)
            url=origin+'/social/v1/profile-links/'+operation
            address=await public_destination(url)
        except (ValueError,OSError):
            raise PublicWallError('profile_source_unavailable') from None
        async def pin(request):
            pin_request(request,urlsplit(origin),address)
        try:
            async with httpx.AsyncClient(timeout=4, transport=self.transport, trust_env=False,
                    follow_redirects=False,event_hooks={'request':[pin]}) as client:
                async with client.stream('POST',url,json=body) as response:
                    if response.status_code in {401,403,410}:
                        raise PublicWallError('profile_access_revoked')
                    if response.status_code!=200:
                        raise PublicWallError('profile_source_unavailable')
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>450_000:
                            raise PublicWallError('profile_response_invalid')
            result=json.loads(raw)
            if not isinstance(result,dict):
                raise PublicWallError('profile_response_invalid')
            return result
        except PublicWallError:
            raise
        except (httpx.HTTPError,OSError,ValueError,TimeoutError):
            raise PublicWallError('profile_source_unavailable') from None


async def accept(wall, actor, kind, code, home_origin, client=None):
    store=ProfileIdentityStore(wall)
    store.require_ready()
    if store.link(actor):
        raise PublicWallError('profile_link_conflict')
    origin,ticket=decode_code(code)
    if origin==home_origin:
        raise PublicWallError('profile_link_to_self')
    from .profile_registration import guard_origin_in
    with wall._connect() as db:
        guard_origin_in(db, actor, origin)
    response=await (client or ProfileTransport()).request(origin,'claim',{
        'ticket':ticket,'audience':home_origin,'consumer':actor})
    if response.get('audience')!=home_origin or response.get('consumer')!=actor:
        raise PublicWallError('profile_response_invalid')
    return store.cache(actor,origin,kind,response.get('token',''),response.get('profile') or {},public_origin=home_origin)


async def refresh(wall, actor, home_origin, client=None):
    store=ProfileIdentityStore(wall)
    link=store.link(actor)
    if not link:
        return {'linked':False}
    try:
        result=await (client or ProfileTransport()).request(link['origin'],'read',{
            'token':unprotect(link['encrypted_token'])})
        if result.get('audience')!=home_origin or result.get('consumer')!=actor:
            raise PublicWallError('profile_response_invalid')
        return store.cache(actor,link['origin'],link['kind'],unprotect(link['encrypted_token']),
                           result.get('profile') or {},refresh=True,public_origin=home_origin,
                           expected_token=link['encrypted_token'])
    except (PublicWallError,ValueError,RuntimeError,OSError) as exc:
        store.mark_failure(actor,link['encrypted_token'],revoked=str(exc)=='profile_access_revoked')
        return store.status(actor)


_locks=weakref.WeakKeyDictionary()


async def refresh_due(wall, home_origin):
    """At most one due profile per authenticated wall read; never block on a queue."""
    loop=asyncio.get_running_loop()
    locks=_locks.setdefault(loop,{})
    lock=locks.setdefault(str(wall.path),asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        due=ProfileIdentityStore(wall).due()
        if due:
            await refresh(wall,due['actor'],home_origin)
