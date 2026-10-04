"""Explicit profile provenance; pending is not a verified profile or a new authority.

No schema/backfill on startup. Key, household and cognition stay independent.
"""
import time
import uuid

from .public_wall import PublicWallError


TABLE = 'profile_registration_sources'


def ready(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone())


def state_in(db, actor):
    if actor in {'aning', 'k'}:
        return {'state': 'local', 'can_interact': True, 'can_edit_profile': True}
    link = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_identity_links'").fetchone():
        link = db.execute('SELECT origin,status FROM profile_identity_links WHERE actor=?', (actor,)).fetchone()
    if link:
        return {'state': 'linked', 'origin': link['origin'], 'verification': link['status'],
                'can_interact': True, 'can_edit_profile': False}
    row = db.execute(f'SELECT choice,origin FROM {TABLE} WHERE actor=?', (actor,)).fetchone() if ready(db) else None
    pending = bool(row and row['choice'] == 'existing')
    return {'state': 'pending' if pending else 'local' if row else 'unconfirmed',
            'origin': row['origin'] if row else '', 'can_interact': not pending,
            'can_edit_profile': not pending}


def guard_in(db, actor):
    if not state_in(db, actor)['can_interact']:
        raise PublicWallError('profile_verification_pending')


def source_in(db, actor, choice, home_origin):
    """Initial selection only. Replaying enrollment cannot change an existing source."""
    if not ready(db):
        raise PublicWallError('profile_registration_schema_not_ready')
    if not isinstance(choice, dict) or set(choice) - {'choice', 'origin'}:
        raise PublicWallError('profile_source_required')
    mode, origin = choice.get('choice'), choice.get('origin', '')
    if mode not in {'local', 'existing'}:
        raise PublicWallError('profile_source_required')
    if actor in {'aning', 'k'}:
        raise PublicWallError('profile_home_is_source')
    if mode == 'existing':
        from .remote_sites import normalize_social_origin
        try:
            origin = normalize_social_origin(origin)
        except ValueError:
            raise PublicWallError('profile_source_invalid') from None
        if origin == home_origin:
            raise PublicWallError('profile_link_to_self')
    elif origin:
        raise PublicWallError('profile_source_invalid')
    current = db.execute(f'SELECT choice,origin FROM {TABLE} WHERE actor=?', (actor,)).fetchone()
    if current:
        if current['choice'] != mode or current['origin'] != origin:
            raise PublicWallError('profile_source_locked')
        return
    status = state_in(db, actor)
    if status['state'] == 'linked':
        if mode != 'existing' or status['origin'] != origin:
            raise PublicWallError('profile_source_locked')
    if mode == 'existing' and db.execute(
            'SELECT 1 FROM profile_identity_grants WHERE actor=? AND revoked IS NULL AND expires>? LIMIT 1',
            (actor, time.time())).fetchone():
        raise PublicWallError('profile_has_outgoing_grants')
    db.execute(f'INSERT INTO {TABLE}(actor,choice,origin,created) VALUES(?,?,?,?)',
               (actor, mode, origin, time.time()))
    if mode == 'local':
        db.execute('INSERT INTO profile_identity_ids(actor,profile_id) VALUES(?,?) ON CONFLICT(actor) DO NOTHING',
                   (actor, uuid.uuid4().hex))


def enroll_in(db, visitor_id, choice, home_origin):
    """Only new public enrollments must choose. Existing enrollment never rehomes."""
    if not ready(db):
        return  # Old adapters remain usable before the explicitly approved migration.
    if db.execute('SELECT 1 FROM wall_registered_names WHERE visitor_id=?', (visitor_id,)).fetchone():
        return
    source_in(db, 'visitor:' + visitor_id, choice, home_origin)


def guard_origin_in(db, actor, origin):
    if not ready(db):
        return
    row = db.execute(f'SELECT choice,origin FROM {TABLE} WHERE actor=?', (actor,)).fetchone()
    if row and (row['choice'] != 'existing' or row['origin'] != origin):
        raise PublicWallError('profile_source_locked')


class ProfileRegistrationStore:
    def __init__(self, wall):
        self.wall = wall

    def ready(self):
        with self.wall._connect() as db:
            return ready(db)

    def migrate(self, *, backup_ready=False):
        if backup_ready is not True:
            raise PublicWallError('profile_backup_required')
        from .profile_identity import ProfileIdentityStore
        ProfileIdentityStore(self.wall).require_ready()
        with self.wall._connect() as db:
            db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                actor TEXT PRIMARY KEY, choice TEXT NOT NULL CHECK(choice IN ('local','existing')),
                origin TEXT NOT NULL DEFAULT '', created REAL NOT NULL)''')

    def state(self, actor):
        with self.wall._connect() as db:
            return state_in(db, actor)

    def guard(self, actor):
        with self.wall._connect() as db:
            guard_in(db, actor)

    def select(self, actor, choice, home_origin):
        with self.wall._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source_in(db, actor, choice, home_origin)
        return self.state(actor)
