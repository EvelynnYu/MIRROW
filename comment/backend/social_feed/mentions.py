"""Structured, home-scoped mentions. Display names never prove identity.

The referenced post/comment remains the body authority. Mention receipts hold
only IDs and read/announcement facts and are committed with the originating act.
"""
from __future__ import annotations

import json
import re
import time
import uuid


def normalise(value=None, *, private=False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('invalid_mentions')
    result = []
    for actor in value:
        if not isinstance(actor, str) or not re.fullmatch(r'(aning|k|visitor:[A-Za-z0-9_-]{1,90})', actor):
            raise ValueError('invalid_mentions')
        if private and actor not in {'aning', 'k'}:
            raise ValueError('private_mentions_home_only')
        if actor not in result:
            result.append(actor)
    return result


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def decode(value) -> list[str]:
    try:
        rows = json.loads(value or '[]')
        # Migrated historical references are readable but never writable targets.
        if not isinstance(rows, list) or len(rows) > 8:
            return []
        if any(not isinstance(actor, str) or not re.fullmatch(
                r'(aning|k|visitor:[A-Za-z0-9_-]{1,90}|archive:[a-f0-9]{32})', actor) for actor in rows):
            return []
        return list(dict.fromkeys(rows))
    except (ValueError, TypeError):
        return []


def project(row: dict) -> dict:
    row['mention_actor_ids'] = decode(row.pop('mentions_json', '[]'))
    return row


async def initialize_private(db):
    for table in ('social_moments', 'social_comments'):
        columns = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
        if 'mentions_json' not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'")


def initialize_public(db):
    for table in ('wall_moments', 'wall_comments'):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        if 'mentions_json' not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS wall_mention_notifications (
            id TEXT PRIMARY KEY, recipient TEXT NOT NULL, actor TEXT NOT NULL,
            moment_id TEXT NOT NULL REFERENCES wall_moments(id) ON DELETE CASCADE,
            comment_id TEXT REFERENCES wall_comments(id) ON DELETE CASCADE,
            created_at REAL NOT NULL, read_at REAL, read_source TEXT NOT NULL DEFAULT '',
            read_source_id TEXT NOT NULL DEFAULT '', announced_at REAL,
            announce_source TEXT NOT NULL DEFAULT '', announce_source_id TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS wall_mention_once
            ON wall_mention_notifications(moment_id,ifnull(comment_id,''),recipient);
        CREATE INDEX IF NOT EXISTS wall_mention_unread
            ON wall_mention_notifications(recipient,read_at,created_at);
    ''')


def validate_public(db, values) -> list[str]:
    ids = normalise(values)
    for actor in ids:
        if actor in {'aning', 'k'}:
            continue
        visitor = actor[8:]
        if (not db.execute('SELECT 1 FROM wall_registered_names WHERE visitor_id=?', (visitor,)).fetchone()
                or db.execute('SELECT 1 FROM wall_removed_contacts WHERE visitor_id=?', (visitor,)).fetchone()):
            raise ValueError('mention_target_unavailable')
        from lounge_reception.runtime import current_runtime
        runtime = current_runtime()
        if runtime:
            from .household_identity import registered_social_visitor, has_active_key
            try:
                registered_social_visitor(runtime, visitor)
                if not has_active_key(runtime, visitor):
                    raise ValueError('mention_target_unavailable')
            except Exception:
                raise ValueError('mention_target_unavailable') from None
    return ids


def deliver(db, ids, actor, moment_id, comment_id, now):
    for recipient in ids:
        if recipient == actor:
            continue
        # One event yields one notice, even when the author/parent is also @ed.
        db.execute('DELETE FROM wall_notifications WHERE recipient=? AND moment_id=? '
                   'AND kind=? AND ifnull(comment_id,\'\')=?',
                   (recipient, moment_id, 'comment' if comment_id else 'moment', comment_id or ''))
        db.execute('INSERT OR IGNORE INTO wall_mention_notifications '
                   '(id,recipient,actor,moment_id,comment_id,created_at) VALUES(?,?,?,?,?,?)',
                   ('wmn_' + uuid.uuid4().hex, recipient, actor, moment_id, comment_id, now))


def notices(db, recipient, *, unannounced=False, limit=None):
    where = 'n.recipient=? AND n.read_at IS NULL AND m.withdrawn_at IS NULL'
    if unannounced:
        where += ' AND n.announced_at IS NULL'
    suffix = '' if limit is None else ' LIMIT ?'
    params = (recipient,) if limit is None else (recipient, max(1, min(100, int(limit))))
    return [dict(row) for row in db.execute(
        "SELECT n.id,n.actor,'mention' AS kind,n.moment_id,n.comment_id,n.created_at,"
        'm.content AS moment_content,c.content AS comment_content '
        'FROM wall_mention_notifications n JOIN wall_moments m ON m.id=n.moment_id '
        'LEFT JOIN wall_comments c ON c.id=n.comment_id '
        f'WHERE {where} ORDER BY n.created_at,n.id{suffix}', params)]


def claim(db, recipient, ids, source, source_id, *, announcement=False):
    field = 'announced' if announcement else 'read'
    if not isinstance(ids, list) or any(not isinstance(item, str) or len(item) > 100 for item in ids):
        raise ValueError('invalid_notification_claim')
    bounded = list(dict.fromkeys(ids))[:100]
    if not bounded:
        return 0
    if any(not isinstance(item, str) or len(item) > 100 for item in bounded):
        raise ValueError('invalid_notification_claim')
    marks = ','.join('?' for _ in bounded)
    source_field = 'announce' if announcement else 'read'
    result = db.execute(f'UPDATE wall_mention_notifications SET {field}_at=?,{source_field}_source=?,{source_field}_source_id=? '
                        f'WHERE recipient=? AND read_at IS NULL AND id IN ({marks})'
                        + (' AND announced_at IS NULL' if announcement else ''),
                        (time.time(), source[:40], source_id[:200], recipient, *bounded))
    return max(0, result.rowcount)


def notice_projection(row, recipient):
    item = dict(row)
    targets = decode(item.pop('mention_targets', '[]'))
    if recipient in targets:
        item['kind'] = 'mention'
    return item


def candidates(runtime, wall, viewer, *, private=False, query='', limit=30):
    """Current public names only, filtered through real admission, never keys."""
    from .public_gateway import _display
    from .household_identity import registered_social_visitor, has_active_key
    actors = ['aning', 'k']
    if not private:
        removed = set(wall.removed_contacts())
        actors += ['visitor:' + identifier for identifier in wall.registered_names()
                   if identifier not in removed]
    needle = str(query or '').strip().casefold()[:40]
    rows = []
    for actor in actors:
        if actor.startswith('visitor:'):
            try:
                registered_social_visitor(runtime, actor[8:])
                if not has_active_key(runtime, actor[8:]):
                    continue
            except Exception:
                continue
        person = _display(runtime, actor, viewer=viewer)
        if needle and needle not in str(person.get('name', '')).casefold() and needle not in str(person.get('nickname', '')).casefold():
            continue
        rows.append({key: person.get(key, '') for key in ('actor_id', 'name', 'nickname', 'avatar')})
        if len(rows) >= max(1, min(30, limit)):
            break
    return {'items': rows, 'capability': 'mentions_v1'}
