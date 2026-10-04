"""Private outbound registry for other families' social walls.

This registry is deliberately separate from lounge friends: a social hostname
cannot be derived from a lounge MCP URL, and a human's Key is never AI's Key.
"""

from __future__ import annotations

import ipaddress
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from lounge_visits.secret_vault import PREFIX, protect, unprotect


class RemoteSiteError(ValueError):
    pass


def normalize_social_origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise RemoteSiteError('invalid_social_origin')
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise RemoteSiteError('invalid_social_origin') from None
    if (parsed.scheme.lower() != 'https' or not host or port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip('/') not in {'', '/comment'}):
        raise RemoteSiteError('invalid_social_origin')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise RemoteSiteError('social_origin_not_public')
    try:
        hostname = host.encode('idna').decode('ascii').casefold()
    except UnicodeError:
        raise RemoteSiteError('invalid_social_origin') from None
    if hostname == 'localhost' or hostname.endswith('.localhost'):
        raise RemoteSiteError('social_origin_not_public')
    netloc = f'[{hostname}]' if ':' in hostname else hostname
    return urlunsplit(('https', netloc, parsed.path.rstrip('/'), '', ''))


@dataclass(frozen=True)
class RemoteSite:
    id: str
    name: str
    origin: str
    human_key: str
    ai_key: str
    enabled: bool
    created_at: float
    updated_at: float

    def public_dict(self) -> dict:
        return {'id': self.id, 'name': self.name, 'origin': self.origin,
                'has_human_key': bool(self.human_key), 'has_ai_key': bool(self.ai_key),
                'enabled': self.enabled, 'created_at': self.created_at,
                'updated_at': self.updated_at}


class RemoteSiteStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def list(self) -> list[RemoteSite]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text('utf-8'))
            if payload.get('schema_version') != 1 or not isinstance(payload.get('sites'), list):
                raise ValueError
            return [self._decode(row) for row in payload['sites']]
        except (OSError, TypeError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            raise RemoteSiteError('invalid_social_site_storage') from exc

    def get(self, site_id: str) -> RemoteSite:
        return next((site for site in self.list() if site.id == site_id),
                    None) or self._not_found()

    @staticmethod
    def _not_found():
        raise RemoteSiteError('social_site_not_found')

    def save(self, *, name: str, origin: str, human_key: str = '', ai_key: str = '',
             enabled: bool = True, site_id: str = '') -> RemoteSite:
        name = str(name or '').strip()
        if not 1 <= len(name) <= 40 or any(ord(ch) < 32 for ch in name):
            raise RemoteSiteError('invalid_social_site_name')
        origin = normalize_social_origin(origin)
        if any(not isinstance(key, str) or len(key) > 256 for key in (human_key, ai_key)):
            raise RemoteSiteError('invalid_social_site_key')
        if not human_key and not ai_key:
            raise RemoteSiteError('social_site_key_required')
        if not isinstance(enabled, bool):
            raise RemoteSiteError('invalid_social_site_status')
        rows = self.list()
        existing = next((site for site in rows if site.id == site_id), None) if site_id else None
        if site_id and existing is None:
            raise RemoteSiteError('social_site_not_found')
        if any(site.id != site_id and site.origin == origin for site in rows):
            raise RemoteSiteError('social_site_duplicate_origin')
        now = time.time()
        site = RemoteSite(site_id or uuid.uuid4().hex, name, origin, human_key, ai_key,
                          enabled, existing.created_at if existing else now, now)
        self._write([site if row.id == site_id else row for row in rows] if existing else [*rows, site])
        return site

    def delete(self, site_id: str) -> None:
        rows = self.list()
        remaining = [site for site in rows if site.id != site_id]
        if len(remaining) == len(rows):
            raise RemoteSiteError('social_site_not_found')
        self._write(remaining)

    @staticmethod
    def _decode(row: dict) -> RemoteSite:
        if not isinstance(row, dict) or set(row) != set(RemoteSite.__dataclass_fields__):
            raise RemoteSiteError('invalid_social_site_storage')
        keys = []
        for field in ('human_key', 'ai_key'):
            secret = row[field]
            if not isinstance(secret, str) or secret and not secret.startswith(PREFIX):
                raise RemoteSiteError('invalid_social_site_storage')
            keys.append(unprotect(secret) if secret else '')
        site = RemoteSite(row['id'], row['name'], normalize_social_origin(row['origin']),
                          keys[0], keys[1], row['enabled'], row['created_at'], row['updated_at'])
        if (not isinstance(site.id, str) or len(site.id) != 32
                or any(ch not in '0123456789abcdef' for ch in site.id)
                or not isinstance(site.enabled, bool)):
            raise RemoteSiteError('invalid_social_site_storage')
        return site

    def _write(self, sites: list[RemoteSite]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        payload = {'schema_version': 1, 'sites': [
            {**asdict(site), 'human_key': protect(site.human_key) if site.human_key else '',
             'ai_key': protect(site.ai_key) if site.ai_key else ''} for site in sites]}
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)


_STORE: RemoteSiteStore | None = None


def get_remote_site_store() -> RemoteSiteStore:
    global _STORE
    if _STORE is None:
        _STORE = RemoteSiteStore(Path(__file__).resolve().parents[1] / 'data' / 'social-feed' / 'remote-sites.json')
    return _STORE
