"""Domain-owned decor, immutable gift snapshots and delivery outbox.

Stored separately from posts; actor identity remains owned by PublicWall and
reception.  No existing post, key or cognition table is migrated here.
"""
from __future__ import annotations
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from functools import lru_cache
from contextlib import contextmanager

from .decor_models import HomeDesign, PersonalDesign, SyncChoice


class DecorError(ValueError):
    pass


class DecorStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.media_dir = self.path.parent / 'social_decor_media'
        self._ready = False

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        if not self._ready:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS designs(actor TEXT PRIMARY KEY,body TEXT NOT NULL,revision INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,owner TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS gifts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,snapshot TEXT NOT NULL,active INTEGER NOT NULL,created REAL NOT NULL,request_id TEXT UNIQUE NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS active_gift_kind ON gifts(kind) WHERE active=1;
              CREATE TABLE IF NOT EXISTS collections(id TEXT PRIMARY KEY,actor TEXT NOT NULL,origin TEXT NOT NULL,gift_id TEXT NOT NULL,snapshot TEXT NOT NULL,received REAL NOT NULL,seen INTEGER NOT NULL DEFAULT 0,UNIQUE(actor,origin,gift_id));
              CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,fact TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS links(actor TEXT PRIMARY KEY,origin TEXT NOT NULL,subject TEXT NOT NULL,UNIQUE(origin,subject));
              CREATE TABLE IF NOT EXISTS proofs(token TEXT PRIMARY KEY,body TEXT NOT NULL,expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS sync_choices(actor TEXT PRIMARY KEY,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sync_jobs(actor TEXT NOT NULL,site_id TEXT NOT NULL,active INTEGER NOT NULL,generation TEXT NOT NULL,PRIMARY KEY(actor,site_id));
              CREATE TABLE IF NOT EXISTS sync_versions(actor TEXT PRIMARY KEY,origin TEXT NOT NULL,version INTEGER NOT NULL);
            ''')
            self._ready = True
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _human_notice_ready(db):
        """The optional human-view marker is never created during normal boot."""
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='human_gift_notice_seen'").fetchone())

    def migrate_human_gift_notices(self, *, backup_ready=False):
        """Explicit migration only; callers must confirm the decor DB backup first."""
        if backup_ready is not True:
            raise DecorError('decor_backup_required')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS human_gift_notice_seen (
                human_actor TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                seen REAL NOT NULL,
                PRIMARY KEY(human_actor, receipt_id)
            )''')

    def human_gift_notices(self, human, machine_actors, *, origin=None):
        """Unread machine receipts for one authenticated human, never machine seen state."""
        subjects = tuple(dict.fromkeys(actor for actor in machine_actors if isinstance(actor, str)))
        if not subjects:
            return []
        with self.connect() as db:
            if not self._human_notice_ready(db):
                return []
            placeholders = ','.join('?' for _ in subjects)
            clauses = [f'c.actor IN ({placeholders})', 's.receipt_id IS NULL']
            params = [*subjects]
            if origin is not None:
                clauses.append('c.origin=?')
                params.append(origin)
            rows = db.execute('SELECT c.id,c.actor,c.origin,c.gift_id,c.snapshot,c.received FROM collections c '
                              'LEFT JOIN human_gift_notice_seen s ON s.human_actor=? AND s.receipt_id=c.id '
                              'WHERE ' + ' AND '.join(clauses) + ' ORDER BY c.received ASC,c.id ASC LIMIT 50',
                              [human, *params]).fetchall()
            from .decor_gift_updates import present
            return [present(db,row) for row in rows]

    def see_human_gift_notices(self, human, receipt_ids, machine_actors):
        """Persist only markers for receipts that this human is currently allowed to inspect."""
        subjects = tuple(dict.fromkeys(actor for actor in machine_actors if isinstance(actor, str)))
        identifiers = tuple(dict.fromkeys(str(value) for value in receipt_ids if isinstance(value, str)))[:50]
        if not subjects or not identifiers:
            return 0
        with self.connect() as db:
            if not self._human_notice_ready(db):
                return 0
            subject_marks = ','.join('?' for _ in subjects)
            receipt_marks = ','.join('?' for _ in identifiers)
            permitted = db.execute(f'SELECT id FROM collections WHERE actor IN ({subject_marks}) '
                                   f'AND id IN ({receipt_marks})', [*subjects, *identifiers]).fetchall()
            now = time.time()
            db.executemany('INSERT OR IGNORE INTO human_gift_notice_seen(human_actor,receipt_id,seen) VALUES(?,?,?)',
                           [(human, row['id'], now) for row in permitted])
        return len(permitted)

    def home(self):
        with self.connect() as db:
            row = db.execute("SELECT body,revision FROM designs WHERE actor='@home'").fetchone()
        data = HomeDesign.model_validate(json.loads(row['body'])).model_dump() if row else HomeDesign().model_dump()
        data['revision'] = row['revision'] if row else 0
        return data

    def personal(self, actor):
        with self.connect() as db:
            row = db.execute('SELECT body FROM designs WHERE actor=?', (actor,)).fetchone()
        return json.loads(row['body']) if row else PersonalDesign().model_dump()

    def asset(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise DecorError('asset_not_found')
        return dict(row)

    def check_assets(self, data, actor):
        for field in ('background', 'card_background', 'music', 'image', 'gift_image', 'frame_asset', 'card_asset'):
            value = data.get(field)
            if value:
                asset = self.asset(value)
                if asset['owner'] != actor or (not asset['mime'].startswith('audio/' if field == 'music' else 'image/')):
                    raise DecorError('asset_not_owned')

    def save_home(self, design: HomeDesign, *, sync_gift_exhibits=()):
        data = design.model_dump()
        self.check_assets(data['theme'], 'aning')
        ids = []
        for exhibit in data['exhibits']:
            self.check_assets(exhibit, 'aning')
            exhibit['id'] = exhibit['id'] or uuid.uuid4().hex
            ids.append(exhibit['id'])
        if len(set(ids)) != len(ids):
            raise DecorError('duplicate_exhibit')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT revision FROM designs WHERE actor='@home'").fetchone()
            revision = row['revision'] if row else 0
            if revision != design.revision:
                raise DecorError('design_conflict')
            data['revision'] = revision + 1
            from .decor_gift_updates import apply_home
            apply_home(db, data['exhibits'], sync_gift_exhibits, revision + 1)
            db.execute('INSERT INTO designs VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET body=excluded.body,revision=excluded.revision',
                       ('@home', json.dumps(data, ensure_ascii=False), revision + 1))
            # Removing an exhibit stops future delivery; immutable receipts survive.
            for gift in db.execute('SELECT id,snapshot FROM gifts WHERE active=1').fetchall():
                if json.loads(gift['snapshot'])['id'] not in ids:
                    db.execute('UPDATE gifts SET active=0 WHERE id=?', (gift['id'],))
        return data

    def save_personal(self, actor, design: PersonalDesign):
        data = design.model_dump()
        self.check_assets(data, actor)
        with self.connect() as db:
            db.execute('INSERT INTO designs VALUES(?,?,1) ON CONFLICT(actor) DO UPDATE SET body=excluded.body,revision=designs.revision+1',
                       (actor, json.dumps(data)))
        return data

    def gifts(self):
        from .decor_gift_updates import present
        with self.connect() as db:
            return [present(db,row) for row in
                    db.execute('SELECT * FROM gifts WHERE active=1 ORDER BY kind')]

    def set_gift(self, kind, exhibit_id, request_id):
        exhibit = next((e for e in self.home()['exhibits'] if e['id'] == exhibit_id), None)
        if exhibit_id and not exhibit:
            raise DecorError('exhibit_not_found')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM gifts WHERE request_id=?', (request_id,)).fetchone():
                return
            db.execute('UPDATE gifts SET active=0 WHERE kind=?', (kind,))
            if exhibit:
                db.execute('INSERT INTO gifts VALUES(?,?,?,?,?,?)', (uuid.uuid4().hex, kind,
                    json.dumps(exhibit, ensure_ascii=False), 1, time.time(), request_id))

    def visit(self, actor, kind, name):
        """An authenticated visit issues only the visiting actor's gift."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            gift = db.execute('SELECT * FROM gifts WHERE active=1 AND kind=?', (kind,)).fetchone()
            if gift and actor not in {'aning', 'k'}:
                from .decor_gift_updates import present
                effective = present(db, gift)
                effective_snapshot = json.dumps(effective['snapshot'], ensure_ascii=False)
                identifier = uuid.uuid4().hex
                count = db.execute('INSERT OR IGNORE INTO collections(id,actor,origin,gift_id,snapshot,received) VALUES(?,?,?,?,?,?)',
                    (identifier, actor, '', gift['id'], effective_snapshot, time.time())).rowcount
                if count:
                    item = effective['snapshot']
                    fact = (f'收据时间：{datetime.now().astimezone().isoformat(timespec="seconds")}。{name}来到本家共域，自动收到了赠礼「{item["name"]}」。'
                            f'展品说明：{item["description"]}；站主留言：{item["human_note"]}；AI留言：{item["ai_note"]}。')
                    db.execute('INSERT INTO events VALUES(?,?,0,?)', ('gift_'+identifier, fact, time.time()))
        return self.collection(actor, pending=True)

    def collection(self, actor, *, pending=False, before: float | None = None):
        from .decor_gift_updates import present
        with self.connect() as db:
            rows = db.execute('SELECT * FROM collections WHERE actor=?' + (' AND seen=0' if pending else '') +
                (' AND received<?' if before else '') + ' ORDER BY received DESC,id DESC LIMIT 50',
                (actor, before) if before else (actor,)).fetchall()
            return [present(db,row) for row in rows]

    def seen(self, actor, receipt):
        with self.connect() as db:
            db.execute('UPDATE collections SET seen=1 WHERE actor=? AND id=?', (actor, receipt))

    def delivery_report(self, *, since=0.0, before_time=None, before_id='', limit=50):
        """Own-domain receipts only; imported gifts belong to another host."""
        limit = max(1, min(int(limit), 100))
        observed_at = time.time()
        with self.connect() as db:
            # The upper bound prevents receipts arriving during the model call
            # from being silently consumed by its persisted observation cursor.
            base = "FROM collections c JOIN gifts g ON g.id=c.gift_id WHERE c.origin='' AND c.received<=?"
            totals = db.execute('SELECT COUNT(*),COALESCE(SUM(c.received>?),0) ' + base,
                                (since, observed_at)).fetchone()
            where, params = '', [observed_at]
            if before_time is not None:
                where = ' AND (c.received<? OR (c.received=? AND c.id<?))'
                params.extend([before_time,before_time,before_id])
            rows = db.execute('SELECT c.id,c.actor,c.received,c.snapshot,g.kind ' + base + where +
                              ' ORDER BY c.received DESC,c.id DESC LIMIT ?', [*params,limit+1]).fetchall()
        items = [dict(row) | {'snapshot':json.loads(row['snapshot']), 'new':row['received']>since}
                 for row in rows[:limit]]
        return {'items':items,'total':totals[0],'new_count':totals[1], 'since':since,'observed_at':observed_at,
                'has_more':len(rows)>limit,
                'next_cursor':{'time':items[-1]['received'],'id':items[-1]['id']} if len(rows)>limit else None}

    def imported_gift(self, actor, origin, receipt, site_name):
        # The source peer supplies content only; local identity, deduplication and
        # event text are constructed here, never accepted as remote instructions.
        from .decor_models import Exhibit
        snapshot = Exhibit.model_validate(receipt['snapshot']).model_dump()
        gift_id = str(receipt['gift_id'])[:80]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            identifier = uuid.uuid4().hex
            inserted = db.execute('INSERT OR IGNORE INTO collections(id,actor,origin,gift_id,snapshot,received) VALUES(?,?,?,?,?,?)',
                (identifier, actor, origin, gift_id, json.dumps(snapshot, ensure_ascii=False), time.time())).rowcount
            if inserted:
                label = 'AI' if actor == 'k' else '站主'
                fact = f'本家收妥时间：{datetime.now().astimezone().isoformat(timespec="seconds")}。{label}在{site_name}的共域收到了赠礼「{snapshot["name"]}」。说明：{snapshot["description"]}；人类主人的留言：{snapshot["human_note"]}；机的留言：{snapshot["ai_note"]}。'
                db.execute('INSERT INTO events VALUES(?,?,0,?)', ('gift_'+identifier, fact, time.time()))
            from .decor_gift_updates import import_update
            import_update(db, actor, origin, gift_id, snapshot, receipt.get('presentation_version',0))

    def new_proof(self, body):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM proofs WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO proofs VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), json.dumps(body), time.time()+300))
        return token

    def proof(self, token):
        with self.connect() as db:
            row = db.execute('SELECT body FROM proofs WHERE token=? AND expires>?',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            raise DecorError('link_expired')
        return json.loads(row['body'])

    def link(self, actor):
        with self.connect() as db:
            row = db.execute('SELECT origin,subject FROM links WHERE actor=?', (actor,)).fetchone()
        return dict(row) if row else None

    def bind(self, actor, origin, subject):
        with self.connect() as db:
            try:
                old = db.execute('SELECT origin,subject FROM links WHERE actor=?',(actor,)).fetchone()
                if not old or old['origin']!=origin or old['subject']!=subject:
                    db.execute('DELETE FROM sync_versions WHERE actor=?',(actor,))
                db.execute('INSERT INTO links VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET origin=excluded.origin,subject=excluded.subject',
                           (actor, origin, subject))
            except sqlite3.IntegrityError:
                raise DecorError('home_identity_already_linked') from None

    def unlink(self, actor):
        with self.connect() as db:
            db.execute('DELETE FROM links WHERE actor=?', (actor,))
            db.execute('DELETE FROM sync_versions WHERE actor=?',(actor,))

    def save_synced(self, actor, origin, version, design):
        data=design.model_dump();self.check_assets(data,actor)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            link = db.execute('SELECT origin FROM links WHERE actor=?',(actor,)).fetchone()
            if not link or link['origin'] != origin:
                raise DecorError('home_link_required')
            row=db.execute('SELECT origin,version FROM sync_versions WHERE actor=?',(actor,)).fetchone()
            if row and row['origin']==origin and row['version']>=version:
                return False
            db.execute('INSERT INTO designs VALUES(?,?,1) ON CONFLICT(actor) DO UPDATE SET body=excluded.body,revision=designs.revision+1',(actor,json.dumps(data)))
            db.execute('INSERT INTO sync_versions VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET origin=excluded.origin,version=excluded.version',(actor,origin,version))
        return True

    def sync_jobs(self, actor):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT site_id,active,generation FROM sync_jobs WHERE actor=?',(actor,))]

    def queue_sync(self, actor, site_id, active):
        with self.connect() as db:
            db.execute('INSERT INTO sync_jobs VALUES(?,?,?,?) ON CONFLICT(actor,site_id) DO UPDATE SET active=excluded.active,generation=excluded.generation',(actor,site_id,int(active),uuid.uuid4().hex))

    def finish_sync(self, actor, site_id, generation):
        with self.connect() as db:
            db.execute('DELETE FROM sync_jobs WHERE actor=? AND site_id=? AND generation=?',(actor,site_id,generation))

    def choice(self, actor, value: SyncChoice | None = None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT INTO sync_choices VALUES(?,?) ON CONFLICT(actor) DO UPDATE SET body=excluded.body',
                           (actor, value.model_dump_json()))
            row = db.execute('SELECT body FROM sync_choices WHERE actor=?', (actor,)).fetchone()
        return json.loads(row['body']) if row else SyncChoice().model_dump()


@lru_cache(maxsize=32)
def _store_at(path):
    return DecorStore(path)


def get_decor_store():
    # Derive from the injected wall path so temporary-wall tests can never
    # accidentally initialize or append to the production decor database.
    from .public_wall import get_public_wall
    return _store_at(get_public_wall().path.with_name('social_decor.db'))
