"""Same profile custody UI/API for the website and local owner. No auth bypass."""
from pathlib import Path
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from .public_wall import get_public_wall, PublicWallError
from .profile_identity import ProfileIdentityStore
from .profile_registration import ProfileRegistrationStore
from .public_gateway import ProfileSource
from . import profile_transport


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class GrantInput(Strict):
    audience: str=Field(min_length=12,max_length=300)


class CodeInput(Strict):
    code: str=Field(min_length=60,max_length=1500)


class FamilyCodeInput(Strict):
    code: str=Field(min_length=60,max_length=7000)


class FamilyBinding(Strict):
    index: int=Field(ge=0,le=8)
    subject: str=Field(pattern=r'^(aning|k|visitor:[A-Za-z0-9_-]{1,92})$')


class FamilyLinkInput(FamilyCodeInput):
    bindings: list[FamilyBinding]=Field(min_length=1,max_length=9)


class ClaimInput(Strict):
    ticket: str=Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    audience: str=Field(min_length=12,max_length=300)
    consumer: str=Field(pattern=r'^(aning|k|visitor:[A-Za-z0-9_-]{1,92})$')


class ReadInput(Strict):
    token: str=Field(pattern=r'^[A-Za-z0-9_-]{43}$')


class PolicyInput(Strict):
    enabled: bool


ui_router=APIRouter(prefix='/api/social-profile-ui')


@ui_router.get('/script')
def local_script():
    return FileResponse(Path(__file__).with_name('profile_link_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})


@ui_router.get('/style')
def local_style():
    return FileResponse(Path(__file__).with_name('profile_link_ui.css'),media_type='text/css',headers={'Cache-Control':'no-store'})


def create_profile_router(runtime=None, *, local=False):
    from .public_gateway import _identity, _managed_profiles, _check_profile_target, _display, PUBLIC_SOCIAL_ORIGIN
    dependencies=[]
    if local:
        from routers.lounge_reception_router import local_ui
        dependencies=[Depends(local_ui)]
    router=APIRouter(prefix='/api/social-profiles' if local else '/social/v1/profile-links',dependencies=dependencies)

    def current():
        if runtime is not None:
            return runtime
        from lounge_reception.runtime import current_runtime
        value=current_runtime()
        if value is None:
            raise HTTPException(503,'profile_runtime_unavailable')
        return value

    def viewer(request,write=False):
        # Admission transitions must remain available to a pending identity; CSRF still applies.
        return 'aning' if local else _identity(current(),request,write=write,admission=False)

    def human_subject(request,subject):
        actor=viewer(request,True)
        _check_profile_target(current(),actor,subject,avatar=True)
        return actor

    def kind(actor):
        return 'human' if actor=='aning' or actor.startswith('visitor:') and current().visitor_service.effective_visitor(actor[8:]).visitor_kind=='human' else 'ai'

    def valid_grant(row):
        # A separate read token cannot outlive the credential that authorized it.
        for actor in dict.fromkeys((row['actor'],row['issuer'])):
            if actor.startswith('visitor:'):
                from .household_identity import registered_social_visitor
                try:
                    registered_social_visitor(current(),actor[8:])
                    from .household_identity import has_active_key
                    visitor=current().visitor_service.effective_visitor(actor[8:])
                    if visitor.status!='active' or not has_active_key(current(),actor[8:]):
                        raise PublicWallError('profile_access_revoked')
                    if visitor.visitor_kind=='external_ai':
                        human=get_public_wall().household_human(actor[8:])
                        if not human or current().visitor_service.effective_visitor(human).status!='active':
                            raise PublicWallError('profile_access_revoked')
                except (PublicWallError,KeyError,ValueError):
                    raise PublicWallError('profile_access_revoked') from None
        if kind(row['actor']) != row['kind']:
            raise PublicWallError('profile_access_revoked')
        if row['issuer'].startswith('visitor:'):
            with current().database.connection() as db:
                if not db.execute('SELECT 1 FROM visitor_keys WHERE id=? AND visitor_id=? AND revoked_at IS NULL',
                                  (row['key_id'],row['issuer'][8:])).fetchone():
                    raise PublicWallError('profile_access_revoked')

    def store():
        return ProfileIdentityStore(get_public_wall())

    @router.get('/ui.js')
    def script():
        return FileResponse(Path(__file__).with_name('profile_link_ui.js'),media_type='text/javascript',headers={'Cache-Control':'no-store'})

    @router.get('/ui.css')
    def style():
        return FileResponse(Path(__file__).with_name('profile_link_ui.css'),media_type='text/css',headers={'Cache-Control':'no-store'})

    @router.get('/state')
    def state(request: Request):
        actor=viewer(request)
        human=kind(actor)=='human'
        return {'ready':store().ready(),'hosting_enabled':store().enabled(),'owner':actor=='aning','human':human,
                'registration_ready':ProfileRegistrationStore(get_public_wall()).ready(),
                'profiles':[{'actor':a,'name':get_public_wall().registered_name(a[8:]) if a.startswith('visitor:') else _display(current(),a,viewer=actor)['name'],'link':store().status(a),
                             'registration':ProfileRegistrationStore(get_public_wall()).state(a),
                             'kind':kind(a),
                             'grants':store().grants(a) if human else []} for a in _managed_profiles(current(),actor)]}

    @router.put('/people/{subject}/source')
    def source(subject: str,body: ProfileSource,request: Request):
        human_subject(request,subject)
        return ProfileRegistrationStore(get_public_wall()).select(subject,body.model_dump(),PUBLIC_SOCIAL_ORIGIN)

    @router.put('/policy')
    def policy(body: PolicyInput,request: Request):
        if viewer(request,True)!='aning':
            raise HTTPException(403,'profile_owner_required')
        store().set_enabled(body.enabled)
        return {'enabled':store().enabled()}

    @router.post('/people/{subject}/grant')
    def issue(subject: str,body: GrantInput,request: Request):
        from .remote_sites import normalize_social_origin
        actor=human_subject(request,subject)
        audience=normalize_social_origin(body.audience)
        if audience==PUBLIC_SOCIAL_ORIGIN:
            raise PublicWallError('profile_link_to_self')
        key_id='__owner__'
        if actor.startswith('visitor:'):
            auth=request.headers.get('authorization','')
            if auth:
                key_id=current().keys.authenticate_bearer_identity(auth[7:])[0]
            else:
                key_id=get_public_wall().resolve_session(request.cookies.get('mirrow_wall_session',''))[1]
        store().require_ready()
        valid_grant({'actor':subject,'kind':kind(subject),'issuer':actor,'key_id':key_id})
        store().snapshot(subject,kind(subject),PUBLIC_SOCIAL_ORIGIN)
        return store().make_grant(subject,kind(subject),actor,key_id,audience,PUBLIC_SOCIAL_ORIGIN)

    @router.post('/family/grant')
    def family_grant(body: GrantInput,request: Request):
        from .remote_sites import normalize_social_origin
        from .profile_family import issue_family
        actor=viewer(request,True)
        if kind(actor)!='human':raise HTTPException(403,'avatar_managed_by_human')
        audience=normalize_social_origin(body.audience)
        if audience==PUBLIC_SOCIAL_ORIGIN:raise PublicWallError('profile_link_to_self')
        key_id='__owner__'
        if actor.startswith('visitor:'):
            auth=request.headers.get('authorization','')
            key_id=current().keys.authenticate_bearer_identity(auth[7:])[0] if auth else get_public_wall().resolve_session(request.cookies.get('mirrow_wall_session',''))[1]
        subjects=[]
        for subject in _managed_profiles(current(),actor):
            status=ProfileRegistrationStore(get_public_wall()).state(subject)
            if status['state'] in {'pending','linked'}:continue
            valid_grant({'actor':subject,'kind':kind(subject),'issuer':actor,'key_id':key_id})
            subjects.append((subject,kind(subject)))
        return issue_family(get_public_wall(),subjects,actor,key_id,audience,PUBLIC_SOCIAL_ORIGIN)

    @router.post('/family/preview')
    def family_preview(body: FamilyCodeInput,request: Request):
        from .profile_family import decode_family
        actor=viewer(request,True)
        if kind(actor)!='human':raise HTTPException(403,'avatar_managed_by_human')
        origin,members=decode_family(body.code)
        # No token is consumed, and the displayed code labels are not yet verified.
        return {'origin':origin,'members':[{k:v for k,v in m.items() if k!='ticket'} for m in members]}

    @router.post('/family/link')
    async def family_link(body: FamilyLinkInput,request: Request):
        from .profile_family import accept_family
        actor=viewer(request,True)
        if kind(actor)!='human':raise HTTPException(403,'avatar_managed_by_human')
        def validate():
            for binding in body.bindings:human_subject(request,binding.subject)
        validate()
        bindings=[{'index':b.index,'subject':b.subject,'kind':kind(b.subject)} for b in body.bindings]
        return await accept_family(get_public_wall(),body.code,bindings,PUBLIC_SOCIAL_ORIGIN,validate=validate)

    @router.post('/people/{subject}/link')
    async def link(subject: str,body: CodeInput,request: Request):
        human_subject(request,subject)
        return await profile_transport.accept(get_public_wall(),subject,kind(subject),body.code,PUBLIC_SOCIAL_ORIGIN)

    @router.post('/people/{subject}/refresh')
    async def refresh(subject: str,request: Request):
        human_subject(request,subject)
        return await profile_transport.refresh(get_public_wall(),subject,PUBLIC_SOCIAL_ORIGIN)

    @router.delete('/people/{subject}/link')
    def detach(subject: str,request: Request):
        human_subject(request,subject)
        store().detach(subject)
        return {'linked':False}

    @router.delete('/people/{subject}/grants/{identifier}')
    def revoke(subject: str,identifier: str,request: Request):
        human_subject(request,subject)
        store().revoke(subject,identifier)
        return {'revoked':True}

    if not local:
        from .profile_key_router import create_key_profile_router
        router.include_router(create_key_profile_router(current,kind,PUBLIC_SOCIAL_ORIGIN))

        @router.post('/claim')
        def claim(body: ClaimInput):
            # The short-lived code itself proves possession. It grants only this
            # public profile, never a wall session or permission to post.
            row=store().claim(body.ticket,body.audience,body.consumer,validate=valid_grant)
            from .profile_identity import home_grant
            return {'token':row['token'],'audience':row['audience'],'consumer':body.consumer,
                    'home_profile':home_grant(row),
                    'profile':store().snapshot(row['actor'],row['kind'],PUBLIC_SOCIAL_ORIGIN)}

        @router.post('/read')
        def read(body: ReadInput):
            row=store().authorization(body.token)
            valid_grant(row)
            return {'audience':row['audience'],'consumer':row['consumer'],
                    'profile':store().snapshot(row['actor'],row['kind'],PUBLIC_SOCIAL_ORIGIN)}

    @router.get('/avatars/{identifier}')
    def avatar(identifier: str,request: Request):
        viewer(request)
        if not re.fullmatch(r'[0-9a-f]{64}\.png',identifier):
            raise HTTPException(404,'avatar_not_found')
        path=get_public_wall().path.parent/'social_profile_avatars'/identifier
        if not path.is_file():
            raise HTTPException(404,'avatar_not_found')
        return FileResponse(path,media_type='image/png',headers={'X-Content-Type-Options':'nosniff'})

    return router


def install_profile_errors(app):
    from fastapi.responses import JSONResponse
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    previous=app.exception_handlers.get(RequestValidationError,request_validation_exception_handler)
    @app.exception_handler(RequestValidationError)
    async def validation(request,exc):
        if request.url.path.startswith('/api/social-profiles/'):
            return JSONResponse({'detail':'invalid_request'},status_code=422)
        return await previous(request,exc)
    @app.exception_handler(PublicWallError)
    async def error(_request,exc):
        from .public_gateway import _status
        return JSONResponse({'detail':str(exc)},status_code=_status(exc))
