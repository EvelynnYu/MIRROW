"""One household code, explicit per-machine mapping, atomic local association.

Underlying grants remain per identity and audience, so revocation stays narrow.
"""
import base64
import json

from .public_wall import PublicWallError
from .profile_identity import ProfileIdentityStore, decode_code, PROFILE, TOKEN
from .profile_registration import guard_origin_in
from .profile_transport import ProfileTransport
from .remote_sites import normalize_social_origin


def decode_family(code):
    try:
        if not isinstance(code, str) or not 60 <= len(code) <= 7000:
            raise ValueError()
        value = json.loads(base64.b64decode(code, altchars=b'-_', validate=True))
        if set(value) != {'v', 'origin', 'members'} or value['v'] != 2:
            raise ValueError()
        origin = normalize_social_origin(value['origin'])
        members = value['members']
        if not isinstance(members, list) or not 1 <= len(members) <= 9:
            raise ValueError()
        ids, tickets = set(), set()
        for member in members:
            if (not isinstance(member, dict) or set(member) != {'kind','nickname','profile_id','ticket'}
                    or member['kind'] not in {'human','ai'} or not isinstance(member['nickname'],str)
                    or not 1 <= len(member['nickname']) <= 40 or any(ord(c)<32 for c in member['nickname'])
                    or not isinstance(member['profile_id'],str) or not PROFILE.fullmatch(member['profile_id'])
                    or not isinstance(member['ticket'],str) or not TOKEN.fullmatch(member['ticket'])
                    or member['profile_id'] in ids or member['ticket'] in tickets):
                raise ValueError()
            ids.add(member['profile_id']);tickets.add(member['ticket'])
        if sum(m['kind']=='human' for m in members)>1:
            raise ValueError()
        return origin, members
    except (ValueError, TypeError, KeyError):
        raise PublicWallError('profile_ticket_invalid') from None


def issue_family(wall, subjects, issuer, key_id, audience, origin):
    if not 1 <= len(subjects) <= 9:
        raise PublicWallError('profile_family_limit')
    store = ProfileIdentityStore(wall)
    profiles = [(actor,kind,store.snapshot(actor,kind,origin)) for actor,kind in subjects]
    members = []
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for actor, kind, snap in profiles:
            grant = store.make_grant(actor,kind,issuer,key_id,audience,origin,transaction=db)
            _, ticket = decode_code(grant['code'])
            members.append({'kind':kind,'nickname':snap['nickname'],'profile_id':snap['profile_id'],'ticket':ticket})
    code = base64.urlsafe_b64encode(json.dumps({'v':2,'origin':origin,'members':members},
        ensure_ascii=False,separators=(',',':')).encode()).decode()
    return {'code':code,'expires_at':grant['expires_at'],'count':len(members)}


async def accept_family(wall, code, bindings, home_origin, *, client=None, validate=None):
    origin, members = decode_family(code)
    if origin == home_origin:
        raise PublicWallError('profile_link_to_self')
    if (not 1 <= len(bindings) <= len(members) or len({b['index'] for b in bindings}) != len(bindings)
            or len({b['subject'] for b in bindings}) != len(bindings)):
        raise PublicWallError('profile_mapping_invalid')
    store = ProfileIdentityStore(wall)
    store.require_ready()
    for binding in bindings:
        index = binding['index']
        if type(index) is not int or not 0 <= index < len(members):
            raise PublicWallError('profile_mapping_invalid')
        if binding['kind'] != members[index]['kind']:
            raise PublicWallError('profile_mapping_invalid')
        if store.link(binding['subject']):
            raise PublicWallError('profile_link_conflict')
        with wall._connect() as db:
            guard_origin_in(db,binding['subject'],origin)
    payloads = []
    for binding in bindings:
        member = members[binding['index']]
        result = await (client or ProfileTransport()).request(origin,'claim',
            {'ticket':member['ticket'],'audience':home_origin,'consumer':binding['subject']})
        profile = result.get('profile') or {}
        if (result.get('audience')!=home_origin or result.get('consumer')!=binding['subject']
                or profile.get('profile_id')!=member['profile_id'] or profile.get('kind')!=member['kind']):
            raise PublicWallError('profile_response_invalid')
        payloads.append((binding,result))
    if validate:
        validate()  # Recheck household management after waiting for remote claims.
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for binding,result in payloads:
            # No silent replacement, including a concurrent association during I/O.
            if db.execute('SELECT 1 FROM profile_identity_links WHERE actor=?',(binding['subject'],)).fetchone():
                raise PublicWallError('profile_link_conflict')
            store.cache(binding['subject'],origin,binding['kind'],result.get('token',''),
                        result['profile'],public_origin=home_origin,transaction=db)
    return {'linked':len(bindings),'profiles':[store.status(b['subject']) for b in bindings]}
