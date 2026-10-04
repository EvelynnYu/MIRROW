"""The bounded AI visit interface for one explicitly registered remote wall.

Only the selected site's Key is used.  Reads and writes never fall back to a
different family or to MIRROW's private feed when a remote site is offline.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from .remote_client import RemoteSocialClient
from .remote_sites import RemoteSite


class RemoteSocialVisitStore:
    def __init__(self, site: RemoteSite, client: RemoteSocialClient | None = None):
        self.site = site
        self.client = client or RemoteSocialClient()
        self.actor_id = ''
        self.gifts = []
        self.gift_status = 'not_available'
        self.has_decor = False
        self.capabilities: set[str] = set()

    async def _request(self, method: str, path: str, *, payload=None, params=None) -> dict:
        return await self.client.request(self.site, 'ai', method, path,
                                         payload=payload, params=params)

    async def me(self) -> str:
        if not self.actor_id:
            info = await self._request('GET', '/social/v1/me')
            self.actor_id = str((info.get('actor') or {}).get('actor_id') or '')
            if not self.actor_id or info.get('can_manage_avatar') is not False:
                raise ValueError('remote_social_identity_invalid')
            self.capabilities = {str(value) for value in info.get('capabilities', [])}
            if 'decor_v1' in self.capabilities:
                self.has_decor = True
                from .decor_federation import receive_remote_gifts
                from .decor_store import get_decor_store
                import httpx
                try:
                    self.gifts = await receive_remote_gifts(self.site,'k',get_decor_store(),self.client)
                    self.gift_status = 'received' if self.gifts else 'no_new_gift'
                except (ValueError, OSError, httpx.HTTPError):
                    # The remote receipt remains unacknowledged and can be retried
                    # on the next visit. A gift failure must not invent a receipt.
                    self.gift_status = 'delivery_pending'
        return self.actor_id

    async def shelf_facts(self):
        if not self.has_decor:
            return {'status':'not_supported'}
        from .decor_context import shelf_facts
        return shelf_facts(await self._request('GET','/social/v1/decor/state'))

    async def list_moments(self, *, limit: int = 10, cursor: str | None = None) -> dict:
        params = {'limit': max(1, min(limit, 10))}
        if cursor:
            before = json.loads(cursor)
            if not isinstance(before, list) or len(before) != 2:
                raise ValueError('remote_social_cursor_invalid')
            params.update(before_time=float(before[0]), before_id=str(before[1]))
        page = await self._request('GET', '/social/v1/moments', params=params)
        return {**page, 'next_cursor': json.dumps(page['next_cursor']) if page.get('next_cursor') else ''}

    async def get_moment(self, moment_id: str) -> dict | None:
        return await self._request('GET', '/social/v1/moments/' + quote(moment_id, safe=''))

    async def search_moments(self, query: str, *, limit: int = 10) -> list[dict]:
        result = await self._request('GET', '/social/v1/moments/search',
                                     params={'q': query[:80], 'limit': max(1, min(limit, 10))})
        return list(result.get('items') or [])

    async def create_moment(self, actor: str, content: str, *, visibility: str,
                            source_key: str, mention_actor_ids: list[str] | None = None,
                            forward_ref=None, **_kwargs) -> dict:
        if actor != 'k' or visibility != 'public':
            raise ValueError('remote_social_public_only')
        mentions = self._mentions(mention_actor_ids)
        if forward_ref is not None and 'forwarding_v1' not in self.capabilities:
            raise ValueError('remote_social_forwarding_unavailable')
        return await self._request('POST', '/social/v1/moments',
                                   payload={'content': content, 'request_id': source_key,
                                            **({'mention_actor_ids': mentions} if mentions else {}),
                                            **({'forward_ref': forward_ref} if forward_ref is not None else {})})

    async def add_comment(self, moment_id: str, actor: str, content: str,
                          *, reply_to_id: str | None, source_key: str,
                          mention_actor_ids: list[str] | None = None) -> dict:
        if actor != 'k':
            raise ValueError('remote_social_actor_invalid')
        mentions = self._mentions(mention_actor_ids)
        return await self._request('POST', '/social/v1/moments/' + quote(moment_id, safe='') + '/comments',
                                   payload={'content': content, 'reply_to_id': reply_to_id,
                                            'request_id': source_key,
                                            **({'mention_actor_ids': mentions} if mentions else {})})

    def _mentions(self, values: list[str] | None) -> list[str]:
        from .mentions import normalise
        mentions = normalise(values)
        if mentions and 'mentions_v1' not in self.capabilities:
            # Do not silently drop a requested cross-home notification.
            raise ValueError('remote_social_mentions_unavailable')
        return mentions

    @property
    def has_inbox(self) -> bool:
        return 'inbox_v1' in self.capabilities

    async def unread_notifications(self, recipient: str, *, limit: int | None = 50) -> list[dict]:
        if recipient != 'k' or not self.has_inbox:
            raise ValueError('remote_social_inbox_unavailable')
        result = await self._request('GET', '/social/v1/notifications',
                                     params={'limit': max(1, min(int(limit or 50), 50))})
        return list(result.get('items') or [])

    async def mark_notifications_read(self, recipient: str, notification_ids=None,
                                      *, read_source: str = '', read_source_id: str = '') -> dict:
        if recipient != 'k' or not self.has_inbox:
            raise ValueError('remote_social_inbox_unavailable')
        ids = list(dict.fromkeys(str(value) for value in (notification_ids or []) if str(value)))[:100]
        if not ids:
            return {'marked': 0}
        return await self._request('POST', '/social/v1/notifications/read',
                                   payload={'notification_ids': ids})

    async def ensure_like(self, moment_id: str, actor: str, *, source_key: str) -> dict:
        if actor != 'k':
            raise ValueError('remote_social_actor_invalid')
        old = await self.get_moment(moment_id)
        identity = await self.me()
        existed = any(row.get('author') == identity for row in (old or {}).get('reactions') or [])
        result = await self._request('PUT', '/social/v1/moments/' + quote(moment_id, safe='') + '/like',
                                     payload={'liked': True})
        return {**result, 'inserted': not existed, 'existing': existed}

    async def set_nickname(self, nickname: str) -> dict:
        return await self._request('PUT', '/social/v1/me/profile', payload={'nickname': nickname})
