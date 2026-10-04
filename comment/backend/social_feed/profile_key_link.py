"""Deployed households link public profiles to the actor authenticated by a Key.

No names, supplied subject IDs, cookies or Key strings become identity authority.
The existing profile grant/read protocol still owns verification and refresh.
"""
import time

from .profile_identity import ProfileIdentityStore, decode_code, digest
from .profile_registration import TABLE, ready
from .profile_transport import ProfileTransport
from .public_wall import PublicWallError
from .remote_sites import RemoteSiteError


def check_target(db, actor, origin, profile_id):
    if not actor.startswith('visitor:'):
        raise PublicWallError('profile_key_identity_required')
    if not ready(db):
        raise PublicWallError('profile_registration_schema_not_ready')
    prior = db.execute('SELECT origin,profile_id FROM profile_identity_links WHERE actor=?', (actor,)).fetchone()
    if prior and (prior['origin'] != origin or prior['profile_id'] != profile_id):
        raise PublicWallError('profile_link_conflict')
    source = db.execute(f'SELECT choice,origin FROM {TABLE} WHERE actor=?', (actor,)).fetchone()
    if source and source['choice'] == 'existing' and source['origin'] != origin:
        raise PublicWallError('profile_source_locked')
    if db.execute('SELECT 1 FROM profile_identity_grants WHERE actor=? AND revoked IS NULL AND expires>?',
                  (actor, time.time())).fetchone():
        raise PublicWallError('profile_has_outgoing_grants')


async def accept_key_profiles(wall, bindings, home_origin, *, client=None, validate=None):
    store = ProfileIdentityStore(wall)
    store.require_ready()
    prepared = []
    for binding in bindings:
        origin, ticket = decode_code(binding['code'])
        if origin == home_origin:
            raise PublicWallError('profile_link_to_self')
        with wall._connect() as db:
            check_target(db, binding['actor'], origin, binding['profile_id'])
        prepared.append((binding, origin, ticket))
    if len({origin for _, origin, _ in prepared}) != 1:
        raise PublicWallError('profile_response_invalid')
    payloads = []
    for binding, origin, ticket in prepared:
        result = await (client or ProfileTransport()).request(origin, 'claim', {
            'ticket': ticket, 'audience': home_origin, 'consumer': binding['actor']})
        snapshot = result.get('profile') or {}
        if (result.get('audience') != home_origin or result.get('consumer') != binding['actor']
                or result.get('home_profile') is not True or snapshot.get('profile_id') != binding['profile_id']
                or snapshot.get('kind') != binding['kind']):
            raise PublicWallError('profile_response_invalid')
        payloads.append((binding, origin, result))
    # Authentication and household ownership may have changed during the callback.
    if validate:
        validate()
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        results = []
        for binding, origin, result in payloads:
            actor = binding['actor']
            check_target(db, actor, origin, binding['profile_id'])
            # Explicit Key binding upgrades a hosted profile. Source and verified
            # snapshots commit together for the whole pair, never on failure.
            db.execute(f'INSERT INTO {TABLE}(actor,choice,origin,created) VALUES(?,?,?,?) '
                       'ON CONFLICT(actor) DO UPDATE SET choice=excluded.choice,origin=excluded.origin',
                       (actor, 'existing', origin, time.time()))
            results.append(store.cache(actor, origin, binding['kind'], result.get('token', ''), result['profile'],
                                       public_origin=home_origin, transaction=db))
        return {'profiles': results}


async def sync_home_profiles(wall, site, home_origin, *, client):
    """Each configured Key maps exactly one source role; failure is never connected."""
    store = ProfileIdentityStore(wall)
    roles = [('human', 'aning')] + ([('ai', 'k')] if site.ai_key else [])
    results = {role: {'status': 'not_configured'} for role in ('human', 'ai')}
    if not site.human_key:
        if site.ai_key: results['ai'] = {'status': 'pending', 'reason': 'profile_human_key_required'}
        return results
    codes = []
    try:
        remote_roles = {}
        snapshots = {}
        for role, subject in roles:
            remote = await client.request(site, role, 'GET', '/social/v1/profile-links/key-profile')
            if remote.get('kind') != role or not str(remote.get('actor_id', '')).startswith('visitor:'):
                raise PublicWallError('profile_key_kind_mismatch')
            remote_roles[role] = remote
            snapshots[role] = store.snapshot(subject, role, home_origin)
            link = remote.get('link') or {}
            if link.get('linked') and link.get('identity_id') != home_origin + '#' + snapshots[role]['profile_id']:
                raise PublicWallError('profile_link_conflict')
        if site.ai_key and remote_roles['ai'].get('human_actor_id') != remote_roles['human']['actor_id']:
            raise PublicWallError('profile_household_mismatch')
        with wall._connect() as db:
            active_claims = all(db.execute(
                "SELECT 1 FROM profile_identity_grants WHERE actor=? AND kind=? AND audience=? AND consumer=? "
                "AND issuer='aning' AND key_id='__owner__' AND revoked IS NULL AND expires>?",
                (subject, role, site.origin, remote_roles[role]['actor_id'], time.time())).fetchone()
                for role, subject in roles)
        if active_claims and all((remote_roles[role].get('link') or {}).get('status') == 'verified'
               and (remote_roles[role].get('link') or {}).get('version', 0) >= snapshots[role]['version']
               for role, _ in roles):
            return {**results, **{role: {'status': 'linked'} for role, _ in roles}}
        members = []
        for role, subject in roles:
            code = store.make_grant(subject, role, 'aning', '__owner__', site.origin, home_origin)['code']
            codes.append((subject, code))
            members.append({'code': code, 'kind': role, 'profile_id': snapshots[role]['profile_id']})
        value = await client.request(site, 'human', 'POST', '/social/v1/profile-links/key-profile',
                                    payload={'members': members, 'ai_key': site.ai_key or ''})
        profiles = value.get('profiles')
        if not isinstance(profiles, list) or len(profiles) != len(roles):
            raise PublicWallError('profile_response_invalid')
        for (role, _), profile in zip(roles, profiles):
            if (profile.get('identity_id') != home_origin + '#' + snapshots[role]['profile_id']
                    or profile.get('status') != 'verified' or profile.get('linked') is not True):
                raise PublicWallError('profile_response_invalid')
        with wall._connect() as db:
            # A remote JSON receipt alone cannot prove it actually called back.
            # Verify local consumption before announcing success or retiring any
            # previous read authorization.
            for (role, subject), (_, code) in zip(roles, codes):
                _, ticket = decode_code(code)
                row = db.execute('SELECT * FROM profile_identity_grants WHERE ticket_hash=?', (digest(ticket),)).fetchone()
                if (not row or row['actor'] != subject or row['kind'] != role or row['audience'] != site.origin
                        or row['consumer'] != remote_roles[role]['actor_id'] or row['revoked'] is not None
                        or row['expires'] <= time.time() or row['issuer'] != 'aning' or row['key_id'] != '__owner__'):
                    raise PublicWallError('profile_response_invalid')
            for (role, subject), (_, code) in zip(roles, codes):
                _, ticket = decode_code(code)
                db.execute("UPDATE profile_identity_grants SET revoked=? WHERE actor=? AND audience=? "
                           "AND consumer=? AND issuer='aning' AND key_id='__owner__' AND ticket_hash!=? AND revoked IS NULL",
                           (time.time(), subject, site.origin, remote_roles[role]['actor_id'], digest(ticket)))
        results.update({role: {'status': 'linked'} for role, _ in roles})
    except (PublicWallError, RemoteSiteError, ValueError, OSError, RuntimeError) as exc:
        allowed = {'profile_key_kind_mismatch', 'profile_household_mismatch', 'profile_link_conflict', 'profile_source_locked',
                   'profile_has_outgoing_grants', 'profile_public_nickname_required',
                   'profile_uploaded_avatar_required', 'profile_schema_not_ready',
                   'profile_registration_schema_not_ready', 'social_site_key_rejected',
                   'social_site_identity_unavailable', 'profile_grant_limit'}
        reason = str(exc) if str(exc) in allowed else 'profile_sync_unavailable'
        results.update({role: {'status': 'pending', 'reason': reason} for role, _ in roles})
        for subject, code in codes:
            # Preserve a consumed grant if a successful remote commit lost its reply.
            _, ticket = decode_code(code)
            with wall._connect() as db:
                db.execute("UPDATE profile_identity_grants SET revoked=? WHERE actor=? AND ticket_hash=? AND consumer=''",
                           (time.time(), subject, digest(ticket)))
    return results
