"""Local SQLite example for messages, music notes and projected events."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from music_system.store import now_iso

DB = Path(__file__).parent / 'data' / 'host.sqlite'

@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, session TEXT, body TEXT, tool_calls TEXT);
        CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT);''')
    try:
        with db: yield db
    finally:
        db.close()

class LocalChronicle:
    def add(self, body='', calls=None, session='standalone'):
        mid = uuid.uuid4().hex
        with connect() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?)', (mid, session, body, json.dumps(calls or [], ensure_ascii=False)))
        return mid

    def find_tool_attachment_messages(self, session, generation_id):
        with connect() as db:
            rows = db.execute('SELECT id AS message_id,tool_calls FROM messages WHERE session=?', (session,)).fetchall()
        return [dict(row) for row in rows if any(
            (call.get('extra_data') or {}).get('generation_id') == generation_id
            for call in json.loads(row['tool_calls']))]

    def update_message_tool_calls(self, mid, calls):
        with connect() as db:
            return db.execute('UPDATE messages SET tool_calls=? WHERE id=?', (calls, mid)).rowcount == 1

    def get_message_by_msg_id(self, mid):
        with connect() as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def get_topic_state(self, _session): return {'is_active': False}

    def messages(self):
        with connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM messages ORDER BY rowid')]
        for row in rows:
            row['role'] = 'assistant' if any(call.get('tool') for call in json.loads(row['tool_calls'])) else 'user'
        return rows

class LocalSessionProjection:
    async def update_message_tool_calls(self, session, mid, calls):
        # Standalone uses one SQLite authority; a full host can also update its UI cache.
        return chronicle.update_message_tool_calls(mid, json.dumps(calls, ensure_ascii=False))

chronicle = LocalChronicle()
projection = LocalSessionProjection()

async def write_event(fact):
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO events VALUES(?,?)', (fact['event_id'], json.dumps(fact, ensure_ascii=False)))
    return True

def record_note(payload, *, source_id=None, source='manual'):
    item = {'id': source_id or uuid.uuid4().hex, 'title':'', 'artist':'', 'subject_id':'k',
            'reaction':'', 'dimensions':{}, 'mode':'manual', **payload, 'source':source,
            'created_at':now_iso()}
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO notes VALUES(?,?)', (item['id'], json.dumps(item, ensure_ascii=False)))
        row = db.execute('SELECT payload FROM notes WHERE id=?', (item['id'],)).fetchone()
    return json.loads(row[0])

def notes():
    with connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM notes ORDER BY rowid DESC')]
