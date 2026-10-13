"""One SQLite message authority, including Memory V2's source columns."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import time
from uuid import uuid4


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'conversation.db'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS local_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, active_date TEXT NOT NULL,
                    calendar_date TEXT NOT NULL, session_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    is_wander INTEGER NOT NULL DEFAULT 0, is_sentinel INTEGER NOT NULL DEFAULT 0,
                    is_reminder INTEGER NOT NULL DEFAULT 0, event_type TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS message_session ON conversation_messages(session_id,id);
                CREATE TABLE IF NOT EXISTS chat_requests(
                    request_id TEXT PRIMARY KEY, user_message_id TEXT NOT NULL,
                    assistant_message_id TEXT, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
            ''')
            # An interrupted provider call is not silently replayed on restart.
            db.execute("UPDATE chat_requests SET state='interrupted',error='process_restarted' WHERE state='running'")
        if not self.setting('session_id'):
            self.set_setting('session_id', f'session_{int(time.time()*1000)}_{uuid4().hex[:8]}')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM local_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.connection() as db:
            db.execute('INSERT INTO local_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, json.dumps(value, ensure_ascii=False)))

    @property
    def session_id(self):
        return self.setting('session_id')

    def profile(self):
        return self.setting('profile', {'human_name': '用户', 'agent_name': 'AI', 'persona': '', 'relationship_started_at': ''})

    def messages(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM conversation_messages WHERE session_id=? ORDER BY id', (self.session_id,))]

    def append(self, role, content, *, message_id=None, event_type='', wander=False, _db=None):
        if _db is None:
            with self.connection() as db:
                return self.append(role, content, message_id=message_id, event_type=event_type, wander=wander, _db=db)
        message_id = message_id or uuid4().hex
        timestamp = datetime.now().astimezone().isoformat()
        if _db is not None:
            db = _db
            existing = db.execute('SELECT * FROM conversation_messages WHERE message_id=?', (message_id,)).fetchone()
            if existing:
                if existing['role'] != role or existing['content'] != content:
                    raise ValueError('message_identity_conflict')
                return dict(existing)
            db.execute('''INSERT INTO conversation_messages(active_date,calendar_date,session_id,timestamp,
                role,content,message_id,is_wander,event_type) VALUES(?,?,?,?,?,?,?,?,?)''',
                (timestamp[:10], timestamp[:10], self.session_id, timestamp, role, content, message_id, int(wander), event_type))
            return dict(db.execute('SELECT * FROM conversation_messages WHERE message_id=?', (message_id,)).fetchone())

    def request(self, request_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM chat_requests WHERE request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def pending_or_failed_requests(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT request_id,state,error FROM chat_requests WHERE state!='complete'")]

    def begin_request(self, request_id, content):
        with self.connection() as db:
            self.append('user', content, message_id=request_id, _db=db)
            db.execute("INSERT INTO chat_requests VALUES(?,?,NULL,'running','')", (request_id, request_id))

    def finish_request(self, request_id, content):
        with self.connection() as db:
            message = self.append('assistant', content, message_id='reply_' + request_id, _db=db)
            db.execute("UPDATE chat_requests SET state='complete',assistant_message_id=? WHERE request_id=?",
                (message['message_id'], request_id))
        return message

    def fail_request(self, request_id):
        with self.connection() as db:
            db.execute("UPDATE chat_requests SET state='failed',error='generation_failed' WHERE request_id=?", (request_id,))

    def backup(self, destination: Path):
        # A consistent SQLite snapshot, not a copy of a live WAL file.
        if destination.exists():
            raise ValueError('backup_already_exists')
        with self.connection() as source, sqlite3.connect(destination) as target:
            source.backup(target)
