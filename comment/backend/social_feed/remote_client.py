"""Bounded, DNS-pinned HTTP client for another family's public social API."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from lounge_visits.network_guard import pin_request, public_destination
from .remote_sites import RemoteSite, RemoteSiteError


class RemoteSocialClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def media(self, site: RemoteSite, actor: str, identifier: str, *, avatar: bool=False, profile_avatar: bool=False) -> tuple[bytes,str]:
        import re
        if not re.fullmatch(r'[0-9a-f]{64}\.(png|gif|mp3)',identifier) or actor not in {'human','ai'}:
            raise RemoteSiteError('invalid_social_request')
        if ((avatar or profile_avatar) and not identifier.endswith('.png')) or (profile_avatar and not avatar):
            raise RemoteSiteError('invalid_social_request')
        key = site.human_key if actor=='human' else site.ai_key
        if not site.enabled or not key:
            raise RemoteSiteError('social_site_identity_unavailable')
        path='/social/v1/profile-links/avatars/' if profile_avatar else '/social/v1/avatars/' if avatar else '/social/v1/decor/media/'
        url = site.origin+path+identifier
        address = await public_destination(url)
        async def pin(request):
            pin_request(request,urlsplit(site.origin),address)
        async with httpx.AsyncClient(timeout=20,transport=self.transport,follow_redirects=False,trust_env=False,event_hooks={'request':[pin]}) as client:
            async with client.stream('GET',url,headers={'Authorization':f'Bearer {key}'}) as response:
                if response.status_code!=200:
                    raise RemoteSiteError('social_media_unavailable')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>10*1024*1024:
                        raise RemoteSiteError('social_response_too_large')
                mime = {'png':'image/png','gif':'image/gif','mp3':'audio/mpeg'}[identifier.rsplit('.',1)[1]]
                return bytes(raw),mime

    async def upload_media(self, site: RemoteSite, subject: str, raw: bytes):
        import re
        if not re.fullmatch(r'visitor:[A-Za-z0-9_-]{1,100}',subject) or len(raw)>10*1024*1024 or not site.enabled or not site.human_key:
            raise RemoteSiteError('invalid_social_request')
        url=site.origin+'/social/v1/decor/media/'+subject
        address=await public_destination(url)
        async def pin(request):
            pin_request(request,urlsplit(site.origin),address)
        async with httpx.AsyncClient(timeout=30,transport=self.transport,follow_redirects=False,trust_env=False,event_hooks={'request':[pin]}) as client:
            async with client.stream('POST',url,content=raw,headers={'Authorization':f'Bearer {site.human_key}','Content-Type':'application/octet-stream'}) as response:
                body=bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>4096:
                        raise RemoteSiteError('social_response_too_large')
                if response.status_code!=200:
                    raise RemoteSiteError('social_media_rejected')
        result=json.loads(body)
        if not isinstance(result,dict):
            raise RemoteSiteError('social_response_invalid')
        return result

    async def request(self, site: RemoteSite, actor: str, method: str, path: str,
                      *, payload: dict | None = None, params: dict | None = None) -> dict:
        if actor not in {'human', 'ai'} or method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            raise RemoteSiteError('invalid_social_request')
        if (not path.startswith('/social/v1/') or '//' in path or '..' in path
                or '?' in path or '#' in path or len(path) > 280):
            raise RemoteSiteError('invalid_social_request')
        key = site.human_key if actor == 'human' else site.ai_key
        if not key or not site.enabled:
            raise RemoteSiteError('social_site_identity_unavailable')
        url = site.origin + path
        expected = urlsplit(site.origin)
        try:
            address = await public_destination(url)
        except ValueError:
            raise RemoteSiteError('social_origin_not_public') from None

        async def pin(request: httpx.Request) -> None:
            pin_request(request, expected, address)

        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport,
                    follow_redirects=False, trust_env=False,
                    event_hooks={'request': [pin]}) as client:
                async with client.stream(method, url, json=payload, params=params,
                        headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'}) as response:
                    if response.status_code in {401, 403}:
                        raise RemoteSiteError('social_site_key_rejected')
                    if not 200 <= response.status_code < 300:
                        raise RemoteSiteError('social_site_unavailable')
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 1_000_000:
                            raise RemoteSiteError('social_response_too_large')
        except RemoteSiteError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise RemoteSiteError('social_site_unavailable') from None
        try:
            result = json.loads(body)
        except (TypeError, ValueError):
            raise RemoteSiteError('social_response_invalid') from None
        if not isinstance(result, dict):
            raise RemoteSiteError('social_response_invalid')
        return result
