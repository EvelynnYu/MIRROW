"""Public Key association surface: authenticated guardian, no selectable actors."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .profile_identity import ProfileIdentityStore
from .profile_key_link import accept_key_profiles
from .public_wall import get_public_wall


class KeyMember(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=60, max_length=1500)
    kind: Literal['human', 'ai']
    profile_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class KeyLinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    members: list[KeyMember] = Field(min_length=1, max_length=2)
    ai_key: str = Field(default='', max_length=256)


def create_key_profile_router(current, kind, home_origin):
    router = APIRouter()

    def authenticate(request):
        from .public_gateway import _identity
        if not request.headers.get('authorization', '').startswith('Bearer '):
            raise HTTPException(401, 'profile_key_identity_required')
        return _identity(current(), request, write=True, admission=False)

    @router.get('/key-profile')
    def key_state(request: Request):
        actor = authenticate(request)
        role = kind(actor)
        human = get_public_wall().household_human(actor[8:]) if role == 'ai' else None
        return {'actor_id': actor, 'kind': role,
                'human_actor_id': 'visitor:' + human if human else '',
                'link': ProfileIdentityStore(get_public_wall()).status(actor)}

    @router.post('/key-profile')
    async def key_link(body: KeyLinkInput, request: Request):
        def resolve():
            human = authenticate(request)
            if kind(human) != 'human':
                raise HTTPException(403, 'profile_human_key_required')
            roles = [member.kind for member in body.members]
            if roles != (['human', 'ai'] if body.ai_key else ['human']):
                raise HTTPException(400, 'profile_mapping_invalid')
            actors = [human]
            if body.ai_key:
                # The second Key goes through exactly the same admission checks,
                # not a caller-supplied visitor ID or a loose list of household names.
                scope = dict(request.scope)
                scope['headers'] = [(k, v) for k, v in request.scope['headers']
                                    if k.lower() not in {b'authorization', b'cookie'}]
                scope['headers'].append((b'authorization', ('Bearer ' + body.ai_key).encode('utf-8')))
                ai = authenticate(Request(scope))
                if kind(ai) != 'ai' or get_public_wall().household_human(ai[8:]) != human[8:]:
                    raise HTTPException(403, 'profile_household_mismatch')
                actors.append(ai)
            return actors
        actors = resolve()
        def validate():
            if resolve() != actors:
                raise HTTPException(403, 'profile_household_mismatch')
        bindings = [{**member.model_dump(), 'actor': actor} for member, actor in zip(body.members, actors)]
        return await accept_key_profiles(get_public_wall(), bindings, home_origin, validate=validate)

    return router
