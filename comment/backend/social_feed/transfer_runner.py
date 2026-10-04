"""Owner-initiated, idempotent cross-home hosted-post transfer orchestration."""

from __future__ import annotations

import json
from urllib.parse import quote

from . import migration
from .public_wall import PublicWallError
from .remote_client import RemoteSocialClient
from .remote_sites import RemoteSite, RemoteSiteError


async def move_hosted_post(wall, site: RemoteSite, actor_kind: str, moment_id: str,
                           destination_origin: str, client: RemoteSocialClient | None = None) -> dict:
    if actor_kind not in {'human', 'ai'} or not moment_id.startswith('sw_') or len(moment_id) > 100:
        raise PublicWallError('invalid_transfer_request')
    origin = migration.validate_origin(site.origin)
    destination_origin = migration.validate_origin(destination_origin)
    if origin == destination_origin:
        raise PublicWallError('transfer_same_home')
    target_actor = 'aning' if actor_kind == 'human' else 'k'
    client = client or RemoteSocialClient()
    existing = migration.inbound_record(wall, origin, moment_id)
    if existing and existing['target_actor'] != target_actor:
        raise PublicWallError('transfer_conflict')
    path = '/social/v1/moments/' + quote(moment_id, safe='')
    if existing:
        transfer_id = existing['id']
        remote_status = await client.request(site, actor_kind, 'GET',
                                             '/social/v1/transfers/' + transfer_id + '/status')
        if remote_status.get('state') == 'committed':
            if remote_status.get('destination_origin') != destination_origin:
                raise PublicWallError('transfer_source_invalid')
            result = migration.activate(wall, transfer_id, remote_status)
            return {'state': 'complete', 'moment': result}
        if remote_status.get('state') != 'prepared':
            raise PublicWallError('transfer_conflict')
        snapshot = json.loads(existing['snapshot_json'])
        prepared = {'transfer_id': transfer_id, 'source_moment_id': moment_id,
                    'digest': existing['digest'], 'snapshot': snapshot}
    else:
        identity = await client.request(site, actor_kind, 'GET', '/social/v1/me')
        source_actor = (identity.get('actor') or {}).get('actor_id')
        if (not isinstance(source_actor, str) or not source_actor.startswith('visitor:')
                or identity.get('can_manage_avatar') is not (actor_kind == 'human')):
            raise PublicWallError('transfer_identity_unavailable')
        prepared = await client.request(site, actor_kind, 'POST', path + '/transfer/prepare',
                                        payload={'destination_origin': destination_origin,
                                                 'target_actor': target_actor})
        if (prepared.get('source_moment_id') != moment_id
                or prepared.get('destination_origin') != destination_origin
                or prepared.get('target_actor') != target_actor
                or not isinstance(prepared.get('snapshot'), dict)
                or prepared['snapshot'].get('moment', {}).get('author') != source_actor):
            raise PublicWallError('transfer_source_invalid')
    transfer_id = prepared['transfer_id']
    staged = migration.stage(wall, transfer_id=transfer_id, source_origin=origin,
                             source_actor=prepared['snapshot']['moment']['author'],
                             target_actor=target_actor, snapshot=prepared['snapshot'],
                             expected_digest=prepared['digest'])
    if staged['state'] == 'active':
        return {'state': 'complete', 'moment': {'id': staged['destination_moment_id'], 'state': 'active'}}
    try:
        remote_status = await client.request(site, actor_kind, 'POST',
                                             '/social/v1/transfers/' + transfer_id + '/commit',
                                             payload={'destination_moment_id': staged['destination_moment_id'],
                                                      'proof': staged['proof']})
    except RemoteSiteError:
        # The HTTP response may have been lost after the old home committed.
        # Ask for durable status before concluding that the transfer is pending.
        try:
            remote_status = await client.request(site, actor_kind, 'GET',
                                                 '/social/v1/transfers/' + transfer_id + '/status')
        except RemoteSiteError:
            return {'state': 'pending', 'transfer_id': transfer_id,
                    'message': '旧站确认尚未返回；原帖已冻结，稍后重试同一条迁移。'}
        if remote_status.get('state') != 'committed':
            return {'state': 'pending', 'transfer_id': transfer_id,
                    'message': '旧站尚未提交迁移；原帖已冻结，稍后重试同一条迁移。'}
    if remote_status.get('destination_origin') != destination_origin:
        raise PublicWallError('transfer_source_invalid')
    result = migration.activate(wall, transfer_id, remote_status)
    return {'state': 'complete', 'moment': result}
