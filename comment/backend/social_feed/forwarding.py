"""Validation and read-time projection for social-feed forwards.

Only a reference is durable.  Same-home source material is resolved from the
current public wall on every read; remote references deliberately remain links.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from .remote_sites import RemoteSiteError, normalize_social_origin


class ForwardError(ValueError):
    pass


def normalize_forward_ref(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'origin', 'moment_id'}:
        raise ForwardError('invalid_forward_ref')
    origin, moment_id = value.get('origin'), value.get('moment_id')
    if not isinstance(origin, str) or not isinstance(moment_id, str):
        raise ForwardError('invalid_forward_ref')
    moment_id = moment_id.strip()
    if not 1 <= len(moment_id) <= 200 or any(ord(ch) < 33 or ord(ch) == 127 for ch in moment_id):
        raise ForwardError('invalid_forward_ref')
    if not origin.strip():
        return {'origin': '', 'moment_id': moment_id}
    try:
        origin = normalize_social_origin(origin)
    except RemoteSiteError:
        raise ForwardError('invalid_forward_origin') from None
    return {'origin': origin, 'moment_id': moment_id}


def encode_forward_ref(value):
    ref = normalize_forward_ref(value)
    return json.dumps(ref, separators=(',', ':')) if ref else ''


def decode_forward_ref(value):
    if not value:
        return None
    try:
        return normalize_forward_ref(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def external_projection(ref):
    return {'reference': ref, 'status': 'link_only',
            'url': ref['origin'] + '/?moment=' + quote(ref['moment_id'], safe='')}


def same_home_projection(db, ref):
    row = db.execute(
        "SELECT id,author,content,created_at,withdrawn_at,forward_json FROM wall_moments WHERE id=?",
        (ref['moment_id'],)).fetchone()
    if not row or row['withdrawn_at'] is not None or decode_forward_ref(row['forward_json']):
        return {'reference': ref, 'status': 'unavailable'}
    # A transferred source is no longer publicly hosted by this home.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wall_transfer_out'").fetchone() and db.execute(
            "SELECT 1 FROM wall_transfer_out WHERE moment_id=? AND state='committed'", (ref['moment_id'],)).fetchone():
        return {'reference': ref, 'status': 'unavailable'}
    return {'reference': ref, 'status': 'available', 'source': {
        'id': row['id'], 'author': row['author'], 'content': row['content'],
        'created_at': row['created_at'], 'visibility': 'public'}}
