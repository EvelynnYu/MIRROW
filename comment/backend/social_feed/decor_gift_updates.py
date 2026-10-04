"""Mutable gift presentation, separate from immutable receipts and event facts."""
import json
from .decor_models import Exhibit
from .decor_store import DecorError


def ready(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gift_presentations'").fetchone())


def migrate(store, *, backup_ready=False):
    if backup_ready is not True:
        raise DecorError('decor_backup_required')
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS gift_presentations (
            actor TEXT NOT NULL, origin TEXT NOT NULL, gift_id TEXT NOT NULL,
            snapshot TEXT NOT NULL, version INTEGER NOT NULL,
            PRIMARY KEY(actor, origin, gift_id))''')


def apply_home(db, exhibits, selected, version):
    if not selected:
        return
    if not ready(db):
        raise DecorError('gift_updates_not_ready')
    by_id = {e['id']: e for e in exhibits}
    if any(eid not in by_id for eid in selected):
        raise DecorError('exhibit_not_found')
    # Every historic round of this exhibit, including inactive rounds. No new gift.
    for gift in db.execute('SELECT id,snapshot FROM gifts').fetchall():
        eid = json.loads(gift['snapshot'])['id']
        if eid in selected:
            db.execute('INSERT INTO gift_presentations VALUES(?,?,?,?,?) ON CONFLICT(actor,origin,gift_id) '
                       'DO UPDATE SET snapshot=excluded.snapshot,version=excluded.version',
                       ('@source','',gift['id'],json.dumps(by_id[eid],ensure_ascii=False),version))


def present(db, row):
    value = dict(row)
    value['snapshot'] = json.loads(value['snapshot']) if isinstance(value['snapshot'],str) else value['snapshot']
    value['presentation_version'] = 0
    if ready(db):
        actor = value.get('actor') if value.get('origin') else '@source'
        update = db.execute('SELECT snapshot,version FROM gift_presentations WHERE actor=? AND origin=? AND gift_id=?',
                            (actor,value.get('origin',''),value.get('gift_id',value['id']))).fetchone()
        if update:
            value.update(snapshot=json.loads(update['snapshot']), presentation_version=update['version'])
    return value


def import_update(db, actor, origin, gift_id, snapshot, version):
    if not version or not ready(db):
        return False
    if type(version) is not int or not 0 < version < 2**63:
        raise DecorError('invalid_gift_update')
    old = db.execute('SELECT snapshot FROM collections WHERE actor=? AND origin=? AND gift_id=?',
                     (actor,origin,gift_id)).fetchone()
    if not old or json.loads(old['snapshot'])['id'] != snapshot['id']:
        raise DecorError('invalid_gift_update')
    current = db.execute('SELECT version FROM gift_presentations WHERE actor=? AND origin=? AND gift_id=?',
                         (actor,origin,gift_id)).fetchone()
    if current and current['version'] >= version:
        return False
    db.execute('INSERT INTO gift_presentations VALUES(?,?,?,?,?) ON CONFLICT(actor,origin,gift_id) '
               'DO UPDATE SET snapshot=excluded.snapshot,version=excluded.version',
               (actor,origin,gift_id,json.dumps(Exhibit.model_validate(snapshot).model_dump(),ensure_ascii=False),version))
    return True


def for_actor(store, actor, gift_ids):
    ids = list(dict.fromkeys(gift_ids))[:50]
    if not ids:
        return []
    with store.connect() as db:
        rows = db.execute('SELECT * FROM collections WHERE actor=? AND origin=\'\' AND gift_id IN ('+
                          ','.join('?' for _ in ids)+')', [actor,*ids]).fetchall()
        return [present(db,row) for row in rows]
