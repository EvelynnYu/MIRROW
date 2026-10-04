"""One authenticated source and one destination; no copied source body."""
from __future__ import annotations

from urllib.parse import quote
from .remote_client import RemoteSocialClient
from .remote_sites import get_remote_site_store
from .forwarding import normalize_forward_ref, ForwardError


async def forward_moment(*, actor, moment_id, source_site_id='', site_id='',
                         content='', visibility='private', source_key=None,
                         local_store=None, sites=None, client=None):
    if actor not in {'aning', 'k'} or visibility not in {'private', 'public'}:
        raise ForwardError('invalid_forward_request')
    from . import get_social_feed_store
    from .public_gateway import PUBLIC_SOCIAL_ORIGIN
    local = local_store or get_social_feed_store()
    registry = sites or get_remote_site_store()
    transport = client or RemoteSocialClient()
    kind = 'human' if actor == 'aning' else 'ai'
    source = registry.get(source_site_id) if source_site_id else None
    destination = registry.get(site_id) if site_id else None
    if (source_site_id and not source) or (site_id and not destination):
        raise ForwardError('social_site_not_found')
    for home in (source, destination):
        if home and (not home.enabled or not (home.human_key if actor == 'aning' else home.ai_key)):
            raise ForwardError('social_site_identity_unavailable')
    if source:
        item = await transport.request(source, kind, 'GET', '/social/v1/moments/' + quote(moment_id, safe=''))
    else:
        item = await local.get_moment(moment_id)
    if (not item or item.get('id') != moment_id or item.get('visibility') != 'public'
            or item.get('migrated') or item.get('forward')):
        raise ForwardError('forward_source_unavailable')
    same_home = (source_site_id or '') == (site_id or '')
    ref = normalize_forward_ref({'origin': '' if same_home else source.origin if source else PUBLIC_SOCIAL_ORIGIN,
                                 'moment_id': moment_id})
    comment = str(content or '').strip() or '转发了一条动态'
    if destination:
        if visibility != 'public':
            raise ForwardError('remote_social_public_only')
        me = await transport.request(destination, kind, 'GET', '/social/v1/me')
        if ('forwarding_v1' not in (me.get('capabilities') or [])
                or me.get('can_manage_avatar') is not (actor == 'aning')):
            raise ForwardError('remote_social_forwarding_unavailable')
        result = await transport.request(destination, kind, 'POST', '/social/v1/moments',
            payload={'content': comment, 'forward_ref': ref, **({'request_id': source_key} if source_key else {})})
    else:
        result = await local.create_moment(actor, comment, visibility=visibility,
                                          forward_ref=ref, source_key=source_key)
    return {**result, 'forward_source_ref': ref}
