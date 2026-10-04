"""Same domain API for the authenticated website and local MIRROW editor."""
from __future__ import annotations
import base64
import hashlib
import json
from typing import Annotated, Literal
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from .decor_models import StrictModel, HomeDesign, HomeEdit, PersonalDesign, GiftChoice, SyncChoice, DraftRequest
from .decor_store import DecorError, get_decor_store
from .decor_media import MAX_UPLOAD, save_media
from .remote_sites import get_remote_site_store, normalize_social_origin
from . import decor_federation as federation


ui_router = APIRouter(prefix='/api/social-decor-ui')

@ui_router.get('/script')
def local_script():
    return FileResponse(Path(__file__).with_name('decor_ui.js'),media_type='text/javascript', headers={'Cache-Control':'no-store'})

@ui_router.get('/style')
def local_style():
    return FileResponse(Path(__file__).with_name('decor_ui.css'),media_type='text/css', headers={'Cache-Control':'no-store'})


@ui_router.get('/studio')
def local_studio():
    return FileResponse(Path(__file__).with_name('decor_studio_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})


@ui_router.get('/music-editor')
def local_music_editor():
    return FileResponse(Path(__file__).with_name('decor_music_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})


def bundled_font(identifier):
    from .decor_presets import font_path
    try:
        path = font_path(identifier)
    except DecorError:
        raise HTTPException(404, 'font_not_found') from None
    # These two licensed fonts are public assets, not credentialed user media.
    return FileResponse(path,media_type='font/ttf',headers={'Cache-Control':'public, max-age=86400',
        'Access-Control-Allow-Origin':'*','X-Content-Type-Options':'nosniff'})


@ui_router.get('/fonts/{identifier}')
def local_font(identifier: str):
    return bundled_font(identifier)


class LinkInput(StrictModel):
    origin: str = Field(max_length=300)
    subject: str = Field(pattern=r'^(aning|k)$')
    token: str = Field(min_length=32,max_length=80)


class SyncInput(LinkInput):
    design: PersonalDesign
    frame_data: str = Field(default='',max_length=14_000_000)
    card_data: str = Field(default='',max_length=14_000_000)


class ReceiptInput(StrictModel):
    id: str = Field(min_length=1,max_length=80)


class HumanNoticeInput(StrictModel):
    receipt_ids: list[str] = Field(min_length=1, max_length=50)


class GiftUpdatesInput(StrictModel):
    gift_ids: list[Annotated[str,Field(min_length=1,max_length=80)]] = Field(min_length=1,max_length=50)


class CollectionRefreshInput(StrictModel):
    subject: str = Field(default='',max_length=100)


class PresetInput(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,60}$')


class MusicImportInput(StrictModel):
    source: str = Field(min_length=1, max_length=2048)
    sharing_rights_confirmed: Literal[True]


def create_decor_router(runtime=None, *, local=False):
    from .public_gateway import _identity, _display, _managed_profiles, _check_profile_target, PUBLIC_SOCIAL_ORIGIN
    from routers.lounge_reception_router import local_ui
    router = APIRouter(prefix='/api/social-decor' if local else '/social/v1/decor',
                       dependencies=[Depends(local_ui)] if local else [])

    def current_runtime():
        if runtime is not None:
            return runtime
        from lounge_reception.runtime import current_runtime as get_runtime
        result = get_runtime()
        if result is None:
            raise HTTPException(503,'接待服务尚未就绪')
        return result

    def identity(request, write=False, *, admission=True):
        if local: return 'aning'
        if not admission: return _identity(current_runtime(),request,write=write,admission=False)
        return _identity(current_runtime(),request,write=write)

    def owner(request):
        if identity(request,True) != 'aning':
            raise HTTPException(403,'只有本家主人可以布置整站')

    def human_target(request, subject):
        actor = identity(request,True)
        _check_profile_target(current_runtime(),actor,subject,avatar=True)
        from .public_wall import get_public_wall
        from .profile_registration import guard_in
        with get_public_wall()._connect() as db:
            guard_in(db, actor)
            guard_in(db, subject)
        return actor

    def human_machine_subjects(request, write=False, *, admission=True):
        """A human can inspect only its own valid household machines."""
        actor = identity(request, write,admission=admission)
        if actor == 'aning':
            return actor, ['k']
        if not actor.startswith('visitor:'):
            return actor, []
        try:
            if current_runtime().visitor_service.effective_visitor(actor[8:]).visitor_kind != 'human':
                return actor, []
        except (KeyError, ValueError):
            return actor, []
        machines = []
        for subject in _managed_profiles(current_runtime(), actor):
            if subject == actor or not subject.startswith('visitor:'):
                continue
            try:
                if current_runtime().visitor_service.effective_visitor(subject[8:]).visitor_kind != 'human':
                    machines.append(subject)
            except (KeyError, ValueError):
                continue
        return actor, machines

    @router.get('/music-editor.js')
    def music_editor_script():
        return FileResponse(Path(__file__).with_name('decor_music_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})

    @router.get('/ui.js')
    def script():
        return FileResponse(Path(__file__).with_name('decor_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})

    @router.get('/ui.css')
    def stylesheet():
        return FileResponse(Path(__file__).with_name('decor_ui.css'),media_type='text/css',headers={'Cache-Control':'no-store'})

    @router.get('/studio.js')
    def studio_script():
        return FileResponse(Path(__file__).with_name('decor_studio_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})

    @router.get('/fonts/{identifier}')
    def public_font(identifier: str):
        return bundled_font(identifier)

    @router.get('/state')
    def state(request: Request):
        actor = identity(request)
        store = get_decor_store()
        managed = _managed_profiles(current_runtime(),actor)
        human = actor=='aning' or actor.startswith('visitor:') and current_runtime().visitor_service.effective_visitor(actor[8:]).visitor_kind=='human'
        from .public_wall import get_public_wall
        from .profile_registration import state_in
        with get_public_wall()._connect() as db:
            editable = {subject: human and state_in(db, actor)['can_interact'] and state_in(db, subject)['can_interact']
                        for subject in managed}
        return {'home':store.home(),'actor':actor,'owner':actor=='aning','human':human,
                'profiles':[{'actor':subject,'name':_display(current_runtime(),subject,viewer=actor)['name'],
                             'can_edit_decor':editable[subject],
                             'design':store.personal(subject),'link':store.link(subject),
                             'sync':store.choice(subject) if actor=='aning' else None} for subject in managed],
                'gifts':store.gifts(),'sites':[site.public_dict() for site in get_remote_site_store().list()] if actor=='aning' else []}

    @router.get('/music-choices')
    def music_choices(request: Request):
        owner(request)
        from .decor_music import music_choices
        return {'songs':music_choices()}

    # Account-backed fetch is an explicit local-owner action, not a capability
    # exposed to visiting humans/AI or the public tunnel.
    if local:
        @router.post('/music-import')
        async def music_import(body: MusicImportInput, request: Request):
            owner(request)
            from .decor_music_import import import_music
            return await import_music(get_decor_store(), body.source)

    @router.get('/people')
    def people(request: Request, actors: str=''):
        identity(request)
        from .public_wall import get_public_wall
        ids = list(dict.fromkeys(actors.split(',')))[:50]
        return {'people':{a:get_decor_store().personal(a) for a in ids if a and get_public_wall().known_actor(a)}}

    @router.put('/home')
    def save_home(body: HomeEdit, request: Request):
        owner(request)
        return get_decor_store().save_home(HomeDesign.model_validate(body.model_dump(exclude={'sync_gift_exhibits'})),
                                          sync_gift_exhibits=body.sync_gift_exhibits)

    @router.get('/presets')
    def presets(request: Request):
        identity(request)
        from .decor_presets import catalog
        return catalog()

    @router.post('/presets/{subject}')
    def select_preset(subject: str, body: PresetInput, request: Request):
        human_target(request,subject)
        from .decor_presets import import_preset
        return import_preset(get_decor_store(),subject,body.id)

    @router.post('/gif/{subject}')
    async def gif(subject: str, request: Request):
        human_target(request,subject)
        # Bounded JSON avoids unbounded multipart spooling and never accepts paths.
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>14_000_000:
                raise DecorError('media_too_large')
        from .decor_gif_api import decode_request, generate
        frames, settings = decode_request(bytes(raw))
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(generate,frames,settings)
        from .decor_media import persist_media
        saved = await run_in_threadpool(persist_media,get_decor_store(),subject,result.data,'gif','image/gif')
        return saved | {'mode':result.mode}

    @router.put('/people/{subject}')
    async def save_person(body: PersonalDesign, subject: str, request: Request):
        actor = human_target(request,subject)
        store = get_decor_store()
        design = store.save_personal(subject,body)
        results = await federation.sync_personal(store,subject,PUBLIC_SOCIAL_ORIGIN) if actor=='aning' else []
        return {'design':design,'sync_results':results}

    @router.put('/people/{subject}/sync')
    async def sync(body: SyncChoice, subject: str, request: Request):
        owner(request)
        if subject not in {'aning','k'}:
            raise HTTPException(403,'只能同步本家身份')
        store = get_decor_store()
        previous = store.choice(subject)
        store.choice(subject,body)
        return {'results':await federation.sync_personal(store,subject,PUBLIC_SOCIAL_ORIGIN,previous=previous)}

    @router.post('/sites/{site_id}/link/{subject}')
    async def link_site(site_id: str, subject: str, request: Request):
        owner(request)
        if subject not in {'aning','k'}:
            raise HTTPException(403,'只能关联本家身份')
        return await federation.link_site(get_decor_store(),get_remote_site_store().get(site_id),subject,PUBLIC_SOCIAL_ORIGIN)

    @router.post('/media/{subject}')
    async def upload(subject: str, request: Request, kind: str='image'):
        human_target(request,subject)
        if kind not in {'image','audio'} or kind=='audio' and identity(request)!='aning':
            raise HTTPException(403,'只有本家主人可上传背景音乐')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_UPLOAD:
                raise DecorError('media_too_large')
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(save_media,get_decor_store(),subject,bytes(raw),kind=='audio')

    @router.get('/media/{identifier}')
    def media(identifier: str, request: Request):
        identity(request)
        store = get_decor_store()
        asset = store.asset(identifier)
        return FileResponse(store.media_dir/asset['id'],media_type=asset['mime'],headers={'X-Content-Type-Options':'nosniff'})

    @router.post('/gifts')
    def gift(body: GiftChoice, request: Request):
        owner(request)
        store = get_decor_store()
        store.set_gift(body.kind,body.exhibit_id,body.request_id)
        return {'gifts':store.gifts()}

    @router.post('/visit')
    async def visit(request: Request):
        actor = identity(request,True,admission=False)
        human = actor=='aning' or actor.startswith('visitor:') and current_runtime().visitor_service.effective_visitor(actor[8:]).visitor_kind=='human'
        name = _display(current_runtime(),actor,viewer='aning')['name']
        gifts = get_decor_store().visit(actor,'human' if human else 'ai',name)
        from .decor_events import publish_pending
        try:
            await publish_pending()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('gift notification pending: category=%s',type(exc).__name__)
        return {'gifts':gifts}

    @router.get('/collection')
    def collection(request: Request, before: float | None=None, subject: str=''):
        actor = identity(request)
        if subject:
            if subject not in _managed_profiles(current_runtime(),actor):
                raise HTTPException(403,'只能查看自己和所管理身份的收藏')
            actor = subject
        return {'items':get_decor_store().collection(actor,before=before)}

    @router.post('/gift-updates')
    def gift_updates(body: GiftUpdatesInput, request: Request):
        from .decor_gift_updates import for_actor
        return {'items':for_actor(get_decor_store(), identity(request),body.gift_ids)}

    @router.post('/collection/refresh')
    async def refresh_collection(body: CollectionRefreshInput, request: Request):
        actor = identity(request,True,admission=False)
        subject = body.subject or actor
        if subject not in _managed_profiles(current_runtime(),actor):
            raise HTTPException(403,'只能查看自己和所管理身份的收藏')
        results = []
        if local:
            store = get_decor_store()
            with store.connect() as db:
                origins = {r[0] for r in db.execute('SELECT DISTINCT origin FROM collections WHERE actor=? AND origin!=\'\'',(subject,))}
            for site in get_remote_site_store().list():
                if site.origin in origins:
                    try:
                        results.append({'name':site.name,'updated':await federation.refresh_remote_gifts(site,subject,store),'status':'success'})
                    except (ValueError,OSError):
                        results.append({'name':site.name,'status':'pending'})
        return {'items':get_decor_store().collection(subject),'results':results}

    @router.post('/seen')
    def seen(body: ReceiptInput, request: Request):
        get_decor_store().seen(identity(request,True,admission=False),body.id)
        return {'ok':True}

    @router.get('/human-gift-notices')
    def human_gift_notices(request: Request, site_id: str=''):
        human, machines = human_machine_subjects(request)
        origin = None
        if site_id:
            # This path is used only by the local site switcher: it binds the
            # current remote page to that exact registered home's imported receipt.
            if not local:
                return {'items': []}
            try:
                origin = get_remote_site_store().get(site_id).origin
            except ValueError:
                raise HTTPException(404, 'social_site_not_found') from None
        rows = get_decor_store().human_gift_notices(human, machines, origin=origin)
        return {'items': [row | {'machine_name': _display(current_runtime(), row['actor'], viewer=human)['name']}
                          for row in rows]}

    @router.post('/human-gift-notices/seen')
    def see_human_gift_notices(body: HumanNoticeInput, request: Request):
        human, machines = human_machine_subjects(request, True,admission=False)
        return {'ok': True, 'marked': get_decor_store().see_human_gift_notices(human, body.receipt_ids, machines)}

    @router.get('/deliveries')
    def deliveries(request: Request, before_time: float | None=None, before_id: str=''):
        owner(request)
        report = get_decor_store().delivery_report(before_time=before_time,before_id=before_id)
        items = []
        for row in report['items']:
            try:
                name = _display(current_runtime(),row['actor'],viewer='aning')['name']
            except (KeyError,ValueError):
                name = '已移除的访客'
            items.append({'id':row['id'],'name':name,'kind':row['kind'],'received':row['received'],
                          'gift_name':row['snapshot']['name']})
        return {**report,'items':items}

    @router.post('/draft')
    async def draft(body: DraftRequest, request: Request):
        owner(request)
        from .decor_events import draft_exhibit
        return await draft_exhibit(body)

    @router.get('/proofs/{token}')
    def proof(token: str):
        # A five-minute random challenge contains no Key or personal history.
        return get_decor_store().proof(token)

    @router.post('/link')
    async def accept_link(body: LinkInput, request: Request):
        actor = identity(request,True)
        if not actor.startswith('visitor:'):
            raise HTTPException(400,'只能关联来访身份')
        human = current_runtime().visitor_service.effective_visitor(actor[8:]).visitor_kind=='human'
        if (body.subject=='aning') != human:
            raise DecorError('link_identity_mismatch')
        origin = normalize_social_origin(body.origin)
        if origin==PUBLIC_SOCIAL_ORIGIN:
            raise DecorError('link_identity_mismatch')
        proof = await federation.read_proof(origin,body.token)
        if proof != {'origin':origin,'subject':body.subject,'audience':PUBLIC_SOCIAL_ORIGIN,'actor':actor}:
            raise DecorError('invalid_link_proof')
        get_decor_store().bind(actor,origin,body.subject)
        return {'ok':True,'origin':origin,'subject':body.subject}

    @router.delete('/link/{subject}')
    def unlink(subject: str, request: Request):
        human_target(request,subject)
        get_decor_store().unlink(subject)
        return {'ok':True}

    @router.put('/sync')
    async def receive_sync(body: SyncInput, request: Request):
        actor = identity(request,True)
        store = get_decor_store()
        origin = normalize_social_origin(body.origin)
        if store.link(actor) != {'origin':origin,'subject':body.subject}:
            raise DecorError('home_link_required')
        payload = body.model_dump(exclude={'token'})
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        proof = dict(await federation.read_proof(origin,body.token))
        version=proof.pop('version',None)
        if type(version) is not int or not 0<version<2**63 or proof != {'operation':'decor_sync','actor':actor,'audience':PUBLIC_SOCIAL_ORIGIN,'digest':digest}:
            raise DecorError('invalid_link_proof')
        data = body.design.model_dump()
        for field,encoded in [('frame_asset',body.frame_data),('card_asset',body.card_data)]:
            if encoded:
                try:
                    raw = base64.b64decode(encoded,validate=True)
                except ValueError:
                    raise DecorError('invalid_decor_image') from None
                data[field] = save_media(store,actor,raw)['id']
            elif data[field]:
                raise DecorError('invalid_decor_image')
        store.save_synced(actor,origin,version,PersonalDesign.model_validate(data))
        return {'ok':True}

    return router


def install_errors(app):
    @app.exception_handler(DecorError)
    async def error(_request,exc):
        return JSONResponse({'detail':str(exc)},status_code=409 if str(exc)=='design_conflict' else 400)
