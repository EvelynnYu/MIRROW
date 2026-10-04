"""Local owner interface for registered remote social walls.

Outbound credentials never leave this router.  These records are not lounge
friends and neither hostname nor Key is inferred from a lounge invitation.
"""

from __future__ import annotations

import json
from urllib.parse import quote
import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from lounge_visits.network_guard import public_destination
from lounge_visits import storage as lounge_storage
from routers.lounge_reception_router import local_ui
from social_feed.remote_client import RemoteSocialClient
from social_feed.remote_sites import RemoteSiteError, get_remote_site_store, normalize_social_origin
from social_feed.public_gateway import PUBLIC_SOCIAL_ORIGIN
from social_feed.public_wall import PublicWallError, get_public_wall
from social_feed.migration import inbound_by_id, inbound_pending, discard_staged, ready as transfer_ready
from social_feed.transfer_runner import move_hosted_post


router = APIRouter(prefix='/api/social-sites', tags=['已注册的共域'],
                   dependencies=[Depends(local_ui)])


class SiteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    origin: str = Field(min_length=12, max_length=300)
    human_key: str | None = Field(default=None, max_length=256)
    ai_key: str | None = Field(default=None, max_length=256)
    import_lounge_friend_id: str = Field(default='', max_length=64)
    enabled: bool = True


class RemotePost(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=1200)
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)
    forward_ref: dict | None = None


class RemoteComment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=600)
    reply_to_id: str | None = None
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)


class InboxRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    notification_ids: list[str] = Field(max_length=100)


class TransferChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: str = Field(pattern=r'^(human|ai)$')


class RemoteRemark(BaseModel):
    model_config = ConfigDict(extra='forbid')
    remark: str = Field(default='', max_length=40)


def _site(site_id: str):
    try:
        return get_remote_site_store().get(site_id)
    except RemoteSiteError:
        raise HTTPException(404, 'social_site_not_found') from None


async def _remote(site_id: str, method: str, path: str, *, payload=None, params=None):
    site = _site(site_id)
    try:
        client = RemoteSocialClient()
        if path != '/social/v1/me':
            identity = await client.request(site, 'human', 'GET', '/social/v1/me')
            if identity.get('can_manage_avatar') is not True or not (identity.get('actor') or {}).get('actor_id'):
                raise RemoteSiteError('social_site_key_kind_mismatch')
            if payload and payload.get('mention_actor_ids') and 'mentions_v1' not in identity.get('capabilities', []):
                raise RemoteSiteError('social_site_mentions_unavailable')
        return await client.request(site, 'human', method, path, payload=payload, params=params)
    except RemoteSiteError as exc:
        code = 403 if str(exc) in {'social_site_identity_unavailable', 'social_site_key_rejected',
                                   'social_site_key_kind_mismatch'} else 502
        raise HTTPException(code, str(exc)) from None


@router.get('')
def list_sites():
    return {'sites': [site.public_dict() for site in get_remote_site_store().list()]}


@router.get('/timeline')
async def timeline(limit: int = Query(30, ge=1, le=30),
                   visibility_filter: str = Query('all', pattern='^(all|public|private)$'),
                   cursor: str | None = Query(None, max_length=16000)):
    from social_feed import get_social_feed_store
    from social_feed.timeline import SocialTimeline, TimelineError
    try:
        result = await SocialTimeline(get_social_feed_store(), get_remote_site_store().list()).read(
            limit=limit, visibility=visibility_filter, cursor=cursor)
        from lounge_reception.runtime import current_runtime
        from social_feed.public_gateway import _decorate
        from routers.social_feed_router import _local_avatar
        runtime = current_runtime()
        for index, item in enumerate(result['items']):
            if not item['source']['site_id'] and runtime:
                item = _decorate(runtime, item, viewer='aning')
                for person in item.get('people', {}).values():
                    _local_avatar(person)
                result['items'][index] = item
        return result
    except TimelineError:
        raise HTTPException(400, 'invalid_timeline_request') from None


@router.get('/timeline/stream')
async def timeline_stream(limit: int = Query(30, ge=1, le=30),
                          visibility_filter: str = Query('all', pattern='^(all|public|private)$'),
                          cursor: str | None = Query(None, max_length=16000)):
    from social_feed import get_social_feed_store
    from social_feed.timeline import SocialTimeline, TimelineError
    events = SocialTimeline(get_social_feed_store(), get_remote_site_store().list()).events(
        limit=limit, visibility=visibility_filter, cursor=cursor)
    try:
        first = await anext(events)  # Validate the cursor before HTTP headers are committed.
    except TimelineError:
        await events.aclose()
        raise HTTPException(400, 'invalid_timeline_request') from None

    async def lines():
        from lounge_reception.runtime import current_runtime
        from social_feed.public_gateway import _decorate
        from routers.social_feed_router import _local_avatar
        try:
            yield json.dumps(first, ensure_ascii=False) + '\n'
            async for event in events:
                rows = event.get('items', []) if event['type'] == 'source' else event.get('page', {}).get('items', [])
                runtime = current_runtime()
                for index, item in enumerate(rows):
                    if not item['source']['site_id'] and runtime:
                        item = _decorate(runtime, item, viewer='aning')
                        for person in item.get('people', {}).values():
                            _local_avatar(person)
                        rows[index] = item
                yield json.dumps(event, ensure_ascii=False) + '\n'
        finally:
            await events.aclose()
    return StreamingResponse(lines(), media_type='application/x-ndjson',
                             headers={'Cache-Control':'no-store', 'X-Accel-Buffering':'no'})


@router.get('/{site_id}/decor/media/{identifier}')
async def decor_media(site_id: str, identifier: str):
    from fastapi.responses import Response
    try:
        raw,mime = await RemoteSocialClient().media(_site(site_id),'human',identifier)
        return Response(raw,media_type=mime,headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})
    except (ValueError,OSError,httpx.HTTPError):
        raise HTTPException(502,'共域物料暂时不可用') from None


@router.post('/{site_id}/decor/media/{subject}')
async def upload_remote_decor(site_id: str, subject: str, request: Request):
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>10*1024*1024:
            raise HTTPException(413,'图片超过 10 MB')
    try:
        return await RemoteSocialClient().upload_media(_site(site_id),subject,bytes(raw))
    except (ValueError,OSError,httpx.HTTPError):
        raise HTTPException(502,'对方暂时无法接收这张图片') from None


@router.get('/{site_id}/avatars/{identifier}')
async def remote_avatar(site_id: str, identifier: str, linked: bool=False):
    from fastapi.responses import Response
    try:
        raw,mime = await RemoteSocialClient().media(_site(site_id),'human',identifier,avatar=True,profile_avatar=linked)
        return Response(raw,media_type=mime,headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})
    except (ValueError,OSError,httpx.HTTPError):
        raise HTTPException(502,'共域头像暂时不可用') from None


@router.api_route('/{site_id}/decor/{operation:path}',methods=['GET','POST','PUT','DELETE'])
async def remote_decor(site_id: str, operation: str, request: Request):
    from social_feed.decor_federation import receive_remote_gifts
    from social_feed.decor_store import get_decor_store
    from social_feed.decor_events import publish_pending
    import re
    allowed = (request.method=='GET' and operation in {'state','collection','people','presets'} or
               request.method=='POST' and (operation in {'visit','seen','collection/refresh'} or re.fullmatch(r'(presets|gif)/visitor:[A-Za-z0-9_-]+',operation)) or
               request.method=='PUT' and re.fullmatch(r'people/visitor:[A-Za-z0-9_-]+',operation) or
               request.method=='DELETE' and re.fullmatch(r'link/visitor:[A-Za-z0-9_-]+',operation))
    if not allowed:
        raise HTTPException(404,'not_found')
    if operation=='visit':
        try:
            await receive_remote_gifts(_site(site_id),'aning',get_decor_store())
        except (ValueError,OSError,httpx.HTTPError):
            return {'gifts':[], 'delivery_pending':True}
        await publish_pending()
        return {'gifts':[r for r in get_decor_store().collection('aning',pending=True) if r['origin']==_site(site_id).origin], 'local_receipts':True}
    body = None
    if request.method in {'POST','PUT'}:
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>(14_000_000 if operation.startswith('gif/') else 40_000):
                raise HTTPException(413,'request_too_large')
        try:
            import json
            body = json.loads(raw)
        except (ValueError,TypeError):
            raise HTTPException(400,'invalid_request') from None
    return await _remote(site_id,request.method,'/social/v1/decor/'+operation,payload=body,params=dict(request.query_params))


@router.get('/migrations/pending')
def pending_migrations():
    try:
        return {'items': inbound_pending(get_public_wall())}
    except PublicWallError as exc:
        raise HTTPException(503, str(exc)) from None


@router.get('/migrations/capability')
def migration_capability():
    wall = get_public_wall()
    with wall._connect() as db:
        return {'enabled': transfer_ready(db)}


@router.get('/lounge-imports')
def lounge_imports():
    return {'friends': [{'id': friend.id, 'name': friend.display_name,
                         'has_ai_key': bool(friend.visitor_key)}
                        for friend in lounge_storage.friends().list_for_actor('k')]}


@router.post('')
@router.put('/{site_id}')
async def save_site(body: SiteInput, site_id: str = ''):
    store = get_remote_site_store()
    try:
        old = store.get(site_id) if site_id else None
        origin = normalize_social_origin(body.origin)
        await public_destination(origin)
        ai_key = body.ai_key if body.ai_key is not None else old.ai_key if old else ''
        human_key = body.human_key if body.human_key is not None else old.human_key if old else ''
        if body.import_lounge_friend_id:
            friend = lounge_storage.friends().get_owned('k', body.import_lounge_friend_id)
            if body.ai_key is not None:
                raise RemoteSiteError('choose_one_ai_key_source')
            ai_key = friend.visitor_key
        saved = store.save(name=body.name, origin=origin, human_key=human_key,
                           ai_key=ai_key, enabled=body.enabled, site_id=site_id)
    except KeyError:
        raise HTTPException(404, 'lounge_friend_not_found') from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    from social_feed.decor_store import get_decor_store
    decor = get_decor_store()
    for subject in ('aning','k'):
        choice = decor.choice(subject)
        if choice['scope']=='all' or choice['scope']=='selected' and saved.id in choice['site_ids']:
            decor.queue_sync(subject,saved.id,True)
    return {'site': saved.public_dict()}


@router.delete('/{site_id}')
def delete_site(site_id: str):
    try:
        get_remote_site_store().delete(site_id)
    except RemoteSiteError:
        raise HTTPException(404, 'social_site_not_found') from None
    return {'ok': True}


@router.post('/{site_id}/test')
async def test_site(site_id: str):
    site = _site(site_id)
    client = RemoteSocialClient()
    result = {}
    for actor in ('human', 'ai'):
        if not (site.human_key if actor == 'human' else site.ai_key):
            result[actor] = {'status': 'not_configured'}
            continue
        try:
            response = await client.request(site, actor, 'GET', '/social/v1/me')
            if not isinstance(response.get('actor'), dict) or not response['actor'].get('actor_id'):
                raise RemoteSiteError('social_response_invalid')
            if response.get('can_manage_avatar') is not (actor == 'human'):
                raise RemoteSiteError('social_site_key_kind_mismatch')
            result[actor] = {'status': 'connected', 'name': str(response['actor'].get('name') or '')[:80]}
        except RemoteSiteError as exc:
            result[actor] = {'status': 'failed', 'reason': str(exc)}
    return {'site_id': site.id, 'identities': result}


@router.post('/{site_id}/profile-sync')
async def sync_site_profiles(site_id: str):
    """An explicit owner action, unlike saving credentials or read-only testing."""
    from social_feed.profile_key_link import sync_home_profiles
    result = await sync_home_profiles(get_public_wall(), _site(site_id), PUBLIC_SOCIAL_ORIGIN,
                                      client=RemoteSocialClient())
    return {'site_id': site_id, 'profiles': result}


@router.get('/{site_id}/me')
async def remote_me(site_id: str):
    return await _remote(site_id, 'GET', '/social/v1/me')


@router.get('/{site_id}/me/ai')
async def remote_ai_me(site_id: str):
    site = _site(site_id)
    if not site.ai_key:
        return {'actor': None}
    try:
        result = await RemoteSocialClient().request(site, 'ai', 'GET', '/social/v1/me')
        if result.get('can_manage_avatar') is not False:
            raise RemoteSiteError('social_site_key_kind_mismatch')
        return result
    except RemoteSiteError as exc:
        raise HTTPException(502, str(exc)) from None


@router.get('/{site_id}/moments')
async def moments(site_id: str, limit: int = Query(default=20, ge=1, le=50),
                  before_time: float | None = None, before_id: str = ''):
    params = {'limit': limit}
    if before_time is not None and before_id:
        params.update(before_time=before_time, before_id=before_id)
    return await _remote(site_id, 'GET', '/social/v1/moments', params=params)


@router.get('/{site_id}/people/{actor_id}')
async def remote_person(site_id: str, actor_id: str):
    return await _remote(site_id, 'GET', '/social/v1/people/' + quote(actor_id, safe=''))


@router.put('/{site_id}/people/{actor_id}/remark')
async def remote_remark(site_id: str, actor_id: str, body: RemoteRemark):
    # The remote human Key owns this viewer's remark. A remote actor is never
    # looked up in the local cognition registry or treated as the local owner.
    return await _remote(site_id, 'PUT', '/social/v1/people/' + quote(actor_id, safe='') + '/remark',
                         payload={'remark': body.remark})


@router.get('/{site_id}/moments/{moment_id}')
async def moment(site_id: str, moment_id: str):
    return await _remote(site_id, 'GET', '/social/v1/moments/' + quote(moment_id, safe=''))


@router.post('/{site_id}/moments/{moment_id}/migrate')
async def migrate_moment(site_id: str, moment_id: str, body: TransferChoice):
    site = _site(site_id)
    try:
        return await move_hosted_post(get_public_wall(), site, body.actor, moment_id,
                                      PUBLIC_SOCIAL_ORIGIN)
    except PublicWallError as exc:
        code = 503 if str(exc) == 'transfer_schema_not_ready' else 409
        raise HTTPException(code, str(exc)) from None
    except RemoteSiteError as exc:
        code = 403 if str(exc) in {'social_site_identity_unavailable', 'social_site_key_rejected'} else 502
        raise HTTPException(code, str(exc)) from None


@router.post('/{site_id}/migrations/{transfer_id}/cancel')
async def cancel_migration(site_id: str, transfer_id: str):
    site = _site(site_id)
    try:
        row = inbound_by_id(get_public_wall(), transfer_id)
        if not row or row['source_origin'] != site.origin or row['state'] != 'staged':
            raise PublicWallError('transfer_not_found')
        actor = 'human' if row['target_actor'] == 'aning' else 'ai'
        result = await RemoteSocialClient().request(site, actor, 'POST',
                                                     '/social/v1/transfers/' + transfer_id + '/cancel')
        if result.get('state') != 'cancelled':
            raise PublicWallError('transfer_cancel_unconfirmed')
        discard_staged(get_public_wall(), transfer_id)
        return {'state': 'cancelled'}
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    except RemoteSiteError as exc:
        raise HTTPException(502, str(exc)) from None


@router.post('/{site_id}/moments')
async def post_moment(site_id: str, body: RemotePost):
    payload = {'content': body.content}
    if body.mention_actor_ids:
        payload['mention_actor_ids'] = body.mention_actor_ids
    if body.forward_ref is not None:
        payload['forward_ref'] = body.forward_ref
    return await _remote(site_id, 'POST', '/social/v1/moments', payload=payload)


@router.post('/{site_id}/moments/{moment_id}/comments')
async def add_comment(site_id: str, moment_id: str, body: RemoteComment):
    return await _remote(site_id, 'POST', '/social/v1/moments/' + quote(moment_id, safe='') + '/comments',
                         payload=body.model_dump(exclude={'mention_actor_ids'}) | (
                             {'mention_actor_ids': body.mention_actor_ids} if body.mention_actor_ids else {}))


@router.get('/{site_id}/mention-people')
async def mention_people(site_id: str, q: str = Query('', max_length=40)):
    return await _remote(site_id, 'GET', '/social/v1/mention-people', params={'q':q,'limit':30})


@router.get('/{site_id}/notifications')
async def mention_notices(site_id: str):
    return await _remote(site_id, 'GET', '/social/v1/notifications', params={'limit':30})


@router.post('/{site_id}/notifications/read')
async def read_mentions(site_id: str, body: InboxRead):
    return await _remote(site_id, 'POST', '/social/v1/notifications/read', payload=body.model_dump())


@router.post('/{site_id}/moments/{moment_id}/like')
async def like(site_id: str, moment_id: str):
    return await _remote(site_id, 'POST', '/social/v1/moments/' + quote(moment_id, safe='') + '/like')


@router.delete('/{site_id}/moments/{moment_id}/comments/{comment_id}')
async def remove_comment(site_id: str, moment_id: str, comment_id: str):
    return await _remote(site_id, 'DELETE', '/social/v1/moments/' + quote(moment_id, safe='')
                         + '/comments/' + quote(comment_id, safe=''))
