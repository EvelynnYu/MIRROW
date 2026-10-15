"""Persistent day boundaries, jobs and receipts beside the message authority."""
from datetime import datetime
import hashlib
import json

STAGES = ('memory', 'cognition', 'diary')


def source_digest(rows):
    source = [{key: row.get(key) for key in ('message_id', 'role', 'content', 'timestamp', 'active_date', 'event_type', 'tool_calls')}
              for row in rows]
    from mirrow_core.model_provenance import row_model
    for item, row in zip(source, rows):
        if row_model(row):
            item['generation_model'] = row_model(row)
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class DayStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS local_days(
                    date TEXT PRIMARY KEY, state TEXT NOT NULL, opened_at TEXT NOT NULL,
                    closed_at TEXT, reason TEXT, source_digest TEXT, last_message_id INTEGER);
                CREATE TABLE IF NOT EXISTS local_day_jobs(
                    date TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                    PRIMARY KEY(date,stage));
                CREATE TABLE IF NOT EXISTS local_status_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
                    active_date TEXT, status TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_module_events(
                    id TEXT PRIMARY KEY, module TEXT NOT NULL, active_date TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            db.execute("UPDATE local_day_jobs SET state='pending' WHERE state='running'")

    def get(self, day):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM local_days WHERE date=?', (day,)).fetchone()
        return dict(row) if row else None

    def active(self):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM local_days WHERE state='open' ORDER BY date DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def open(self, day, now):
        existing = self.get(day)
        if existing and existing['state'] != 'open':
            raise ValueError('day_already_closed')
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO local_days(date,state,opened_at) VALUES(?,'open',?)", (day, now.isoformat()))

    def rows(self, day):
        return [row for row in self.store.messages() if row['active_date'] == day]

    def close(self, day, reason, now):
        rows = self.rows(day)
        with self.store.connection() as db:
            changed = db.execute("UPDATE local_days SET state='closed',closed_at=?,reason=?,source_digest=?,last_message_id=? WHERE date=? AND state='open'",
                (now.isoformat(), reason, source_digest(rows), max((r['id'] for r in rows), default=0), day)).rowcount
            if changed:
                for stage in STAGES:
                    db.execute("INSERT OR IGNORE INTO local_day_jobs(date,stage,state,updated_at) VALUES(?,?,'pending',?)", (day, stage, now.isoformat()))
        return bool(changed)

    def verify(self, day):
        record = self.get(day)
        rows = self.rows(day)
        if not record or record['state'] != 'closed' or source_digest(rows) != record['source_digest']:
            raise ValueError('closed_day_source_changed')
        return rows

    def result(self, day, stage):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM local_day_jobs WHERE date=? AND stage=?', (day, stage)).fetchone()
        return {**dict(row), 'result': json.loads(row['result'])} if row else None

    def update(self, day, stage, state, result=None):
        with self.store.connection() as db:
            db.execute('UPDATE local_day_jobs SET state=?,result=?,updated_at=? WHERE date=? AND stage=?',
                (state, json.dumps(result or {}, ensure_ascii=False), datetime.now().astimezone().isoformat(), day, stage))

    def list(self):
        with self.store.connection() as db:
            days = [dict(row) for row in db.execute('SELECT * FROM local_days ORDER BY date DESC')]
        for day in days:
            day['jobs'] = [self.result(day['date'], stage) for stage in STAGES]
        return days

    def register_event(self, event_id, module, day, occurred_at, payload):
        # Host-only hook: callers pass confirmed, attributed facts, never credentials.
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.store.connection() as db:
            prior = db.execute('SELECT * FROM local_module_events WHERE id=?', (event_id,)).fetchone()
            if prior:
                if (prior['module'], prior['active_date'], prior['occurred_at'], prior['payload']) != (module, day, occurred_at, body):
                    raise ValueError('module_event_identity_conflict')
                return
            record = self.get(day)
            if record and record['state'] == 'closed':
                raise ValueError('event_for_closed_day')
            db.execute('INSERT INTO local_module_events VALUES(?,?,?,?,?)', (event_id, module, day, occurred_at, body))

    def events(self, day):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM local_module_events WHERE active_date=? ORDER BY occurred_at,id', (day,)).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]
