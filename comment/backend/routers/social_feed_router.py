"""Thin HTTP boundary for A-Ning's side of the private social feed."""

from typing import Literal, Optional
import ipaddress

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from social_feed import get_social_feed_store
from social_feed.store import SocialFeedError


router = APIRouter(prefix="/api/social-feed", tags=["social-feed"])
from social_feed.forward_router import router as forward_router
router.include_router(forward_router)


@router.post('/owner-ticket')
def owner_ticket(request: Request):
    """Only the local desktop UI can start an owner session on the public wall."""
    peer = request.client.host if request.client else ''
    try:
        local_peer = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local_peer = False
    host = request.headers.get('host', '').lower()
    origin = request.headers.get('origin', '')
    if (not local_peer or host not in {'127.0.0.1:8005', 'localhost:8005'}
            or origin not in {'http://127.0.0.1:5173', 'http://localhost:5173',
                              'http://127.0.0.1:8005', 'http://localhost:8005'}
            or request.headers.get('x-mirrow-owner-pair') != '1'):
        raise HTTPException(403, 'owner_pairing_requires_local_ui')
    from social_feed.owner_access import issue_owner_ticket
    from social_feed.public_gateway import PUBLIC_SOCIAL_ORIGIN
    return JSONResponse({'ticket': issue_owner_ticket(), 'origin': PUBLIC_SOCIAL_ORIGIN},
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


class MomentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=1200)
    visibility: Literal["private", "public"] = "private"
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)
    forward_ref: dict | None = None


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=600)
    reply_to_id: Optional[str] = None
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)


class MomentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=1200)
    revision: int = Field(default=1, ge=1)


class MarkReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_ids: list[str] = Field(default_factory=list)


class VisibilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Literal["private", "public"]


class RemarkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str = Field(default='', max_length=40)


class LocalProfileUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nickname: str = Field(min_length=1, max_length=40)


def _owner_person(actor_id: str) -> dict:
    from lounge_reception.runtime import current_runtime
    from social_feed.public_gateway import _display
    from social_feed.public_wall import get_public_wall
    runtime = current_runtime()
    if runtime is None:
        raise HTTPException(503, '公开朋友圈接待尚未就绪')
    if not get_public_wall().known_actor(actor_id):
        raise HTTPException(404, 'person_not_found')
    return _display(runtime, actor_id, viewer='aning')


@router.get('/mention-people')
def mention_people(q: str = Query('', max_length=40), visibility: Literal['private','public'] = 'public'):
    from lounge_reception.runtime import current_runtime
    from social_feed.public_wall import get_public_wall
    from social_feed.mentions import candidates
    runtime = current_runtime()
    if runtime is None:
        raise HTTPException(503, 'social_identity_not_ready')
    result = candidates(runtime, get_public_wall(), 'aning', private=visibility=='private', query=q)
    for person in result['items']:
        _local_avatar(person)
    return result


def _local_avatar(person: dict) -> dict:
    from social_feed.public_gateway import PUBLIC_SOCIAL_ORIGIN
    prefix = PUBLIC_SOCIAL_ORIGIN + '/social/v1/avatars/'
    url = person.get('avatar', '')
    profile_prefix = PUBLIC_SOCIAL_ORIGIN + '/social/v1/profile-links/avatars/'
    if url.startswith(profile_prefix):
        person['avatar'] = '/api/social-feed/profile-avatars/' + url[len(profile_prefix):]
        return person
    if url.startswith(prefix):
        digest = url[len(prefix):].split('?', 1)[0]
        if len(digest) == 68 and digest.endswith('.png') and all(c in '0123456789abcdef' for c in digest[:-4]):
            from social_feed.avatar_images import AVATAR_DIR
            target = AVATAR_DIR / digest
            version = f'?v={target.stat().st_mtime_ns}' if target.is_file() else ''
            person['avatar'] = '/api/social-feed/avatars/' + digest + version
    return person


@router.get('/avatars/{digest}.png')
def public_avatar_for_local_ui(digest: str):
    from social_feed.avatar_images import AVATAR_DIR
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise HTTPException(404, 'avatar_not_found')
    target = AVATAR_DIR / (digest + '.png')
    if not target.is_file():
        raise HTTPException(404, 'avatar_not_found')
    return FileResponse(target, media_type='image/png', headers={'Cache-Control': 'private, max-age=300'})


@router.get('/profile-avatars/{digest}.png')
def linked_profile_avatar(digest: str):
    from social_feed.public_wall import get_public_wall
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise HTTPException(404,'avatar_not_found')
    path=get_public_wall().path.parent/'social_profile_avatars'/(digest+'.png')
    if not path.is_file():
        raise HTTPException(404,'avatar_not_found')
    return FileResponse(path,media_type='image/png',headers={'X-Content-Type-Options':'nosniff'})


@router.get('/people/{actor_id}')
def person_card(actor_id: str):
    return {'person': _local_avatar(_owner_person(actor_id))}


@router.post('/people/{actor_id}/avatar')
async def update_local_wall_avatar(actor_id: str, request: Request):
    """Update only the two local wall identities, never chat/persona avatars."""
    if actor_id not in {'aning', 'k'}:
        raise HTTPException(403, 'wall_avatar_owner_only')
    from social_feed.avatar_images import MAX_UPLOAD_BYTES, save_avatar
    from social_feed.public_gateway import PUBLIC_SOCIAL_ORIGIN
    from social_feed.public_wall import get_public_wall
    from social_feed.profile_identity import ProfileIdentityStore
    try:
        ProfileIdentityStore(get_public_wall()).guard_write(actor_id)
    except ValueError as exc:
        raise HTTPException(403,str(exc)) from None
    if request.headers.get('content-type', '').split(';', 1)[0].lower() not in {'image/png', 'image/jpeg'}:
        raise HTTPException(415, 'image_type_required')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, 'request_too_large')
    try:
        target = save_avatar(actor_id, bytes(raw))
    except ValueError:
        raise HTTPException(415, 'invalid_avatar_image') from None
    get_public_wall().set_profile(actor_id, avatar=PUBLIC_SOCIAL_ORIGIN + '/social/v1/avatars/' + target.name
                                  + f'?v={target.stat().st_mtime_ns}')
    return {'person': _local_avatar(_owner_person(actor_id))}


@router.put('/people/{actor_id}/profile')
def update_local_wall_profile(actor_id: str, body: LocalProfileUpdate):
    if actor_id not in {'aning','k'}:
        raise HTTPException(403,'wall_profile_owner_only')
    from social_feed.public_wall import PublicWallError,get_public_wall
    try:
        get_public_wall().set_profile(actor_id,nickname=body.nickname)
    except PublicWallError as exc:
        raise HTTPException(403 if str(exc)=='profile_edit_at_source' else 400,str(exc)) from None
    return {'person':_local_avatar(_owner_person(actor_id))}


@router.put('/people/{actor_id}/remark')
def person_remark(actor_id: str, body: RemarkUpdate):
    from social_feed.public_wall import PublicWallError, get_public_wall
    from social_feed.public_gateway import _cognition_name
    if _cognition_name(actor_id):
        raise HTTPException(403, 'remark_locked_by_cognition')
    try:
        get_public_wall().set_remark('aning', actor_id, body.remark)
    except PublicWallError as exc:
        raise HTTPException(404 if str(exc) == 'person_not_found' else 400, str(exc)) from None
    return {'person': _local_avatar(_owner_person(actor_id))}


def _domain_error(exc: SocialFeedError) -> HTTPException:
    error = str(exc)
    status = 404 if error in {"moment_not_found", "reply_not_found", "comment_not_found"} else 403 if error == "forbidden" else 409 if error == "revision_conflict" else 400
    return HTTPException(status_code=status, detail=error)


@router.get("/moments")
async def list_moments(
    day: Optional[str] = Query(None),
    anchor_day: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    visibility_filter: Optional[str] = Query(None, pattern='^(all|public|private)$'),
):
    try:
        from social_feed.public_wall import get_public_wall
        from social_feed.public_gateway import PUBLIC_SOCIAL_ORIGIN
        from social_feed.profile_transport import refresh_due
        await refresh_due(get_public_wall(),PUBLIC_SOCIAL_ORIGIN)
        result = await get_social_feed_store().list_moments(
            day=day,
            anchor_day=anchor_day,
            cursor=cursor,
            limit=limit,
            visibility=None if visibility_filter in {None, 'all'} else visibility_filter,
        )
        from lounge_reception.runtime import current_runtime
        from social_feed.public_gateway import _decorate
        runtime = current_runtime()
        if runtime:
            result['items'] = [_decorate(runtime, item, viewer='aning') for item in result['items']]
            for item in result['items']:
                for person in item.get('people', {}).values():
                    _local_avatar(person)
        return result
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.post("/moments")
async def create_moment(body: MomentCreate):
    try:
        result = await get_social_feed_store().create_moment(
            "aning", body.content, visibility=body.visibility, mention_actor_ids=body.mention_actor_ids,
            forward_ref=body.forward_ref
        )
        return result
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.get('/moments/{moment_id}')
async def moment_detail(moment_id: str):
    item = await get_social_feed_store().get_moment(moment_id)
    if not item:
        raise HTTPException(404,'moment_not_found')
    from lounge_reception.runtime import current_runtime
    from social_feed.public_gateway import _decorate
    runtime = current_runtime()
    if runtime:
        item = _decorate(runtime,item,viewer='aning')
        for person in item.get('people',{}).values():
            _local_avatar(person)
    return item


@router.delete("/moments/{moment_id}")
async def delete_moment(moment_id: str):
    """站主可删除自己的动态或移除公开墙的访客动态。"""
    try:
        return await get_social_feed_store().delete_moment(moment_id, "aning", allow_owner_override=True)
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.patch("/moments/{moment_id}")
async def edit_moment(moment_id: str, body: MomentEdit):
    """站主只能修改自己发布的动态；公开帖按 revision 防覆盖。"""
    try:
        return await get_social_feed_store().edit_moment(moment_id, 'aning', body.content, body.revision)
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.patch("/moments/{moment_id}/visibility")
async def set_moment_visibility(moment_id: str, body: VisibilityUpdate):
    """站主可切换任意一条动态的可见性（含 AI 的动态）。"""
    try:
        return await get_social_feed_store().set_moment_visibility(
            moment_id,
            "aning",
            body.visibility,
            allow_owner_override=True,
        )
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.post("/moments/{moment_id}/comments")
async def add_comment(moment_id: str, body: CommentCreate):
    try:
        return await get_social_feed_store().add_comment(
            moment_id, "aning", body.content, reply_to_id=body.reply_to_id, mention_actor_ids=body.mention_actor_ids
        )
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.delete("/moments/{moment_id}/comments/{comment_id}")
async def delete_comment(moment_id: str, comment_id: str):
    """Mirror the public wall's author/owner deletion rule in the local UI."""
    try:
        return await get_social_feed_store().delete_comment(moment_id, comment_id, "aning")
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.post("/moments/{moment_id}/like")
async def toggle_like(moment_id: str):
    try:
        return await get_social_feed_store().toggle_like(moment_id, "aning")
    except SocialFeedError as exc:
        raise _domain_error(exc) from exc


@router.get("/notifications")
async def unread_notifications():
    items = await get_social_feed_store().unread_notifications("aning")
    return {"items": items, "count": len(items)}


@router.post("/notifications/mark-read")
async def mark_notifications_read(body: MarkReadBody):
    count = await get_social_feed_store().mark_notifications_read(
        "aning", body.notification_ids, read_source="ui"
    )
    return {"ok": True, "count": count}
