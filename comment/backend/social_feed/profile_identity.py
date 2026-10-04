"""Opt-in profile custody. Visitor credentials and wall ownership stay local.

Only a public nickname, normalized avatar and opaque identity are exchanged.
Schema creation is explicit; merely importing or reading this module is safe.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .public_wall import PublicWallError


TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
PROFILE = re.compile(r'^[0-9a-f]{32}$')


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def home_grant(row) -> bool:
    """The local human explicitly delegates its own household's public profile."""
    return (row['issuer'] == 'aning' and row['key_id'] == '__owner__'
            and (row['actor'], row['kind']) in {('aning', 'human'), ('k', 'ai')})


class ProfileIdentityStore:
    def __init__(self, wall):
        self.wall = wall

    def ready(self) -> bool:
        with self.wall._connect() as db:
            return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_identity_links'").fetchone())

    def migrate(self, *, backup_ready=False):
        if backup_ready is not True:
            raise PublicWallError('profile_backup_required')
        with self.wall._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profile_identity_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO profile_identity_policy(id,enabled) VALUES(1,0);
                CREATE TABLE IF NOT EXISTS profile_identity_ids (
                    actor TEXT PRIMARY KEY, profile_id TEXT UNIQUE NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, digest TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS profile_identity_grants (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, kind TEXT NOT NULL,
                    issuer TEXT NOT NULL, key_id TEXT NOT NULL, audience TEXT NOT NULL,
                    ticket_hash TEXT UNIQUE NOT NULL, ticket_expires REAL NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL, encrypted_token TEXT NOT NULL,
                    consumer TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    expires REAL NOT NULL, revoked REAL);
                CREATE INDEX IF NOT EXISTS profile_grants_actor ON profile_identity_grants(actor);
                CREATE TABLE IF NOT EXISTS profile_identity_links (
                    actor TEXT PRIMARY KEY, origin TEXT NOT NULL, profile_id TEXT NOT NULL,
                    encrypted_token TEXT NOT NULL, kind TEXT NOT NULL,
                    nickname TEXT NOT NULL, avatar TEXT NOT NULL,
                    version INTEGER NOT NULL, checked REAL NOT NULL,
                    next_check REAL NOT NULL, status TEXT NOT NULL,
                    UNIQUE(origin,profile_id));
                CREATE INDEX IF NOT EXISTS profile_links_due ON profile_identity_links(next_check);
            """)

    def require_ready(self):
        if not self.ready():
            raise PublicWallError('profile_schema_not_ready')

    def enabled(self) -> bool:
        if not self.ready():
            return False
        with self.wall._connect() as db:
            return bool(db.execute('SELECT enabled FROM profile_identity_policy WHERE id=1').fetchone()[0])

    def set_enabled(self, value: bool):
        self.require_ready()
        with self.wall._connect() as db:
            db.execute('UPDATE profile_identity_policy SET enabled=? WHERE id=1', (bool(value),))

    def link(self, actor: str):
        if not self.ready():
            return None
        with self.wall._connect() as db:
            row = db.execute('SELECT * FROM profile_identity_links WHERE actor=?', (actor,)).fetchone()
        return dict(row) if row else None

    def status(self, actor: str):
        row = self.link(actor)
        if not row:
            return {'linked': False}
        return {'linked': True, 'origin': row['origin'], 'identity_id': row['origin'] + '#' + row['profile_id'],
                'version': row['version'], 'checked_at': row['checked'], 'status': row['status']}

    def source_identity(self, actor, origin):
        """Display the same opaque ID on the source wall without minting on read."""
        from .profile_registration import ProfileRegistrationStore
        if not self.ready() or self.link(actor) or not ProfileRegistrationStore(self.wall).state(actor)['can_interact']:return None
        with self.wall._connect() as db:
            row=db.execute('SELECT profile_id FROM profile_identity_ids WHERE actor=?',(actor,)).fetchone()
        return {'linked':False,'hosted':True,'origin':origin,'identity_id':origin+'#'+row['profile_id'],'status':'source'} if row else None

    def overlay(self, actor: str, local: dict):
        row = self.link(actor)
        if row:
            return {**local, 'nickname': row['nickname'], 'avatar': row['avatar'],
                    'profile_identity': self.status(actor)}
        from .profile_registration import ProfileRegistrationStore
        if ProfileRegistrationStore(self.wall).state(actor)['state'] == 'pending':
            return {**local, 'nickname': '', 'avatar': ''}
        return local

    def guard_write(self, actor: str):
        from .profile_registration import ProfileRegistrationStore
        ProfileRegistrationStore(self.wall).guard(actor)
        if self.link(actor):
            raise PublicWallError('profile_edit_at_source')

    def make_grant(self, actor, kind, issuer, key_id, audience, origin, *, transaction=None):
        from lounge_visits.secret_vault import protect
        self.require_ready()
        if not self.enabled() and not home_grant({'actor': actor, 'kind': kind, 'issuer': issuer, 'key_id': key_id}):
            raise PublicWallError('profile_hosting_disabled')
        self.guard_write(actor)  # A projected identity cannot become a second authority.
        ticket, token, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
        encrypted = protect(token)
        with (nullcontext(transaction) if transaction is not None else self.wall._connect()) as db:
            if transaction is None: db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            if db.execute('SELECT 1 FROM profile_identity_links WHERE actor=?',(actor,)).fetchone():
                raise PublicWallError('profile_edit_at_source')
            db.execute('DELETE FROM profile_identity_grants WHERE ticket_expires<? AND consumer=\'\'', (now,))
            count = db.execute('SELECT COUNT(*) FROM profile_identity_grants WHERE actor=? AND revoked IS NULL AND expires>?', (actor, now)).fetchone()[0]
            if count >= 40:
                raise PublicWallError('profile_grant_limit')
            db.execute('INSERT INTO profile_identity_ids(actor,profile_id) VALUES(?,?) ON CONFLICT(actor) DO NOTHING',
                       (actor, uuid.uuid4().hex))
            db.execute('INSERT INTO profile_identity_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                       (uuid.uuid4().hex, actor, kind, issuer, key_id, audience, digest(ticket), now+300,
                        digest(token), encrypted, '', now, now+90*86400))
        code = base64.urlsafe_b64encode(json.dumps({'v': 1, 'origin': origin, 'ticket': ticket}, separators=(',', ':')).encode()).decode()
        return {'code': code, 'expires_at': now+300}

    def claim(self, ticket, audience, consumer, *, validate=None):
        from lounge_visits.secret_vault import unprotect
        if not TOKEN.fullmatch(ticket):
            raise PublicWallError('profile_ticket_invalid')
        self.require_ready()
        with self.wall._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM profile_identity_grants WHERE ticket_hash=?', (digest(ticket),)).fetchone()
            if (not row or row['revoked'] or row['ticket_expires'] < time.time() or
                    row['audience'] != audience or row['consumer']):
                raise PublicWallError('profile_ticket_invalid')
            if not self.enabled() and not home_grant(row):
                raise PublicWallError('profile_hosting_disabled')
            if validate:
                validate(dict(row))
            db.execute('UPDATE profile_identity_grants SET consumer=? WHERE id=?', (consumer, row['id']))
        return dict(row) | {'consumer': consumer, 'token': unprotect(row['encrypted_token'])}

    def authorization(self, token):
        self.require_ready()
        if not TOKEN.fullmatch(token):
            raise PublicWallError('profile_access_revoked')
        with self.wall._connect() as db:
            row = db.execute('SELECT * FROM profile_identity_grants WHERE token_hash=?', (digest(token),)).fetchone()
        if (not row or row['revoked'] or row['expires'] < time.time() or not row['consumer']
                or not self.enabled() and not home_grant(row)):
            raise PublicWallError('profile_access_revoked')
        return dict(row)

    def grants(self, actor):
        if not self.ready():
            return []
        with self.wall._connect() as db:
            return [dict(r) for r in db.execute('SELECT id,audience,created,expires,revoked FROM profile_identity_grants WHERE actor=? ORDER BY created DESC LIMIT 100', (actor,))]

    def revoke(self, actor, identifier):
        self.require_ready()
        with self.wall._connect() as db:
            db.execute('UPDATE profile_identity_grants SET revoked=? WHERE actor=? AND id=?', (time.time(), actor, identifier))

    def snapshot(self, actor, kind, origin):
        """Raw local public profile only; never cognition, registered names or remarks."""
        self.guard_write(actor)
        from .avatar_images import avatar_path, normalized_png
        with self.wall._connect() as db:
            row = db.execute('SELECT nickname,avatar FROM wall_profiles WHERE actor_id=?', (actor,)).fetchone()
        name, avatar = (row['nickname'], row['avatar']) if row else ('', '')
        if not name:
            raise PublicWallError('profile_public_nickname_required')
        image = ''
        if avatar:
            path = avatar_path(actor)
            if avatar.split('?', 1)[0] != origin+'/social/v1/avatars/'+path.name or not path.is_file():
                raise PublicWallError('profile_uploaded_avatar_required')
            raw = path.read_bytes()
            image = base64.b64encode(normalized_png(raw)).decode('ascii')
        value = {'kind': kind, 'nickname': name, 'avatar_data': image}
        value_digest = digest(json.dumps(value, sort_keys=True, separators=(',', ':')))
        with self.wall._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            if db.execute('SELECT 1 FROM profile_identity_links WHERE actor=?', (actor,)).fetchone():
                raise PublicWallError('profile_edit_at_source')
            db.execute('INSERT INTO profile_identity_ids(actor,profile_id) VALUES(?,?) ON CONFLICT(actor) DO NOTHING', (actor, uuid.uuid4().hex))
            db.execute('UPDATE profile_identity_ids SET version=version+1,digest=? WHERE actor=? AND digest!=?', (value_digest, actor, value_digest))
            identity = db.execute('SELECT profile_id,version FROM profile_identity_ids WHERE actor=?', (actor,)).fetchone()
        return value | {'origin': origin, 'profile_id': identity['profile_id'], 'version': identity['version']}

    def cache(self, actor, origin, kind, token, snapshot, *, refresh=False, public_origin='', expected_token='', transaction=None):
        from lounge_visits.secret_vault import protect
        from .avatar_images import normalized_png
        self.require_ready()
        if (snapshot.get('origin') != origin or snapshot.get('kind') != kind or
                not PROFILE.fullmatch(str(snapshot.get('profile_id', ''))) or
                type(snapshot.get('version')) is not int or snapshot['version'] < 1 or
                not isinstance(snapshot.get('nickname'), str) or not 1 <= len(snapshot['nickname']) <= 40 or
                any(ord(c)<32 for c in snapshot['nickname']) or not TOKEN.fullmatch(token)):
            raise PublicWallError('profile_response_invalid')
        prior = self.link(actor)
        if prior and (prior['origin'] != origin or prior['profile_id'] != snapshot['profile_id']):
            raise PublicWallError('profile_link_conflict')
        if prior and prior['version'] > snapshot['version']:
            raise PublicWallError('profile_version_regressed')
        with self.wall._connect() as db:
            if db.execute('SELECT 1 FROM profile_identity_grants WHERE actor=? AND revoked IS NULL AND expires>? LIMIT 1', (actor,time.time())).fetchone():
                raise PublicWallError('profile_has_outgoing_grants')
        avatar = ''
        image = snapshot.get('avatar_data', '')
        if not isinstance(image, str) or len(image)>400_000:
            raise PublicWallError('profile_response_invalid')
        if image:
            try:
                raw = normalized_png(base64.b64decode(image, validate=True))
            except (ValueError, TypeError):
                raise PublicWallError('profile_response_invalid') from None
            asset = hashlib.sha256(raw).hexdigest()+'.png'
            directory = self.wall.path.parent/'social_profile_avatars'
            directory.mkdir(parents=True,exist_ok=True)
            path = directory/asset
            if not path.exists():
                staged = directory/(uuid.uuid4().hex+'.tmp')
                try:
                    staged.write_bytes(raw)
                    staged.replace(path)
                finally:
                    staged.unlink(missing_ok=True)
            avatar = public_origin+'/social/v1/profile-links/avatars/'+asset
        encrypted, now = protect(token), time.time()
        with (nullcontext(transaction) if transaction is not None else self.wall._connect()) as db:
            if transaction is None: db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_origin_in
            guard_origin_in(db, actor, origin)
            current = db.execute('SELECT origin,profile_id,version,encrypted_token FROM profile_identity_links WHERE actor=?', (actor,)).fetchone()
            if refresh and (not current or current['encrypted_token']!=expected_token):
                raise PublicWallError('profile_link_conflict')
            if current and (current['origin']!=origin or current['profile_id']!=snapshot['profile_id'] or current['version']>snapshot['version']):
                raise PublicWallError('profile_link_conflict')
            if db.execute('SELECT 1 FROM profile_identity_grants WHERE actor=? AND revoked IS NULL AND expires>? LIMIT 1', (actor,time.time())).fetchone():
                raise PublicWallError('profile_has_outgoing_grants')
            other = db.execute('SELECT actor FROM profile_identity_links WHERE origin=? AND profile_id=? AND actor!=?', (origin,snapshot['profile_id'],actor)).fetchone()
            if other:
                raise PublicWallError('profile_identity_already_linked')
            db.execute('INSERT INTO profile_identity_links VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(actor) DO UPDATE SET encrypted_token=excluded.encrypted_token,nickname=excluded.nickname,avatar=excluded.avatar,version=excluded.version,checked=excluded.checked,next_check=excluded.next_check,status=excluded.status',
                       (actor,origin,snapshot['profile_id'],encrypted,kind,snapshot['nickname'],avatar,snapshot['version'],now,now+60,'verified'))
        return {'linked':True,'origin':origin,'identity_id':origin+'#'+snapshot['profile_id'],
                'version':snapshot['version'],'checked_at':now,'status':'verified'}

    def mark_failure(self, actor, expected_token, *, revoked=False):
        with self.wall._connect() as db:
            db.execute('UPDATE profile_identity_links SET next_check=?,status=? WHERE actor=? AND encrypted_token=?',
                       (time.time()+300,'revoked' if revoked else 'offline',actor,expected_token))

    def due(self):
        if not self.ready():
            return None
        with self.wall._connect() as db:
            row=db.execute('SELECT * FROM profile_identity_links WHERE next_check<=? ORDER BY next_check LIMIT 1',(time.time(),)).fetchone()
        return dict(row) if row else None

    def detach(self, actor):
        self.require_ready()
        with self.wall._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import ready, TABLE
            prior = db.execute('SELECT origin FROM profile_identity_links WHERE actor=?', (actor,)).fetchone()
            if prior and ready(db):
                # Explicit detach is not a silent takeover by this wall.
                db.execute(f'INSERT INTO {TABLE}(actor,choice,origin,created) VALUES(?,?,?,?) '
                           'ON CONFLICT(actor) DO UPDATE SET choice=excluded.choice,origin=excluded.origin',
                           (actor, 'existing', prior['origin'], time.time()))
            db.execute('DELETE FROM profile_identity_links WHERE actor=?', (actor,))


def decode_code(value):
    from .remote_sites import normalize_social_origin
    if not isinstance(value,str) or not 60 <= len(value) <= 1500:
        raise PublicWallError('profile_ticket_invalid')
    try:
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
        body = json.loads(raw)
        if set(body) != {'v','origin','ticket'} or body['v'] != 1 or not TOKEN.fullmatch(body['ticket']):
            raise ValueError()
        return normalize_social_origin(body['origin']), body['ticket']
    except (ValueError, TypeError, KeyError):
        raise PublicWallError('profile_ticket_invalid') from None
