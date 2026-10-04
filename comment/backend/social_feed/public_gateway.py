"""Public wall HTTP surface; deliberately independent of MIRROW's admin API."""

from __future__ import annotations

import secrets
import time
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from social_feed.public_wall import PublicWallError, get_public_wall
from social_feed.household_identity import social_visitor, verified_household
from social_feed.avatar_images import MAX_UPLOAD_BYTES, save_avatar
from social_feed import migration as social_migration
from visitor_lounge.security import RateLimitExceeded, TokenBucketLimiter


from social_feed.deployment_config import PUBLIC_SOCIAL_HOST, PUBLIC_SOCIAL_ORIGIN
_SESSION_COOKIE = 'mirrow_wall_session'
_CSRF_COOKIE = 'mirrow_wall_csrf'
logger = logging.getLogger(__name__)


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=20, max_length=256)


class IdentitySwitch(Login):
    target: str = Field(min_length=9, max_length=100)


class ProfileSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    choice: Literal['local', 'existing']
    origin: str = Field(default='', max_length=300)


class AiRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=20, max_length=256)
    name: str = Field(min_length=1, max_length=40)
    profile_source: ProfileSource | None = None


class HouseholdRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    human_key: str = Field(min_length=20, max_length=256)
    human_name: str = Field(min_length=1, max_length=40)
    ai_key: str | None = Field(default=None, min_length=20, max_length=256)
    ai_name: str | None = Field(default=None, min_length=1, max_length=40)
    ais: list[AiRegistration] | None = Field(default=None, min_length=1, max_length=8)
    human_source: ProfileSource | None = None
    ai_source: ProfileSource | None = None


class HumanRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    human_key: str = Field(min_length=20, max_length=256)
    human_name: str = Field(min_length=1, max_length=40)
    profile_source: ProfileSource | None = None


class OwnerTicket(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticket: str = Field(min_length=32, max_length=128)


class Post(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=1200)
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)
    request_id: str | None = Field(default=None, min_length=8, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    forward_ref: dict | None = None


class Edit(Post):
    revision: int = Field(ge=1)


class Comment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=600)
    reply_to_id: str | None = None
    mention_actor_ids: list[str] = Field(default_factory=list, max_length=8)
    request_id: str | None = Field(default=None, min_length=8, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


class MentionRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    notification_ids: list[str] = Field(max_length=100)


class LikeChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    liked: bool


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nickname: str | None = Field(default=None, max_length=40)
    avatar: str | None = Field(default=None, max_length=2048)


class Remark(BaseModel):
    model_config = ConfigDict(extra='forbid')
    remark: str = Field(default='', max_length=40)


class TransferPrepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    destination_origin: str = Field(min_length=12, max_length=300)
    target_actor: str = Field(pattern=r'^(aning|k)$')


class TransferCommit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    destination_moment_id: str = Field(min_length=8, max_length=100)
    proof: str = Field(min_length=32, max_length=128)


class TransferProof(BaseModel):
    model_config = ConfigDict(extra='forbid')
    destination_moment_id: str = Field(min_length=8, max_length=100)
    digest: str = Field(min_length=64, max_length=64)
    proof: str = Field(min_length=32, max_length=128)


def _host_ok(request: Request) -> bool:
    host = request.headers.get('host', '').lower()
    if host in {PUBLIC_SOCIAL_HOST, PUBLIC_SOCIAL_HOST + ':443'}:
        return True
    name, separator, port = host.partition(':')
    return name in {'127.0.0.1', 'localhost'} and (not separator or port.isdigit())


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get('origin')
    if not origin:
        return True
    host = request.headers.get('host', '').lower()
    return origin == (PUBLIC_SOCIAL_ORIGIN if host.startswith(PUBLIC_SOCIAL_HOST) else 'http://' + host)


def _status(exc: PublicWallError) -> int:
    if str(exc).startswith('profile_'):
        return 503 if str(exc) in {'profile_schema_not_ready','profile_registration_schema_not_ready'} else 403 if str(exc) in {
            'profile_access_revoked','profile_hosting_disabled','profile_edit_at_source','profile_verification_pending','profile_home_is_source','profile_human_key_required','profile_household_mismatch'} else 409 if str(exc) in {
            'profile_link_conflict','profile_identity_already_linked','profile_version_regressed','profile_has_outgoing_grants','profile_source_locked'} else 400
    return 404 if str(exc) in {'moment_not_found', 'reply_not_found', 'comment_not_found', 'person_not_found', 'transfer_not_found'} else 403 if str(exc) == 'forbidden' else 503 if str(exc) == 'transfer_schema_not_ready' else 409 if str(exc) in {'revision_conflict', 'transfer_conflict', 'moment_transfer_pending'} else 400


def _identity(runtime, request: Request, *, write: bool = False, admission: bool = True) -> str:
    wall = get_public_wall()
    auth = request.headers.get('authorization', '')
    if auth:
        if request.cookies.get(_SESSION_COOKIE) or not auth.startswith('Bearer ') or len(auth) > 512:
            raise HTTPException(401, 'invalid_authentication')
        result = runtime.keys.authenticate_bearer_identity(auth[7:])
        if not result:
            raise HTTPException(401, 'invalid_authentication')
        key_id, visitor_id = result
    else:
        raw_session = request.cookies.get(_SESSION_COOKIE, '')
        csrf = request.headers.get('x-mirrow-csrf') if write else None
        if write and (not csrf or csrf != request.cookies.get(_CSRF_COOKIE)):
            raise HTTPException(403, 'csrf_required')
        result = wall.resolve_session(raw_session, csrf)
        if not result:
            raise HTTPException(401, 'login_required')
        visitor_id, key_id = result
    if visitor_id == '__owner__' and key_id == '__owner__' and not auth:
        return 'aning'
    with runtime.database.connection() as db:
        row = db.execute('SELECT 1 FROM visitor_keys k JOIN visitors v ON v.id=k.visitor_id '
                         "WHERE k.id=? AND k.visitor_id=? AND k.revoked_at IS NULL AND v.status IN ('active','suspended')",
                         (key_id, visitor_id)).fetchone()
    if not row:
        raise HTTPException(401, 'credential_revoked')
    try:
        from .household_identity import registered_social_visitor
        registered_social_visitor(runtime, visitor_id)
        if write and admission:
            from .profile_registration import ProfileRegistrationStore
            ProfileRegistrationStore(wall).guard('visitor:' + visitor_id)
    except PublicWallError as exc:
        raise HTTPException(403, str(exc)) from None
    wall.note_contact(visitor_id)
    return 'visitor:' + visitor_id


def _home_remark(runtime, actor: str) -> str:
    if not actor.startswith('visitor:'):
        return ''
    try:
        label = (runtime.visitors.visitor(actor[8:]).display_name or '').strip()
    except Exception:
        label = ''
    if label and label not in {'好友', '未认领访客', '等待好友认领'}:
        return label[:40]
    try:
        from lounge_visits import storage
        friend_id = get_public_wall().linked_friend(actor[8:])
        return storage.friends().get_owned('k', friend_id).display_name[:40] if friend_id else ''
    except (KeyError, ValueError):
        return ''


def _cognition_name(actor: str) -> str:
    if not actor.startswith('visitor:'):
        return ''
    from lounge_visits.cognition_profiles import get as cognition_profile
    from cognition.other_book import entities
    primary = cognition_profile(actor).get('primary_entity_id') or ''
    entity = entities().get(primary) if primary else None
    return str(entity.get('name') or '').strip()[:40] if isinstance(entity, dict) else ''


def _display(runtime, actor: str, *, viewer: str) -> dict:
    wall = get_public_wall()
    if actor.startswith('archive:'):
        label = wall.transfer_person(actor) or '历史参与者'
        return {'actor_id': actor, 'name': label, 'nickname': label,
                'avatar': '', 'remark': '', 'canonical_name': '', 'verified': False}
    saved = wall.profile(actor)
    from .profile_registration import ProfileRegistrationStore
    registration = ProfileRegistrationStore(wall).state(actor)
    home_viewer = viewer in {'aning', 'k'}
    nickname = saved['nickname'] or (wall.registered_name(actor[8:]) if home_viewer and actor.startswith('visitor:') else '') or (
        '站主' if actor == 'aning' else 'AI' if actor == 'k' else '未设置网名')
    cognition_name = _cognition_name(actor) if home_viewer else ''
    default = _home_remark(runtime, actor) if home_viewer else ''
    remark = '' if actor == viewer or (home_viewer and actor in {'aning', 'k'}) else (
        cognition_name or wall.remark(viewer, actor, home_default=default))
    label = nickname + (f'（{remark}）' if remark and remark != nickname else '')
    avatar = saved['avatar']
    if registration['state'] == 'pending':
        nickname = '资料待验证'
        avatar = ''
        label = nickname + (f'（{remark}）' if remark else '')
    result = {'actor_id': actor, 'name': label, 'nickname': nickname,
              'avatar': avatar, 'remark': remark,
              'canonical_name': '', 'verified': registration['state'] != 'pending'}
    result['profile_registration'] = registration
    result['can_edit_profile'] = registration['can_edit_profile'] and not saved.get('profile_identity', {}).get('linked', False)
    if saved.get('profile_identity'):
        result['profile_identity'] = saved['profile_identity']
    else:
        from .profile_identity import ProfileIdentityStore
        source=ProfileIdentityStore(wall).source_identity(actor,PUBLIC_SOCIAL_ORIGIN)
        if source:result['profile_identity']=source
    if home_viewer and actor.startswith('visitor:'):
        result['cognition_bound'] = bool(cognition_name)
        result['remark_locked'] = bool(cognition_name)
    return result


def _managed_profiles(runtime, viewer: str) -> list[str]:
    """A human manages its own wall identity and linked AIs; the local AI belongs to the local human."""
    if viewer == 'aning':
        return ['aning', 'k']
    if not viewer.startswith('visitor:'):
        return [viewer]
    visitor_id = viewer[8:]
    if runtime.visitor_service.effective_visitor(visitor_id).visitor_kind != 'human':
        return [viewer]
    wall = get_public_wall()
    linked = []
    for item in wall.household_links():
        if item['human_visitor_id'] != visitor_id:
            continue
        ai_id = item['ai_visitor_id']
        try:
            ai = runtime.visitor_service.effective_visitor(ai_id)
        except (KeyError, ValueError):
            continue
        if ai.visitor_kind == 'external_ai' and ai.status in {'active', 'suspended'}:
            linked.append('visitor:' + ai_id)
    return [viewer, *linked]


def _check_profile_target(runtime, viewer: str, subject: str, *, avatar: bool = False) -> None:
    if subject not in _managed_profiles(runtime, viewer):
        raise HTTPException(403, 'profile_not_managed')
    if avatar and viewer not in {'aning'} and runtime.visitor_service.effective_visitor(viewer[8:]).visitor_kind != 'human':
        raise HTTPException(403, 'avatar_managed_by_human')


def _decorate(runtime, item: dict | None, *, viewer: str) -> dict | None:
    if item is None:
        return None
    actors = {item['author']}
    actors.update(row['author'] for row in item.get('comments', []))
    actors.update(row['author'] for row in item.get('reactions', []))
    actors.update(item.get('mention_actor_ids', []))
    forward_source = (item.get('forward') or {}).get('source') or {}
    if forward_source.get('author'):
        actors.add(forward_source['author'])
    for comment in item.get('comments', []):
        actors.update(comment.get('mention_actor_ids', []))
    item['people'] = {actor: _display(runtime, actor, viewer=viewer) for actor in actors}
    item['can_manage'] = not item.get('migrated') and get_public_wall().can_manage_moment(viewer, item['author'])
    item['hosting_mode'] = 'hosted' if item['author'].startswith('visitor:') else 'home'
    item['can_moderate'] = not item.get('migrated') and viewer == 'aning' and item['hosting_mode'] == 'hosted'
    for comment in item.get('comments', []):
        comment['can_delete'] = not item.get('migrated') and get_public_wall().can_delete_comment(viewer, comment['author'])
    return item


def _receive_login_gift(runtime, actor: str) -> None:
    if not actor.startswith('visitor:'):
        return
    try:
        from .decor_store import get_decor_store
        visitor = runtime.visitor_service.effective_visitor(actor[8:])
        get_decor_store().visit(actor, 'human' if visitor.visitor_kind == 'human' else 'ai',
                               _display(runtime, actor, viewer='aning')['name'])
    except Exception as exc:
        # Authentication does not depend on decor; the next visit retries.
        logger.warning('social arrival gift pending: category=%s', type(exc).__name__)


def create_public_social_app(runtime) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    failed_login = TokenBucketLimiter(10, 10 / 60)
    requests = TokenBucketLimiter(120, 2)
    decor_uploads = TokenBucketLimiter(10, 10 / 60)

    def visitor_session(visitor_id: str, key_id: str, request: Request) -> JSONResponse:
        wall = get_public_wall()
        wall.note_contact(visitor_id)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        wall.issue_session(token, csrf, visitor_id, key_id, time.time() + 86400)
        actor_id = 'visitor:' + visitor_id
        # Arrival is durable even if a stale browser does not mount its decor UI.
        _receive_login_gift(runtime, actor_id)
        response = JSONResponse({'ok': True, 'actor': _display(runtime, actor_id, viewer=actor_id)})
        local = request.url.hostname in {'127.0.0.1', 'localhost'}
        response.set_cookie(_SESSION_COOKIE, token, max_age=86400, httponly=True, secure=not local, samesite='strict')
        response.set_cookie(_CSRF_COOKIE, csrf, max_age=86400, httponly=False, secure=not local, samesite='strict')
        return response

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        decor_upload = request.method in {'POST','PUT'} and (request.url.path.startswith(('/social/v1/decor/media/','/social/v1/decor/gif/')) or request.url.path=='/social/v1/decor/sync')
        decor_body = request.method in {'POST','PUT'} and request.url.path.startswith('/social/v1/decor/')
        profile_body = request.method in {'POST','PUT'} and request.url.path.startswith('/social/v1/profile-links/')
        avatar_upload = (request.url.path == '/social/v1/me/avatar' or
                         request.url.path.startswith('/social/v1/people/') and request.url.path.endswith('/avatar'))
        safe_route = 'avatar_upload' if avatar_upload else 'other'
        host_ok = _host_ok(request)
        origin_ok = _origin_ok(request)
        if not host_ok or not origin_ok:
            # Keep credentials, raw headers and image bytes out of logs.  The
            # short reason is enough to distinguish a bad Tunnel host from an
            # Origin mismatch when a friend reports request_rejected.
            logger.warning('public social request rejected: route=%s reason=%s',
                           safe_route, 'host' if not host_ok else 'origin')
            return JSONResponse({'detail': 'request_rejected'}, status_code=403)
        if any(sum(k.lower() == header for k, _ in request.scope['headers']) > 1
               for header in (b'host', b'origin', b'authorization', b'cookie')):
            logger.warning('public social request rejected: route=%s reason=duplicate_header', safe_route)
            return JSONResponse({'detail': 'request_rejected'}, status_code=400)
        if request.headers.get('content-length'):
            try:
                max_body = 28_100_000 if decor_upload else MAX_UPLOAD_BYTES if avatar_upload else 8192
                if request.url.path in {'/social/v1/decor/home','/social/v1/decor/draft'}:
                    max_body = 40_000
                if int(request.headers['content-length']) > max_body:
                    return JSONResponse({'detail': 'request_too_large'}, status_code=413)
            except ValueError:
                logger.warning('public social request rejected: route=%s reason=content_length', safe_route)
                return JSONResponse({'detail': 'request_rejected'}, status_code=400)
        if decor_upload:
            try:
                uploader=_identity(runtime,request,write=True)
            except HTTPException as exc:
                return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
            if not decor_uploads.allow(uploader):
                return JSONResponse({'detail':'rate_limited'},status_code=429)
        if decor_body or profile_body:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>(8192 if profile_body else 28_100_000 if decor_upload else 40_000):
                    return JSONResponse({'detail':'request_too_large'},status_code=413)
            request._body = bytes(body)
        peer = request.client.host if request.client else 'unknown'
        if not requests.allow(peer):
            return JSONResponse({'detail': 'rate_limited'}, status_code=429)
        if request.method == 'GET' and request.url.path in {'/social/v1/moments','/social/v1/me'}:
            try:
                _identity(runtime, request)
            except HTTPException:
                pass
            else:
                from .profile_transport import refresh_due
                await refresh_due(get_public_wall(),PUBLIC_SOCIAL_ORIGIN)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({'detail': 'invalid_request'}, status_code=422)

    @app.exception_handler(PublicWallError)
    async def wall_error(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=_status(error))

    @app.get('/')
    def page():
        return FileResponse(Path(__file__).with_name('public_wall.html'), media_type='text/html')

    @app.get('/wall.js')
    def script():
        return FileResponse(Path(__file__).with_name('public_wall.js'), media_type='text/javascript')

    @app.get('/forward-ui.js')
    def forward_script():
        return FileResponse(Path(__file__).with_name('forward_ui.js'), media_type='text/javascript', headers={'Cache-Control':'no-store'})

    @app.get('/wall-state.js')
    def wall_state_script():
        return FileResponse(Path(__file__).with_name('public_wall_state.js'), media_type='text/javascript',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/social/v1/avatars/{digest}.png')
    def uploaded_avatar(digest: str, request: Request):
        _identity(runtime, request)
        if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
            raise HTTPException(404, 'avatar_not_found')
        from social_feed.avatar_images import AVATAR_DIR
        target = AVATAR_DIR / (digest + '.png')
        if not target.is_file():
            raise HTTPException(404, 'avatar_not_found')
        return FileResponse(target, media_type='image/png')

    @app.post('/social/v1/login')
    def login(body: Login, request: Request):
        if not failed_login.allow(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, 'rate_limited')
        try:
            identity = runtime.keys.authenticate_identity(body.key)
        except RateLimitExceeded:
            raise HTTPException(429, 'rate_limited') from None
        if not identity:
            raise HTTPException(401, 'invalid_key')
        key_id, visitor_id = identity
        try:
            from .household_identity import registered_social_visitor
            registered_social_visitor(runtime, visitor_id)
        except PublicWallError as exc:
            raise HTTPException(403, str(exc)) from None
        return visitor_session(visitor_id, key_id, request)

    @app.post('/social/v1/household/register')
    def register_household(body: HouseholdRegistration, request: Request):
        """Live Keys prove every member; register one human with up to eight AIs atomically."""
        if not failed_login.allow(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, 'rate_limited')
        if body.ais is not None:
            if body.ai_key is not None or body.ai_name is not None:
                raise HTTPException(400, 'invalid_household_request')
            ai_entries = [(item.name, item.key) for item in body.ais]
        elif body.ai_key is not None and body.ai_name is not None:
            ai_entries = [(body.ai_name, body.ai_key)]
        else:
            raise HTTPException(400, 'invalid_household_request')
        if not body.human_name.strip() or any(not name.strip() for name, _ in ai_entries):
            raise HTTPException(400, 'names_required')
        try:
            human, ais = verified_household(runtime, body.human_key, [key for _, key in ai_entries])
        except RateLimitExceeded:
            raise HTTPException(429, 'rate_limited') from None
        except PublicWallError as exc:
            raise HTTPException(403, str(exc)) from None
        try:
            get_public_wall().link_households(human[1],
                [(identity[1], name) for identity, (name, _) in zip(ais, ai_entries)],
                human_name=body.human_name, profile_origin=PUBLIC_SOCIAL_ORIGIN,
                profile_sources={human[1]: body.human_source.model_dump() if body.human_source else None,
                    **{identity[1]: (source.model_dump() if source else None) for identity, source in zip(ais,
                        [item.profile_source for item in body.ais] if body.ais is not None else [body.ai_source])}})
        except PublicWallError as exc:
            raise HTTPException(_status(exc), str(exc)) from None
        return visitor_session(human[1], human[0], request)

    @app.post('/social/v1/human/register')
    def register_human(body: HumanRegistration, request: Request):
        """Human-first enrollment; an AI can be linked later by the owner or two-Key form."""
        if not failed_login.allow(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, 'rate_limited')
        if not body.human_name.strip():
            raise HTTPException(400, 'names_required')
        try:
            identity = runtime.keys.authenticate_identity(body.human_key)
        except RateLimitExceeded:
            raise HTTPException(429, 'rate_limited') from None
        if not identity:
            raise HTTPException(401, 'invalid_key')
        key_id, visitor_id = identity
        visitor = social_visitor(runtime, visitor_id, require_link=False)
        if visitor.visitor_kind != 'human':
            raise HTTPException(403, 'human_key_required')
        wall = get_public_wall()
        try:
            wall.register_human_name(visitor_id, body.human_name,
                profile_source=body.profile_source.model_dump() if body.profile_source else None,
                profile_origin=PUBLIC_SOCIAL_ORIGIN)
        except PublicWallError as exc:
            raise HTTPException(_status(exc), str(exc)) from None
        return visitor_session(visitor_id, key_id, request)

    @app.post('/social/v1/owner-login')
    def owner_login(body: OwnerTicket, request: Request):
        if not failed_login.allow(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, 'rate_limited')
        from social_feed.owner_access import consume_owner_ticket
        if not consume_owner_ticket(body.ticket):
            raise HTTPException(401, 'invalid_owner_ticket')
        wall = get_public_wall()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        wall.issue_session(token, csrf, '__owner__', '__owner__', time.time() + 86400)
        response = JSONResponse({'ok': True, 'actor': _display(runtime, 'aning', viewer='aning')})
        local = request.url.hostname in {'127.0.0.1', 'localhost'}
        response.set_cookie(_SESSION_COOKIE, token, max_age=86400, httponly=True, secure=not local, samesite='strict')
        response.set_cookie(_CSRF_COOKIE, csrf, max_age=86400, httponly=False, secure=not local, samesite='strict')
        return response

    @app.post('/social/v1/logout')
    def logout(request: Request):
        token = request.cookies.get(_SESSION_COOKIE, '')
        if token:
            get_public_wall().revoke_session(token)
        response = JSONResponse({'ok': True})
        response.delete_cookie(_SESSION_COOKIE)
        response.delete_cookie(_CSRF_COOKIE)
        return response

    @app.get('/social/v1/me')
    def me(request: Request):
        viewer = _identity(runtime, request)
        _receive_login_gift(runtime, viewer)
        human = viewer == 'aning' or (viewer.startswith('visitor:') and
                 runtime.visitor_service.effective_visitor(viewer[8:]).visitor_kind == 'human')
        capabilities = ['read', 'edit_own', 'comment', 'delete_own_comment', 'like', 'withdraw_own',
                        'changes', 'identity_card', 'private_remark', 'profile', 'set_nickname']
        capabilities.extend(['post','decor_v1','mentions_v1','inbox_v1','forwarding_v1'])
        from .profile_registration import ProfileRegistrationStore
        if not ProfileRegistrationStore(get_public_wall()).state(viewer)['can_interact']:
            capabilities = ['read', 'changes', 'identity_card', 'profile_link', 'decor_v1']
        return {'actor': _display(runtime, viewer, viewer=viewer), 'can_manage_avatar': human, 'capabilities':
                capabilities}

    @app.get('/social/v1/me/managed-profiles')
    def managed_profiles(request: Request):
        viewer = _identity(runtime, request)
        return {'profiles': [_display(runtime, subject, viewer=viewer)
                             for subject in _managed_profiles(runtime, viewer)]}

    @app.get('/social/v1/me/identities')
    def identities(request: Request):
        from .identity_switch import household_identities
        viewer = _identity(runtime, request)
        return {'identities': [_display(runtime, subject, viewer=viewer) for subject in
                              household_identities(runtime, get_public_wall(), viewer)]}

    @app.post('/social/v1/me/switch')
    def switch_identity(body: IdentitySwitch, request: Request):
        from .identity_switch import household_identities
        viewer = _identity(runtime, request, write=True, admission=False)
        wall = get_public_wall()
        if body.target not in household_identities(runtime, wall, viewer):
            raise HTTPException(403, 'identity_not_in_household')
        if not failed_login.allow(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, 'rate_limited')
        try:
            identity = runtime.keys.authenticate_identity(body.key)
        except RateLimitExceeded:
            raise HTTPException(429, 'rate_limited') from None
        if not identity or 'visitor:' + identity[1] != body.target:
            raise HTTPException(401, 'identity_key_mismatch')
        from .household_identity import registered_social_visitor
        try:
            registered_social_visitor(runtime, identity[1])
        except PublicWallError as exc:
            raise HTTPException(403, str(exc)) from None
        response = visitor_session(identity[1], identity[0], request)
        wall.revoke_session(request.cookies.get(_SESSION_COOKIE, ''))
        return response

    @app.put('/social/v1/me/profile')
    def profile(body: Profile, request: Request):
        actor = _identity(runtime, request, write=True)
        _check_profile_target(runtime, actor, actor, avatar=body.avatar is not None)
        get_public_wall().set_profile(actor, body.nickname, body.avatar)
        return {'actor': _display(runtime, actor, viewer=actor)}

    @app.put('/social/v1/people/{subject}/profile')
    def managed_profile(subject: str, body: Profile, request: Request):
        viewer = _identity(runtime, request, write=True)
        _check_profile_target(runtime, viewer, subject, avatar=body.avatar is not None)
        get_public_wall().set_profile(subject, body.nickname, body.avatar)
        return {'actor': _display(runtime, subject, viewer=viewer)}

    @app.get('/social/v1/people/{actor_id}')
    def person_card(actor_id: str, request: Request):
        viewer = _identity(runtime, request)
        wall = get_public_wall()
        if not ((actor_id.startswith('archive:') and wall.transfer_person(actor_id)) or wall.known_actor(actor_id)):
            raise HTTPException(404, 'person_not_found')
        return {'person': _display(runtime, actor_id, viewer=viewer)}

    @app.put('/social/v1/people/{actor_id}/remark')
    def person_remark(actor_id: str, body: Remark, request: Request):
        viewer = _identity(runtime, request, write=True)
        if viewer == 'aning' and _cognition_name(actor_id):
            raise HTTPException(403, 'remark_locked_by_cognition')
        get_public_wall().set_remark(viewer, actor_id, body.remark)
        return {'person': _display(runtime, actor_id, viewer=viewer)}

    async def _upload_avatar(request: Request, subject: str):
        viewer = _identity(runtime, request, write=True)
        _check_profile_target(runtime, viewer, subject, avatar=True)
        from .profile_identity import ProfileIdentityStore
        ProfileIdentityStore(get_public_wall()).guard_write(subject)
        if request.headers.get('content-type', '').split(';', 1)[0].lower() not in {'image/png', 'image/jpeg'}:
            raise HTTPException(415, 'image_type_required')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, 'request_too_large')
        try:
            target = save_avatar(subject, bytes(raw))
        except ValueError:
            raise HTTPException(415, 'invalid_avatar_image') from None
        url = PUBLIC_SOCIAL_ORIGIN + '/social/v1/avatars/' + target.name + f'?v={target.stat().st_mtime_ns}'
        wall = get_public_wall()
        wall.set_profile(subject, avatar=url)
        return {'actor': _display(runtime, subject, viewer=viewer)}

    @app.post('/social/v1/me/avatar')
    async def upload_avatar(request: Request):
        viewer = _identity(runtime, request, write=True)
        return await _upload_avatar(request, viewer)

    @app.post('/social/v1/people/{subject}/avatar')
    async def upload_managed_avatar(subject: str, request: Request):
        return await _upload_avatar(request, subject)

    @app.get('/social/v1/moments')
    def moments(request: Request, limit: int = 30, before_time: float | None = None, before_id: str = ''):
        viewer = _identity(runtime, request)
        if (before_time is None) != (not before_id):
            raise HTTPException(400, 'invalid_cursor')
        result = get_public_wall().list_moments(limit=limit, before=(before_time, before_id) if before_time is not None else None)
        result['items'] = [_decorate(runtime, item, viewer=viewer) for item in result['items']]
        return result

    @app.get('/social/v1/moments/search')
    def search_moments(request: Request, q: str = '', limit: int = 10):
        viewer = _identity(runtime, request)
        if not 1 <= len(q.strip()) <= 80:
            raise HTTPException(400, 'invalid_social_query')
        return {'items': [_decorate(runtime, item, viewer=viewer)
                          for item in get_public_wall().search_moments(q, limit=limit)]}

    @app.get('/social/v1/moments/{moment_id}')
    def read(moment_id: str, request: Request):
        viewer = _identity(runtime, request)
        item = get_public_wall().get_moment(moment_id)
        if item is None:
            raise HTTPException(404, 'moment_not_found')
        return _decorate(runtime, item, viewer=viewer)

    @app.get('/social/v1/moments/{moment_id}/status')
    def moment_status(moment_id: str, request: Request):
        return get_public_wall().removal_status(_identity(runtime, request), moment_id)

    @app.post('/social/v1/moments/{moment_id}/transfer/prepare')
    async def transfer_prepare(moment_id: str, body: TransferPrepare, request: Request):
        actor = _identity(runtime, request, write=True)
        try:
            await social_migration.public_destination(social_migration.validate_origin(body.destination_origin))
        except ValueError:
            raise HTTPException(400, 'transfer_origin_not_public') from None
        return social_migration.prepare(get_public_wall(), actor, moment_id,
                                        body.destination_origin, body.target_actor)

    @app.get('/social/v1/transfers/{transfer_id}/status')
    def transfer_status(transfer_id: str, request: Request):
        return social_migration.outbound_status(get_public_wall(), _identity(runtime, request), transfer_id)

    @app.post('/social/v1/transfers/{transfer_id}/cancel')
    def transfer_cancel(transfer_id: str, request: Request):
        return social_migration.cancel(get_public_wall(), _identity(runtime, request, write=True), transfer_id)

    @app.post('/social/v1/transfers/{transfer_id}/commit')
    async def transfer_commit(transfer_id: str, body: TransferCommit, request: Request):
        actor = _identity(runtime, request, write=True)
        status = social_migration.outbound_status(get_public_wall(), actor, transfer_id)
        if status['state'] == 'committed':
            return social_migration.commit(get_public_wall(), actor, transfer_id, body.destination_moment_id)
        await social_migration.verify_remote_stage(status['destination_origin'], transfer_id, body.proof,
                                                    status['digest'], body.destination_moment_id)
        return social_migration.commit(get_public_wall(), actor, transfer_id, body.destination_moment_id)

    @app.post('/social/v1/transfers/{transfer_id}/proof')
    def transfer_proof(transfer_id: str, body: TransferProof):
        # No visitor Key is sent to the new site. The random one-time proof is
        # readable only by the local owner process staging this exact transfer.
        return social_migration.prove(get_public_wall(), transfer_id, body.proof,
                                      body.digest, body.destination_moment_id)

    @app.post('/social/v1/moments')
    def create(body: Post, request: Request):
        viewer = _identity(runtime, request, write=True)
        source = f'social-http:{viewer}:post:{body.request_id}' if body.request_id else None
        result = get_public_wall().create_moment(viewer, body.content, source_key=source,
                                                  mention_actor_ids=body.mention_actor_ids, forward_ref=body.forward_ref)
        return result if result.get('status') == 'withdrawn' else _decorate(runtime, result, viewer=viewer)

    @app.patch('/social/v1/moments/{moment_id}')
    def edit(moment_id: str, body: Edit, request: Request):
        viewer = _identity(runtime, request, write=True)
        return _decorate(runtime, get_public_wall().edit_moment(viewer, moment_id, body.content, body.revision), viewer=viewer)

    @app.delete('/social/v1/moments/{moment_id}')
    def withdraw(moment_id: str, request: Request):
        viewer = _identity(runtime, request, write=True)
        return get_public_wall().withdraw(viewer, moment_id, owner_override=viewer == 'aning')

    @app.post('/social/v1/moments/{moment_id}/comments')
    def comment(moment_id: str, body: Comment, request: Request):
        viewer = _identity(runtime, request, write=True)
        source = f'social-http:{viewer}:comment:{body.request_id}' if body.request_id else None
        return get_public_wall().add_comment(viewer, moment_id, body.content, body.reply_to_id, source_key=source,
                                             mention_actor_ids=body.mention_actor_ids)

    @app.get('/social/v1/mention-people')
    def mention_people(request: Request, q: str = '', limit: int = 30):
        from .mentions import candidates
        viewer = _identity(runtime, request)
        return candidates(runtime, get_public_wall(), viewer, query=q, limit=limit)

    @app.get('/social/v1/notifications')
    def mention_notices(request: Request, limit: int = 30):
        viewer = _identity(runtime, request)
        rows = get_public_wall().notifications(viewer, limit=max(1,min(30,limit)))
        for row in rows:
            row['actor_name'] = _display(runtime, row['actor'], viewer=viewer)['name']
        return {'items': rows}

    @app.post('/social/v1/notifications/read')
    def read_mentions(body: MentionRead, request: Request):
        viewer = _identity(runtime, request, write=True)
        return {'marked': get_public_wall().mark_notifications_read(viewer, body.notification_ids, 'public_ui')}

    @app.delete('/social/v1/moments/{moment_id}/comments/{comment_id}')
    def delete_comment(moment_id: str, comment_id: str, request: Request):
        return get_public_wall().delete_comment(_identity(runtime, request, write=True), moment_id, comment_id)

    @app.post('/social/v1/moments/{moment_id}/like')
    def like(moment_id: str, request: Request):
        return get_public_wall().toggle_like(_identity(runtime, request, write=True), moment_id)

    @app.put('/social/v1/moments/{moment_id}/like')
    def set_like(moment_id: str, body: LikeChoice, request: Request):
        return get_public_wall().set_like(_identity(runtime, request, write=True), moment_id, body.liked)

    @app.get('/social/v1/changes')
    def changes(request: Request, after: int = 0, limit: int = 100):
        viewer = _identity(runtime, request)
        result = get_public_wall().changes(after, limit)
        for row in result['items']:
            row['moment'] = _decorate(runtime, row['moment'], viewer=viewer)
        return result

    from .decor_router import create_decor_router, install_errors
    app.include_router(create_decor_router(runtime))
    from .profile_router import create_profile_router
    app.include_router(create_profile_router(runtime))
    install_errors(app)
    return app
