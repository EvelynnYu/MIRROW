"""Per-post, recoverable transfer between independently hosted public walls.

Schema creation is deliberately explicit.  A normal backend boot never changes
the production public-wall database merely because this module was imported.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

import httpx

from lounge_visits.network_guard import pin_request, public_destination

from .public_wall import PublicWallError
from .remote_sites import normalize_social_origin


MAX_SNAPSHOT_BYTES = 750_000


def migrate_schema(db: sqlite3.Connection) -> None:
    db.executescript('''BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS wall_transfer_out (
            id TEXT PRIMARY KEY,
            moment_id TEXT NOT NULL UNIQUE,
            author TEXT NOT NULL,
            destination_origin TEXT NOT NULL,
            target_actor TEXT NOT NULL,
            digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','committed','cancelled')),
            destination_moment_id TEXT NOT NULL DEFAULT '',
            prepared_at REAL NOT NULL,
            committed_at REAL
        );
        CREATE TABLE IF NOT EXISTS wall_transfer_in (
            id TEXT PRIMARY KEY,
            source_origin TEXT NOT NULL,
            source_actor TEXT NOT NULL,
            target_actor TEXT NOT NULL,
            source_moment_id TEXT NOT NULL,
            destination_moment_id TEXT NOT NULL UNIQUE,
            digest TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            proof_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('staged','active')),
            staged_at REAL NOT NULL,
            activated_at REAL,
            UNIQUE(source_origin, source_moment_id)
        );
        CREATE TABLE IF NOT EXISTS wall_transfer_people (
            actor_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
        );
        COMMIT;
    ''')
    try:
        db.execute("ALTER TABLE wall_moments ADD COLUMN forward_json TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def ready(db: sqlite3.Connection) -> bool:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name IN ('wall_transfer_out','wall_transfer_in','wall_transfer_people')")}
    return names == {'wall_transfer_out', 'wall_transfer_in', 'wall_transfer_people'}


def require_ready(db: sqlite3.Connection) -> None:
    if not ready(db):
        raise PublicWallError('transfer_schema_not_ready')


def assert_mutable(db: sqlite3.Connection, moment_id: str) -> None:
    if ready(db) and db.execute("SELECT 1 FROM wall_transfer_out WHERE moment_id=? AND state='prepared'",
                                (moment_id,)).fetchone():
        raise PublicWallError('moment_transfer_pending')


def canonical(snapshot: dict) -> bytes:
    data = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise PublicWallError('transfer_snapshot_too_large')
    return data


def digest(snapshot: dict) -> str:
    return hashlib.sha256(canonical(snapshot)).hexdigest()


def historical_actor(source_origin: str, actor: str) -> str:
    return 'archive:' + hashlib.sha256((source_origin + '\0' + actor).encode()).hexdigest()[:32]


def validate_origin(origin: str) -> str:
    normalized = normalize_social_origin(origin)
    if urlsplit(normalized).path:
        raise PublicWallError('invalid_transfer_origin')
    return normalized


def prepare(wall, actor: str, moment_id: str, destination_origin: str, target_actor: str) -> dict:
    actor = wall._actor(actor)
    destination_origin = validate_origin(destination_origin)
    if target_actor not in {'aning', 'k'}:
        raise PublicWallError('invalid_transfer_target')
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        row = db.execute('SELECT * FROM wall_moments WHERE id=? AND withdrawn_at IS NULL',
                         (moment_id,)).fetchone()
        if not row:
            raise PublicWallError('moment_not_found')
        if row['author'] != actor or not actor.startswith('visitor:'):
            raise PublicWallError('forbidden')
        prior = db.execute('SELECT * FROM wall_transfer_out WHERE moment_id=?', (moment_id,)).fetchone()
        active_prior = prior if prior and prior['state'] == 'prepared' else None
        if prior and prior['state'] == 'committed':
            raise PublicWallError('transfer_conflict')
        if active_prior:
            if (prior['author'] != actor or prior['destination_origin'] != destination_origin
                    or prior['target_actor'] != target_actor):
                raise PublicWallError('transfer_conflict')
        item = wall._item(db, row)
        # Snapshot only the durable reference.  A source from this home must
        # become an explicit old-home link rather than a destination-local ID.
        from .forwarding import decode_forward_ref
        ref = decode_forward_ref(row['forward_json'])
        item.pop('forward', None)
        if ref:
            if not ref['origin']:
                from .public_gateway import PUBLIC_SOCIAL_ORIGIN
                ref = {**ref, 'origin': validate_origin(PUBLIC_SOCIAL_ORIGIN)}
            item['forward_ref'] = ref
        actors = {item['author'], *(c['author'] for c in item['comments']),
                  *(r['author'] for r in item['reactions'])}
        actors.update(item.get('mention_actor_ids', []))
        for comment in item['comments']:
            actors.update(comment.get('mention_actor_ids', []))
        people = {}
        for other in actors:
            profile = db.execute('SELECT nickname FROM wall_profiles WHERE actor_id=?', (other,)).fetchone()
            people[other] = str((profile['nickname'] if profile and profile['nickname'] else '') or
                                ('站主' if other == 'aning' else 'AI' if other == 'k' else '未设置网名'))[:40]
        snapshot = {'moment': item, 'people': people}
        snap_digest = digest(snapshot)
        if active_prior and active_prior['digest'] != snap_digest:
            raise PublicWallError('transfer_snapshot_changed')
        transfer_id = active_prior['id'] if active_prior else 'wt_' + uuid.uuid4().hex
        if prior and prior['state'] == 'cancelled':
            db.execute("UPDATE wall_transfer_out SET id=?,author=?,destination_origin=?,target_actor=?,digest=?,"
                       "state='prepared',destination_moment_id='',prepared_at=?,committed_at=NULL WHERE moment_id=?",
                       (transfer_id, actor, destination_origin, target_actor, snap_digest, time.time(), moment_id))
        elif not prior:
            db.execute('INSERT INTO wall_transfer_out(id,moment_id,author,destination_origin,target_actor,digest,state,prepared_at) '
                       "VALUES(?,?,?,?,?,?,'prepared',?)",
                       (transfer_id, moment_id, actor, destination_origin, target_actor, snap_digest, time.time()))
    return {'transfer_id': transfer_id, 'source_moment_id': moment_id,
            'destination_origin': destination_origin, 'target_actor': target_actor,
            'digest': snap_digest, 'snapshot': snapshot, 'state': 'prepared'}


def stage(wall, *, transfer_id: str, source_origin: str, source_actor: str,
          target_actor: str, snapshot: dict, expected_digest: str) -> dict:
    if (not isinstance(transfer_id, str) or not re.fullmatch(r'wt_[a-f0-9]{32}', transfer_id)
            or not isinstance(expected_digest, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_digest)):
        raise PublicWallError('invalid_transfer_request')
    source_origin = validate_origin(source_origin)
    if target_actor not in {'aning', 'k'} or not source_actor.startswith('visitor:'):
        raise PublicWallError('invalid_transfer_identity')
    if digest(snapshot) != expected_digest:
        raise PublicWallError('transfer_digest_mismatch')
    item = snapshot.get('moment')
    if (not isinstance(item, dict) or item.get('author') != source_actor or
            not isinstance(item.get('id'), str) or not item['id'].startswith('sw_') or
            not isinstance(item.get('content'), str) or not 1 <= len(item['content']) <= 1200 or
            item.get('visibility') != 'public' or not isinstance(item.get('comments'), list) or
            not isinstance(item.get('reactions'), list) or not isinstance(snapshot.get('people'), dict)):
        raise PublicWallError('invalid_transfer_snapshot')
    if item.get('forward_ref') is not None:
        from .forwarding import normalize_forward_ref
        try:
            if normalize_forward_ref(item['forward_ref']) != item['forward_ref'] or not item['forward_ref']['origin']:
                raise ValueError
        except (ValueError, TypeError):
            raise PublicWallError('invalid_transfer_snapshot') from None
    def valid_time(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value) and 0 < value < 100_000_000_000
        except OverflowError:
            return False

    if (not valid_time(item.get('created_at')) or not valid_time(item.get('updated_at'))
            or not isinstance(item.get('revision'), int) or item['revision'] < 1
            or any(ord(ch) < 32 and ch not in '\n\t' for ch in item['content'])):
        raise PublicWallError('invalid_transfer_snapshot')
    if len(item['comments']) > 2000 or len(item['reactions']) > 2000:
        raise PublicWallError('transfer_snapshot_too_large')
    if any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in item['comments']):
        raise PublicWallError('invalid_transfer_snapshot')
    comment_ids = {row['id'] for row in item['comments']}
    if len(comment_ids) != len(item['comments']):
        raise PublicWallError('invalid_transfer_snapshot')
    for row in item['comments']:
        if (not isinstance(row.get('id'), str) or not isinstance(row.get('author'), str)
                or not isinstance(row.get('content'), str) or not 1 <= len(row['content']) <= 600
                or any(ord(ch) < 32 and ch not in '\n\t' for ch in row['content'])
                or row.get('reply_to_id') not in (None, *comment_ids)
                or not valid_time(row.get('created_at'))):
            raise PublicWallError('invalid_transfer_snapshot')
    for row in item['reactions']:
        if (not isinstance(row, dict) or not isinstance(row.get('author'), str)
                or row.get('reaction') != 'like' or not valid_time(row.get('created_at'))):
            raise PublicWallError('invalid_transfer_snapshot')
    people = snapshot['people']
    actors = {source_actor, *(row['author'] for row in item['comments']),
              *(row['author'] for row in item['reactions'])}
    from .mentions import encode, decode
    for entry in [item, *item['comments']]:
        mentions = entry.get('mention_actor_ids', [])
        if not isinstance(mentions, list) or decode(encode(mentions)) != mentions:
            raise PublicWallError('invalid_transfer_snapshot')
        actors.update(mentions)
    if any(not isinstance(people.get(actor), str) or not 1 <= len(people[actor]) <= 40 for actor in actors):
        raise PublicWallError('invalid_transfer_snapshot')
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        prior = db.execute('SELECT * FROM wall_transfer_in WHERE id=?', (transfer_id,)).fetchone()
        if prior:
            if (prior['source_origin'], prior['source_actor'], prior['target_actor'], prior['digest']) != (
                    source_origin, source_actor, target_actor, expected_digest):
                raise PublicWallError('transfer_conflict')
            proof = secrets.token_urlsafe(32) if prior['state'] == 'staged' else ''
            if proof:
                db.execute('UPDATE wall_transfer_in SET proof_hash=? WHERE id=?',
                           (hashlib.sha256(proof.encode()).hexdigest(), transfer_id))
            return {'transfer_id': transfer_id, 'destination_moment_id': prior['destination_moment_id'],
                    'state': prior['state'], 'proof': proof}
        destination_id = 'sw_' + uuid.uuid4().hex
        proof = secrets.token_urlsafe(32)
        db.execute('INSERT INTO wall_transfer_in(id,source_origin,source_actor,target_actor,source_moment_id,'
                   'destination_moment_id,digest,snapshot_json,proof_hash,state,staged_at) '
                   "VALUES(?,?,?,?,?,?,?,?,?,'staged',?)",
                   (transfer_id, source_origin, source_actor, target_actor, item['id'], destination_id,
                    expected_digest, canonical(snapshot).decode(), hashlib.sha256(proof.encode()).hexdigest(), time.time()))
    return {'transfer_id': transfer_id, 'destination_moment_id': destination_id,
            'state': 'staged', 'proof': proof}


def prove(wall, transfer_id: str, proof: str, expected_digest: str, destination_moment_id: str) -> dict:
    with wall._connect() as db:
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_in WHERE id=?', (transfer_id,)).fetchone()
    if (not row or row['digest'] != expected_digest or row['destination_moment_id'] != destination_moment_id
            or not secrets.compare_digest(row['proof_hash'], hashlib.sha256(proof.encode()).hexdigest())):
        raise PublicWallError('transfer_proof_rejected')
    return {'transfer_id': transfer_id, 'digest': row['digest'],
            'destination_moment_id': row['destination_moment_id'], 'state': row['state']}


async def verify_remote_stage(origin: str, transfer_id: str, proof: str, expected_digest: str,
                              destination_moment_id: str, *, transport=None) -> None:
    """Old home checks the new home's durable stage, with a pinned public IP."""
    origin = validate_origin(origin)
    url = origin + '/social/v1/transfers/' + transfer_id + '/proof'
    try:
        address = await public_destination(url)
        expected = urlsplit(origin)

        async def pin(request: httpx.Request) -> None:
            pin_request(request, expected, address)

        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False,
                                     trust_env=False, event_hooks={'request': [pin]}) as client:
            async with client.stream('POST', url, json={'proof': proof, 'digest': expected_digest,
                                                        'destination_moment_id': destination_moment_id},
                                     headers={'Accept': 'application/json'}) as response:
                if response.status_code != 200:
                    raise PublicWallError('transfer_destination_unavailable')
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 4096:
                        raise PublicWallError('transfer_destination_unavailable')
        answer = json.loads(body)
    except (ValueError, httpx.HTTPError, TimeoutError, TypeError):
        raise PublicWallError('transfer_destination_unavailable') from None
    if (not isinstance(answer, dict) or answer.get('transfer_id') != transfer_id
            or answer.get('digest') != expected_digest
            or answer.get('destination_moment_id') != destination_moment_id
            or answer.get('state') not in {'staged', 'active'}):
        raise PublicWallError('transfer_destination_unavailable')


def outbound_status(wall, actor: str, transfer_id: str) -> dict:
    with wall._connect() as db:
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_out WHERE id=?', (transfer_id,)).fetchone()
    if not row or row['author'] != actor:
        raise PublicWallError('transfer_not_found')
    return {key: row[key] for key in ('id', 'moment_id', 'destination_origin', 'target_actor',
                                     'digest', 'state', 'destination_moment_id')}


def cancel(wall, actor: str, transfer_id: str) -> dict:
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_out WHERE id=?', (transfer_id,)).fetchone()
        if not row or row['author'] != actor:
            raise PublicWallError('transfer_not_found')
        if row['state'] == 'cancelled':
            return {'id': transfer_id, 'state': 'cancelled'}
        if row['state'] != 'prepared':
            raise PublicWallError('transfer_already_committed')
        db.execute("UPDATE wall_transfer_out SET state='cancelled' WHERE id=?", (transfer_id,))
    return {'id': transfer_id, 'state': 'cancelled'}


def inbound_record(wall, source_origin: str, source_moment_id: str) -> dict | None:
    with wall._connect() as db:
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_in WHERE source_origin=? AND source_moment_id=?',
                         (source_origin, source_moment_id)).fetchone()
    return dict(row) if row else None


def inbound_by_id(wall, transfer_id: str) -> dict | None:
    with wall._connect() as db:
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_in WHERE id=?', (transfer_id,)).fetchone()
    return dict(row) if row else None


def inbound_pending(wall) -> list[dict]:
    with wall._connect() as db:
        require_ready(db)
        rows = db.execute("SELECT id,source_origin,source_moment_id,target_actor,staged_at "
                          "FROM wall_transfer_in WHERE state='staged' ORDER BY staged_at DESC").fetchall()
    return [dict(row) for row in rows]


def discard_staged(wall, transfer_id: str) -> None:
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        row = db.execute('SELECT state FROM wall_transfer_in WHERE id=?', (transfer_id,)).fetchone()
        if row and row['state'] == 'active':
            raise PublicWallError('transfer_already_committed')
        db.execute('DELETE FROM wall_transfer_in WHERE id=?', (transfer_id,))


def commit(wall, actor: str, transfer_id: str, destination_moment_id: str) -> dict:
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_out WHERE id=?', (transfer_id,)).fetchone()
        if not row or row['author'] != actor:
            raise PublicWallError('transfer_not_found')
        if row['state'] == 'committed':
            if row['destination_moment_id'] != destination_moment_id:
                raise PublicWallError('transfer_conflict')
            return {key: row[key] for key in ('id', 'moment_id', 'destination_origin', 'target_actor',
                                              'digest', 'state', 'destination_moment_id')}
        if row['state'] != 'prepared':
            raise PublicWallError('transfer_conflict')
        now = time.time()
        db.execute("UPDATE wall_moments SET mentions_json='[]',content='',forward_json='',withdrawn_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                   (now, now, row['moment_id']))
        db.execute('DELETE FROM wall_comments WHERE moment_id=?', (row['moment_id'],))
        db.execute('DELETE FROM wall_likes WHERE moment_id=?', (row['moment_id'],))
        db.execute('DELETE FROM wall_notifications WHERE moment_id=?', (row['moment_id'],))
        db.execute('DELETE FROM wall_mention_notifications WHERE moment_id=?', (row['moment_id'],))
        db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                   (row['moment_id'], 'withdrawn', now))
        db.execute("UPDATE wall_transfer_out SET state='committed',destination_moment_id=?,committed_at=? WHERE id=?",
                   (destination_moment_id, now, transfer_id))
    return outbound_status(wall, actor, transfer_id)


def activate(wall, transfer_id: str, source_status: dict) -> dict:
    with wall._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db)
        row = db.execute('SELECT * FROM wall_transfer_in WHERE id=?', (transfer_id,)).fetchone()
        if not row:
            raise PublicWallError('transfer_not_found')
        if (source_status.get('state') != 'committed' or source_status.get('id') != transfer_id
                or source_status.get('moment_id') != row['source_moment_id']
                or source_status.get('digest') != row['digest']
                or source_status.get('target_actor') != row['target_actor']
                or source_status.get('destination_moment_id') != row['destination_moment_id']):
            raise PublicWallError('transfer_source_not_committed')
        if row['state'] == 'active':
            return {'id': row['destination_moment_id'], 'state': 'active'}
        item = json.loads(row['snapshot_json'])['moment']
        people = json.loads(row['snapshot_json'])['people']
        now = time.time()
        from .mentions import encode
        def transferred_mentions(entry):
            return encode([row['target_actor'] if actor == row['source_actor'] else historical_actor(row['source_origin'], actor)
                           for actor in entry.get('mention_actor_ids', [])])
        from .forwarding import encode_forward_ref
        db.execute('INSERT INTO wall_moments(id,author,content,created_at,updated_at,revision,mentions_json,forward_json) VALUES(?,?,?,?,?,?,?,?)',
                   (row['destination_moment_id'], row['target_actor'], item['content'],
                    float(item['created_at']), now, max(1, int(item['revision'])), transferred_mentions(item),
                    encode_forward_ref(item.get('forward_ref'))))
        comment_map = {comment['id']: 'wc_' + uuid.uuid4().hex for comment in item['comments']}
        for comment in item['comments']:
            actor = historical_actor(row['source_origin'], comment['author'])
            db.execute('INSERT INTO wall_comments(id,moment_id,author,content,reply_to_id,created_at,mentions_json) VALUES(?,?,?,?,?,?,?)',
                       (comment_map[comment['id']], row['destination_moment_id'], actor,
                        comment['content'], None,
                        float(comment['created_at']), transferred_mentions(comment)))
        for comment in item['comments']:
            if comment.get('reply_to_id'):
                db.execute('UPDATE wall_comments SET reply_to_id=? WHERE id=?',
                           (comment_map[comment['reply_to_id']], comment_map[comment['id']]))
        for reaction in item['reactions']:
            actor = historical_actor(row['source_origin'], reaction['author'])
            db.execute('INSERT OR IGNORE INTO wall_likes(moment_id,actor,created_at) VALUES(?,?,?)',
                       (row['destination_moment_id'], actor, float(reaction['created_at'])))
        for original, label in people.items():
            db.execute('INSERT OR IGNORE INTO wall_transfer_people(actor_id,display_name) VALUES(?,?)',
                       (historical_actor(row['source_origin'], original), str(label)[:40]))
        db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                   (row['destination_moment_id'], 'upsert', now))
        db.execute("UPDATE wall_transfer_in SET state='active',snapshot_json='',proof_hash='',activated_at=? WHERE id=?",
                   (now, transfer_id))
    return {'id': row['destination_moment_id'], 'state': 'active'}
