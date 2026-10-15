"""One SQLite message authority, including Memory V2's source columns."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import time
from uuid import uuid4
from mirrow_core.model_provenance import normalize_model_label


class Store:
    def __init__(self, directory: Path):
        self.active_date_provider = None
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
            columns = {row['name'] for row in db.execute('PRAGMA table_info(conversation_messages)')}
            if 'tool_calls' not in columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN tool_calls TEXT NOT NULL DEFAULT '[]'")
            if 'generation_model' not in columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN generation_model TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TRIGGER IF NOT EXISTS generation_model_immutable
                BEFORE UPDATE OF generation_model ON conversation_messages
                WHEN NEW.generation_model != OLD.generation_model
                BEGIN SELECT RAISE(ABORT, 'generation_model_immutable'); END''')
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

    def append(self, role, content, *, message_id=None, event_type='', wander=False, sentinel=False, active_date=None, generation_model='', _db=None):
        if active_date is None:
            active_date = self.active_date_provider() if self.active_date_provider else datetime.now().astimezone().date().isoformat()
        if _db is None:
            with self.connection() as db:
                return self.append(role, content, message_id=message_id, event_type=event_type, wander=wander, sentinel=sentinel, active_date=active_date, generation_model=generation_model, _db=db)
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
                role,content,message_id,is_wander,is_sentinel,event_type,generation_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (active_date, timestamp[:10], self.session_id, timestamp, role, content, message_id, int(wander), int(sentinel), event_type,
                 normalize_model_label(generation_model) if role == 'assistant' else ''))
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

    def finish_request(self, request_id, content, tool_calls=None, *, generation_model=''):
        with self.connection() as db:
            message = self.append('assistant', content, message_id='reply_' + request_id, generation_model=generation_model, _db=db)
            db.execute('UPDATE conversation_messages SET tool_calls=? WHERE message_id=?',
                (json.dumps(tool_calls or [], ensure_ascii=False), message['message_id']))
            db.execute("UPDATE chat_requests SET state='complete',assistant_message_id=? WHERE request_id=?",
                (message['message_id'], request_id))
        return message

    def switch_model(self, model, previous):
        """Atomic setting plus source event; caller owns the foreground lock."""
        day = self.active_date_provider()
        with self.connection() as db:
            current = db.execute("SELECT value FROM local_settings WHERE key='selected_model'").fetchone()
            old = json.loads(current['value']) if current else previous
            if old == model:
                return False
            db.execute("INSERT INTO local_settings VALUES('selected_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(model),))
            before, after = normalize_model_label(old) or '未知', normalize_model_label(model) or '未知'
            self.append('event', f'用户将主聊天模型从 {before} 切换为 {after}。后台任务使用各自配置，已经开始的调用保留原模型。',
                        event_type='model_switch', active_date=day, _db=db)
            return True

    def fail_request(self, request_id):
        with self.connection() as db:
            db.execute("UPDATE chat_requests SET state='failed',error='generation_failed' WHERE request_id=?", (request_id,))

    def backup(self, destination: Path):
        # A consistent SQLite snapshot, not a copy of a live WAL file.
        if destination.exists():
            raise ValueError('backup_already_exists')
        with self.connection() as source, sqlite3.connect(destination) as target:
            source.backup(target)
