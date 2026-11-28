"""Bounded, claim-before-send Xiaohongshu direct messages.

This domain deliberately does not share the comment ledger.  A direct-message
reply is tied to the newest consecutive inbound run in one verified personal
conversation.  The inbox badge is only a discovery hint: once observed, a
conversation remains pending here until AI has replied or chosen to leave it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xhs_interactions.native import InteractionBlocked, box
from .dm_navigation import DirectChatNavigation
from xhs_publish.store import ROOT, account_config, now
from wander_manager.xhs_profile_navigation import identity, normalized


DB = ROOT / 'events' / 'xhs_direct_messages.db'


def _digest(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _entity_name(entity_id: str) -> str:
    """Resolve a bound other-book identity only after stable-ID verification."""
    if entity_id == 'human':
        return '人类伙伴'
    if not entity_id:
        return ''
    try:
        from cognition.other_book import entities
        return str((entities().get(entity_id) or {}).get('name') or '')
    except Exception:
        return ''


def _recognition(source: dict[str, Any]) -> dict[str, str]:
    binding = source.get('identity_binding') or {}
    name = str(source.get('name') or '')
    entity = str(binding.get('entity_name') or _entity_name(str(binding.get('entity_id') or '')))
    if binding.get('binding_status') == 'bound' and entity:
        return {'身份': '本机已确认绑定', '人物': entity, '小红书昵称': name}
    return {'身份': '仅本次准入，未确认人物绑定', '小红书昵称': name}


def _rows(root):
    return [{**node.attrib, 'box': box(node), 'node': node} for node in root.iter('node')]


def _descendants(row):
    return [{**node.attrib, 'box': box(node), 'node': node} for node in row['node'].iter('node')]


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def conversation_rows(root) -> list[dict[str, Any]]:
    """Extract only observed personal conversation rows from the messages list."""
    found = []
    for recycler in root.iter('node'):
        if recycler.get('class') != 'androidx.recyclerview.widget.RecyclerView':
            continue
        for node in list(recycler):
            bounds = box(node)
            if (node.get('class') != 'android.view.ViewGroup' or node.get('clickable') != 'true'
                    or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
                continue
            descendants = _descendants({'node': node})
            avatars = [item for item in descendants if item.get('class') == 'android.widget.ImageView'
                       and item.get('clickable') == 'true' and item['box'][0] < bounds[0] + (bounds[2] - bounds[0]) * .38]
            text_nodes = [item for item in descendants if item.get('class') == 'android.widget.TextView' and item.get('text', '').strip()]
            if len(avatars) != 1 or not text_nodes:
                continue
            avatar = avatars[0]
            labels = [item for item in text_nodes if item['box'][0] >= avatar['box'][2]
                      and item['box'][0] < bounds[0]+(bounds[2]-bounds[0])*.65]
            labels.sort(key=lambda item: item['box'][1])
            if not labels:
                continue
            name = labels[0]['text'].strip()
            count_match = re.search(r'(?:[，, ]|^)(\d+)条未读(?:[，, ]|$)', node.get('content-desc',''))
            bubble_counts = [int(item.get('text', '').rstrip('+')) for item in text_nodes
                         if item.get('text','').rstrip('+').isdigit() and
                         item['box'][0] < avatar['box'][2]+(avatar['box'][2]-avatar['box'][0])*.4
                         and item['box'][1] < avatar['box'][1]+(avatar['box'][3]-avatar['box'][1])*.5]
            unread_count = int(count_match.group(1)) if count_match else (bubble_counts[0] if bubble_counts else 0)
            unread = unread_count > 0
            preview = labels[1]['text'].strip() if len(labels)>1 else ''
            found.append({'name': name, 'preview': preview[:300], 'unread': unread, 'unread_count': unread_count,
                          'row': {'box': bounds}, 'avatar': {'box': avatar['box']}})
    return [item for item in found if sum(normalized(r['name']) == normalized(item['name']) for r in found) == 1]


def record_visible_unread(root, session, account, *, store=None) -> int:
    """Persist the native message-page badges without opening any conversation.

    The inbox and the normal XHS reader share this exact provisional identity,
    so a badge noticed while AI is browsing can be resumed after that badge has
    disappeared.  `root` must be a live, account-verified message-page tree.
    """
    if not session or not isinstance(account, dict) or not account.get('account_id'):
        raise ValueError('dm_visible_unread_identity_required')
    store = store or DirectMessageStore()
    count = 0
    for item in conversation_rows(root):
        if not item['unread']:
            continue
        thread = _digest(['dm-inbox', account['account_id'], normalized(item['name'])])
        store.put_thread(session, thread, item['name'])
        store.mark_pending(session, thread, item['name'], item.get('unread_count', 1))
        count += 1
    return count


def message_rows(root, *, width: int = 1080) -> list[dict[str, Any]]:
    """Return text-only bubbles, retaining direction and a stable visible order."""
    values = []
    for recycler in root.iter('node'):
        if recycler.get('class') != 'androidx.recyclerview.widget.RecyclerView':
            continue
        for node in list(recycler):
            if node.get('class') != 'android.widget.LinearLayout':
                continue
            frames = [c for c in node if c.get('class')=='android.widget.FrameLayout']
            if not frames:
                continue
            clock = next((c.get('text','') for c in node if c.get('class')=='android.widget.TextView'), '')
            children = _descendants({'node':frames[0]})
            avatars = [item for item in children if item.get('class')=='android.widget.RelativeLayout'
                       and 0 < item['box'][2] - item['box'][0] < width * .3]
            if len(avatars) != 1:
                values.append({'direction':'unknown','text':'','clock':clock})
                continue
            avatar = avatars[0]
            direction = 'incoming' if sum(avatar['box'][::2]) / 2 < width/2 else 'outgoing'
            texts = [c for c in children if c.get('class')=='android.widget.TextView' and c.get('text','').strip()]
            images = [c for c in children if c.get('class')=='android.widget.ImageView'
                      and not _contains(avatar['box'],c['box'])]
            wording = texts[0]['text'] if len(texts)==1 and not images else ''
            values.append({'direction':direction,'text':wording,'clock':clock,'avatar':avatar['box']})
    return values


class DirectMessageStore:
    def __init__(self, path=None):
        self.path = Path(path or DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS dm_sources (id TEXT PRIMARY KEY, session TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS dm_replies (source TEXT PRIMARY KEY, session TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS dm_threads (thread TEXT NOT NULL, session TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(thread,session))')
            db.execute('CREATE TABLE IF NOT EXISTS dm_pending (thread TEXT NOT NULL, session TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(thread,session))')
            db.execute('CREATE TABLE IF NOT EXISTS dm_contacts (profile_id TEXT PRIMARY KEY, record TEXT NOT NULL)')

            # Before batch admission existed, a pending row could carry a raw source.
            # Preserve the unread count and reverify the account before delivery.
            legacy = db.execute("SELECT rowid,record FROM dm_pending WHERE json_extract(record,'$.state') != 'completed' AND json_extract(record,'$.batch_token') IS NULL").fetchall()
            for rowid, raw in legacy:
                row = json.loads(raw)
                row.update(state='pending', admission='review', source='', review_source='', profile_id='',
                           batch_token=secrets.token_urlsafe(18))
                db.execute('UPDATE dm_pending SET record=? WHERE rowid=?', (json.dumps(row, ensure_ascii=False), rowid))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_source(self, session, source):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO dm_sources VALUES (?,?,?)', (source['source'], session, json.dumps(source, ensure_ascii=False)))
            row = db.execute('SELECT record FROM dm_sources WHERE id=? AND session=?', (source['source'], session)).fetchone()
        if not row:
            raise ValueError('dm_source_owner_changed')
        return json.loads(row[0])

    def source(self, session, source):
        with self.connect() as db:
            row = db.execute('SELECT record FROM dm_sources WHERE id=? AND session=?', (source, session)).fetchone()
        return json.loads(row[0]) if row else None

    def put_thread(self, session, thread, name):
        with self.connect() as db:
            db.execute('INSERT INTO dm_threads VALUES (?,?,?) ON CONFLICT(thread,session) DO UPDATE SET name=excluded.name',
                       (thread, session, name))

    def thread_name(self, session, thread):
        with self.connect() as db:
            row = db.execute('SELECT name FROM dm_threads WHERE thread=? AND session=?', (thread, session)).fetchone()
        return str(row[0]) if row else ''

    def mark_pending(self, session, thread, name, unread_count):
        unread_count = max(1, int(unread_count or 1))
        value = {'thread': thread, 'name': name, 'state': 'pending', 'admission': 'review', 'batch_token': secrets.token_urlsafe(18), 'observed_at': now(),
                 'updated_at': now(), 'source': '', 'unread_count': unread_count,
                 'deferred_unread_count': 0}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (thread, session)).fetchone()
            if row:
                old = json.loads(row[0])
                # A completed conversation becomes pending again only when the
                # native inbox supplies a candidate.  Badge counts are not an
                # identity: even the same count may be a distinct later note.
                if old['state'] == 'completed':
                    if old.get('admission') == 'rejected':
                        value['previous_rejected_source'] = old.get('review_source') or old.get('source', '')
                    db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                               (json.dumps(value, ensure_ascii=False), thread, session))
                    return value
                if old['state'] == 'pending' and not old.get('review_source'):
                    old['unread_count'] = max(int(old.get('unread_count', 1) or 1), unread_count)
                if old['state'] == 'reading' or old.get('review_source'):
                    # A later badge belongs after the frozen material currently
                    # being considered.  Do not replace its source or count it
                    # twice on repeated scans of the same badge.
                    old['deferred_unread_count'] = max(old.get('deferred_unread_count', 0), unread_count)
                old.update(name=name, updated_at=now())
                db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                           (json.dumps(old, ensure_ascii=False), thread, session))
                return old
            db.execute('INSERT INTO dm_pending VALUES (?,?,?)', (thread, session, json.dumps(value, ensure_ascii=False)))
        return value

    def pending(self, session):
        with self.connect() as db:
            rows = db.execute('SELECT record FROM dm_pending WHERE session=? ORDER BY rowid', (session,)).fetchall()
        return [json.loads(row[0]) for row in rows if json.loads(row[0]).get('state') != 'completed']

    def interactive_pending(self, session):
        return [row for row in self.pending(session) if row.get('admission') == 'approved'
                and row.get('review_source') and not self.is_blocked(row.get('profile_id'))]

    def review_pending(self, session):
        return [row for row in self.pending(session) if row.get('admission') != 'approved']

    def admin_pending(self):
        with self.connect() as db:
            rows = db.execute('SELECT session,record FROM dm_pending ORDER BY rowid DESC').fetchall()
        value = []
        for session, record in rows:
            row = json.loads(record)
            if row.get('state') != 'completed' and row.get('admission') == 'review':
                value.append({'session': session, 'thread': row.get('thread',''), 'name': row.get('name',''),
                              'batch_token': row.get('batch_token',''), 'unread_count': row.get('unread_count',0),
                              'observed_at': row.get('observed_at',''), 'updated_at': row.get('updated_at','')})
        return value

    def approve_batch(self, session, thread, batch_token):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (thread, session)).fetchone()
            if not row:
                raise ValueError('dm_pending_not_found')
            value = json.loads(row[0])
            if not secrets.compare_digest(str(value.get('batch_token','')), str(batch_token or '')):
                raise ValueError('dm_batch_changed')
            if not value.get('review_source'):
                raise ValueError('dm_preview_required')
            if not batch_token or value.get('admission')!='review' or value.get('state') != 'pending':
                raise ValueError('dm_pending_not_actionable')
            value.update(admission='approved', updated_at=now())
            db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                       (json.dumps(value, ensure_ascii=False), thread, session))
        return value

    def freeze_preview(self, session, thread, batch_token, source):
        """A local-owner preview freezes a source but never marks it delivered to AI."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (thread, session)).fetchone()
            if not row:
                raise ValueError('dm_pending_not_found')
            value = json.loads(row[0])
            if value.get('admission') == 'approved' or not secrets.compare_digest(str(value.get('batch_token','')), str(batch_token or '')):
                raise ValueError('dm_batch_changed')
            if (value.get('state') != 'pending' or not batch_token
                    or value.get('review_source') not in {None,'',source['source']}):
                raise ValueError('dm_batch_changed')
            contact = {'state':'known','entity_id':'','name':source.get('name',''),'updated_at':now()}
            db.execute('INSERT OR IGNORE INTO dm_contacts VALUES (?,?)', (source['thread'],json.dumps(contact, ensure_ascii=False)))
            contact = json.loads(db.execute('SELECT record FROM dm_contacts WHERE profile_id=?',(source['thread'],)).fetchone()[0])
            if contact.get('state') == 'blocked':
                value.update(state='completed',admission='blocked')
            value.update(review_source=source['source'], profile_id=source['thread'], previewed_at=now(), updated_at=now())
            db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                       (json.dumps(value, ensure_ascii=False), thread, session))
        return value

    def promote_preview(self, session, thread, token, source):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_sources WHERE id=? AND session=?', (source, session)).fetchone()
            if not row:
                raise ValueError('dm_source_not_read')
            value = json.loads(row[0])
            pending = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (thread,session)).fetchone()
            batch = json.loads(pending[0]) if pending else {}
            if (batch.get('state') not in {'pending','reading'} or batch.get('admission') != 'approved'
                    or not token or token != batch.get('batch_token') or source != batch.get('review_source')
                    or value.get('thread') != batch.get('profile_id')):
                raise ValueError('dm_batch_changed')
            profile_id = value.get('thread','')
            contact_row = db.execute('SELECT record FROM dm_contacts WHERE profile_id=?', (profile_id,)).fetchone()
            contact = json.loads(contact_row[0]) if contact_row else {}
            from xhs_interactions.native import config as interaction_config
            owner = bool(profile_id and profile_id == interaction_config().get('owner_account_id'))
            if contact.get('state') == 'blocked':
                raise ValueError('dm_contact_blocked')
            batch.update(state='reading', source=source, updated_at=now())
            db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?', (json.dumps(batch, ensure_ascii=False), thread, session))
            if (value.get('identity_binding') or {}).get('delivered_at'):
                return value
            entity_id = 'human' if owner else contact.get('entity_id','')
            value['identity_binding'] = {'profile_id': profile_id, 'entity_id':entity_id,
                'entity_name':_entity_name(entity_id), 'binding_status':'bound' if owner else contact.get('state','unbound'),
                'active_date':__import__('conversation_manager_v2').resolve_active_date(session), 'delivered_at':now()}
            value.pop('owner_preview', None); value.pop('previewed_at', None)
            db.execute('UPDATE dm_sources SET record=? WHERE id=?', (json.dumps(value, ensure_ascii=False), source))
        return value

    def reject_batch(self, session, thread, batch_token):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (thread, session)).fetchone()
            if not row:
                raise ValueError('dm_pending_not_found')
            value = json.loads(row[0])
            if not secrets.compare_digest(str(value.get('batch_token','')), str(batch_token or '')):
                raise ValueError('dm_batch_changed')
            if value.get('state') != 'pending' or value.get('admission') != 'review':
                raise ValueError('dm_pending_not_actionable')
            if value.get('deferred_unread_count') and value.get('review_source'):
                prior = db.execute('SELECT record FROM dm_sources WHERE session=? AND id=?',
                                   (session,value['review_source'])).fetchone()
                value.update(state='pending', admission='review', batch_token=secrets.token_urlsafe(18),
                             previous_rejected_source=value['review_source'], unread_count=value['deferred_unread_count'],
                             deferred_unread_count=0, seen_messages=json.loads(prior[0]).get('incoming_messages',[]) if prior else [],
                             review_source='',source='',profile_id='',observed_at=now(),updated_at=now())
            else:
                value.update(state='completed', admission='rejected', updated_at=now())
            db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                       (json.dumps(value, ensure_ascii=False), thread, session))
        return value

    def contacts(self):
        with self.connect() as db:
            rows = db.execute('SELECT profile_id,record FROM dm_contacts ORDER BY profile_id').fetchall()
        from xhs_interactions.native import config
        owner = config().get('owner_account_id')
        return [{'profile_id': row[0], **json.loads(row[1]), 'is_owner': bool(owner and row[0] == owner)} for row in rows]

    def contact(self, profile_id):
        with self.connect() as db:
            row = db.execute('SELECT record FROM dm_contacts WHERE profile_id=?', (profile_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def is_blocked(self, profile_id):
        return bool((self.contact(profile_id) or {}).get('state') == 'blocked')

    def bind_contact(self, profile_id, *, entity_id='', name=''):
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError('dm_profile_identity_required')
        if not self.has_verified_profile(profile_id):
            raise ValueError('dm_profile_identity_unverified')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT record FROM dm_contacts WHERE profile_id=?', (profile_id,)).fetchone()
            old = json.loads(found[0]) if found else {}
            # Binding is descriptive; it must never silently lift a local block.
            state = 'blocked' if old.get('state') == 'blocked' else ('bound' if entity_id else 'known')
            value = {'entity_id': entity_id, 'name': name or old.get('name',''), 'state': state,
                     'updated_at': now(), 'block_sync': old.get('block_sync','') if state == 'blocked' else ''}
            db.execute('INSERT INTO dm_contacts VALUES (?,?) ON CONFLICT(profile_id) DO UPDATE SET record=excluded.record',
                       (profile_id, json.dumps(value, ensure_ascii=False)))
        return {'profile_id': profile_id, **value}

    def has_verified_profile(self, profile_id):
        """Only a native profile visit recorded in a source can authorize binding."""
        with self.connect() as db:
            rows = db.execute('SELECT record FROM dm_sources').fetchall()
        return any(json.loads(row[0]).get('thread') == profile_id for row in rows)

    def block_contact(self, profile_id, *, name=''):
        if not profile_id or not self.has_verified_profile(profile_id):
            raise ValueError('dm_profile_identity_unverified')
        old = self.contact(profile_id) or {}
        value = {**old, 'entity_id': old.get('entity_id',''), 'name': name or old.get('name',''), 'state':'blocked', 'block_sync':'pending', 'updated_at':now()}
        with self.connect() as db:
            db.execute('INSERT INTO dm_contacts VALUES (?,?) ON CONFLICT(profile_id) DO UPDATE SET record=excluded.record',
                       (profile_id, json.dumps(value, ensure_ascii=False)))
            db.execute("UPDATE dm_pending SET record=json_set(record,'$.state','completed','$.admission','blocked') WHERE json_extract(record,'$.profile_id')=?", (profile_id,))
        return {'profile_id': profile_id, **value}

    def set_block_sync(self, profile_id, status):
        if status not in {'verified','pending'}:
            raise ValueError('dm_block_sync_invalid')
        value = self.contact(profile_id)
        if not value or value.get('state') != 'blocked':
            raise ValueError('dm_contact_not_blocked')
        value.update(block_sync=status, updated_at=now())
        with self.connect() as db:
            db.execute('UPDATE dm_contacts SET record=? WHERE profile_id=?', (json.dumps(value, ensure_ascii=False), profile_id))
        return {'profile_id':profile_id, **value}

    def complete_source(self, session, source):
        value = self.source(session, source)
        if not value:
            raise ValueError('dm_source_not_read')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pending_thread = value.get('inbox_thread') or value['thread']
            row = db.execute('SELECT record FROM dm_pending WHERE thread=? AND session=?', (pending_thread, session)).fetchone()
            if not row:
                return
            pending = json.loads(row[0])
            # A new batch has no active source until it is read.  An old leave
            # or delayed reconciliation must not complete that later batch.
            if pending.get('source') != source:
                return
            incomplete = max(0, int(value.get('unread_remaining', 0) or 0))
            deferred = max(0, int(pending.get('deferred_unread_count', 0) or 0))
            if incomplete or deferred:
                pending.update(state='pending', source='', admission='review', review_source='', profile_id='',
                               batch_token=secrets.token_urlsafe(18), unread_count=max(incomplete, deferred),
                               deferred_unread_count=0,
                               seen_messages=list(value.get('incoming_messages') or []), updated_at=now())
            else:
                pending.update(state='completed', source=source, updated_at=now())
            db.execute('UPDATE dm_pending SET record=? WHERE thread=? AND session=?',
                       (json.dumps(pending, ensure_ascii=False), pending_thread, session))

    def reply(self, session, source):
        with self.connect() as db:
            row = db.execute('SELECT record FROM dm_replies WHERE source=? AND session=?', (source, session)).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self, session, source, text):
        if not isinstance(text, str) or not text.strip() or len(text)>10000:
            raise ValueError('dm_reply_text_invalid')
        source_record = self.source(session, source)
        if not source_record:
            raise ValueError('dm_source_not_read')
        if not (source_record.get('identity_binding') or {}).get('delivered_at'):
            raise ValueError('dm_source_not_admitted')
        contact = self.contact(source_record.get('thread', ''))
        if contact and contact.get('state') == 'blocked':
            raise ValueError('dm_contact_blocked')
        value = {'source': source, 'session': session, 'text': text, 'state': 'preparing',
                 'created_at': now(), 'updated_at': now(), 'receipt': {}, 'error': ''}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_replies WHERE source=?', (source,)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing['session'] != session or existing['text'] != text:
                    raise ValueError('dm_reply_already_claimed')
                return existing
            db.execute('INSERT INTO dm_replies VALUES (?,?,?,?)', (source, session, 'preparing', json.dumps(value, ensure_ascii=False)))
        return value

    def transition(self, session, source, expected, state, **updates):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM dm_replies WHERE source=? AND session=?', (source, session)).fetchone()
            if not row:
                raise ValueError('dm_reply_not_found')
            value = json.loads(row[0])
            if value['state'] not in set(expected):
                raise ValueError('dm_reply_state_conflict')
            value.update(updates, state=state, updated_at=now())
            db.execute('UPDATE dm_replies SET state=?,record=? WHERE source=?', (state, json.dumps(value, ensure_ascii=False), source))
        return value


class DirectMessagesPhone(DirectChatNavigation):
    """Native layout adapter. Every profile and newest-message check is repeated."""
    def _chat_header(self, name):
        return (len([r for r in self.rows if 0 < r['box'][1]<250 and normalized(r['text'])==normalized(name)])==1
                and any(r['class']=='android.widget.EditText' for r in self.rows))

    async def direct_inbox(self, account):
        await self.home(account)
        bottom = max(row['box'][3] for row in self.rows)
        buttons = [row for row in self.rows if row.get('content-desc', '').startswith('消息') and row['box'][1] > bottom * .8]
        if len(buttons) != 1:
            raise InteractionBlocked('dm_message_entry_ambiguous')
        await self.tap(buttons[0])
        if not self.matching_rows('评论和@'):
            raise InteractionBlocked('dm_inbox_unverified')

    async def scan_direct(self, account, scope):
        if scope not in {'new','history'}:
            raise ValueError('invalid_dm_scope')
        await self.direct_inbox(account)
        rows = conversation_rows(self.root)
        return [dict(item,thread=_digest(['dm-inbox',account['account_id'],normalized(item['name'])]))
                for item in rows if scope=='history' or item['unread']][:10]

    @staticmethod
    def _message_key(row):
        return (row.get('direction', ''), row.get('text', ''), row.get('clock', ''))

    async def _extend_history(self, values, needed):
        """Read older visible bubbles only when an exact overlap proves order."""
        combined = list(values)
        for _ in range(3):
            await self.device.swipe(540, 650, 540, 1800, 500)
            await self.observe()
            older = message_rows(self.root)
            old_keys = [self._message_key(row) for row in older if row.get('text')]
            new_keys = [self._message_key(row) for row in combined if row.get('text')]
            overlap = 0
            for count in range(1, min(len(old_keys), len(new_keys)) + 1):
                if old_keys[-count:] == new_keys[:count]:
                    overlap = count
            if not overlap:
                break
            shared = old_keys[-overlap:]
            if any(sum(key == item for item in old_keys + new_keys) != 2 for key in shared):
                break
            prefix = [row for row in older if row.get('text')][:-overlap]
            combined = prefix + combined
            tail = []
            for row in reversed(combined):
                if row['direction'] != 'incoming':
                    break
                if row['text']:
                    tail.append(row)
            if len(tail) >= needed:
                break
        return combined

    async def _return_to_tail(self, expected):
        for _ in range(4):
            values = message_rows(self.root)
            if values and values[-1]['direction'] == 'incoming' and values[-1]['text'] == expected:
                return
            await self.device.swipe(540, 1800, 540, 650, 500)
            await self.observe()
        raise InteractionBlocked('dm_chat_tail_unverified')

    async def read_direct(self, account, thread, name, unread_count=1, seen_messages=()):
        provisional = _digest(['dm-inbox',account['account_id'],normalized(name)])
        if thread == provisional:
            await self.direct_inbox(account)
            matches = [r for r in conversation_rows(self.root) if normalized(r['name'])==normalized(name)]
            if len(matches)!=1:
                raise InteractionBlocked('dm_thread_ambiguous')
            await self.tap(matches[0]['row'])
            if not self._chat_header(name):
                raise InteractionBlocked('dm_chat_unverified')
            # Tapping the inbox avatar opens the conversation. The left bubble's
            # avatar opens the actual author's profile, which owns the account ID.
            avatars = []
            for view in self.root.iter('node'):
                if view.get('class')!='androidx.recyclerview.widget.RecyclerView' or box(view)[1]>500:
                    continue
                avatars.extend(n for n in view.iter('node') if n.get('class')=='android.widget.ImageView'
                               and n.get('clickable')=='true' and 0 < box(n)[0]<90
                               and 75 < box(n)[2]-box(n)[0]<155)
            if not avatars:
                raise InteractionBlocked('dm_no_inbound_avatar')
            await self.tap({'box':box(avatars[-1])})
            from types import SimpleNamespace
            thread = identity(SimpleNamespace(text='\n'.join(r['text'] for r in self.rows)))
            if (not thread or not any(normalized(r['text'])==normalized(name) for r in self.rows)
                    or not self.matching_rows('发私信')):
                raise InteractionBlocked('dm_profile_unverified')
            await self.click('发私信')
        else:
            await self.recipient_chat(account,{'profile_id':thread,'note_author':name})
        if not self._chat_header(name):
            raise InteractionBlocked('dm_chat_unverified')
        values = message_rows(self.root)
        incoming = [row for row in values if row['direction'] == 'incoming']
        if not incoming or not incoming[-1]['text']:
            raise InteractionBlocked('dm_no_text_inbound')
        latest = incoming[-1]
        if values[-1] is not latest:
            raise InteractionBlocked('dm_latest_not_inbound')
        run = []
        for row in reversed(values):
            if row['direction'] != 'incoming':
                break
            if not row['text']:
                raise InteractionBlocked('dm_inbound_run_incomplete')
            run.append(row)
        run.reverse()
        if not run or any(sum(row['direction'] == 'incoming' and row['text'] == item['text']
                              and row.get('clock', '') == item.get('clock', '') for row in values) != 1 for item in run):
            raise InteractionBlocked('dm_source_ambiguous')
        unread_count = max(1, int(unread_count or 1))
        seen = {(str(row.get('text', '')), str(row.get('clock', ''))) for row in seen_messages if isinstance(row, dict)}
        fresh = [row for row in run if (row['text'], str(row.get('clock', ''))) not in seen]
        if len(fresh) < unread_count:
            values = await self._extend_history(values, unread_count)
            run = []
            for row in reversed(values):
                if row['direction'] != 'incoming':
                    break
                if not row['text']:
                    raise InteractionBlocked('dm_inbound_run_incomplete')
                run.append(row)
            run.reverse()
            fresh = [row for row in run if (row['text'], str(row.get('clock', ''))) not in seen]
        unread_remaining = max(0, unread_count - len(fresh))
        # The badge proves how many newest inbound messages need attention.
        # Retain only that suffix, so earlier messages in the visible thread are
        # never reintroduced as fresh material.
        run = fresh[-unread_count:]
        if not run:
            raise InteractionBlocked('dm_unread_material_not_visible')
        messages = [{'text': row['text'], 'clock': row.get('clock', '')} for row in run]
        first = values.index(next(row for row in values if row is fresh[0]))
        prior_messages = [{'direction': row['direction'], 'text': row['text']} for row in values[max(0, first-4):first]
                          if row['direction'] in {'incoming', 'outgoing'} and row['text']]
        source = _digest([account['account_id'], thread, messages])
        return {'source': source, 'thread': thread, 'name': name, 'incoming_text': messages[-1]['text'],
                'incoming_messages': messages,
                'received_at': now(), 'account': account, 'unread_count': unread_count,
                'unread_remaining': unread_remaining, 'truncated': bool(unread_remaining),
                'prior_messages': prior_messages}

    async def prepare_direct_reply(self, account, source, text):
        if source['account'] != account:
            raise InteractionBlocked('dm_account_changed')
        material = await self.read_direct(account, source['thread'], source['name'], source.get('unread_count', 1))
        if material['source'] != source['source']:
            raise InteractionBlocked('dm_latest_inbound_changed')
        await self._return_to_tail(source['incoming_text'])
        edits = [row for row in self.rows if row['class'] == 'android.widget.EditText']
        if any(row.get('text') not in ('', '发消息…', '发消息...') and row.get('hint') != row.get('text') for row in edits):
            raise InteractionBlocked('dm_existing_editor_requires_attention')
        if len(edits)!=1:
            raise InteractionBlocked('dm_editor_unverified')
        await self.tap(edits[0])
        edits = [r for r in self.rows if r['class']=='android.widget.EditText' and r.get('focused')=='true']
        if len(edits) != 1 or not await self.device.set_text(edits[0]['box'], text, require_empty=True):
            raise InteractionBlocked('dm_editor_unverified')
        await self.observe()
        if not any(row['class'] == 'android.widget.EditText' and row['text'] == text for row in self.rows):
            raise InteractionBlocked('dm_text_readback_mismatch')
        self._prepared_text = text
        self.send_button()
        return material

    def send_button(self):
        editors = [r for r in self.rows if r['class']=='android.widget.EditText'
                   and r['text']==self._prepared_text and r.get('focused')=='true']
        if len(editors)!=1:
            raise InteractionBlocked('dm_editor_changed')
        left,top,right,bottom=editors[0]['box']
        buttons = [r for r in self.rows if r['text']=='发送' and r.get('clickable')=='true'
                   and r.get('enabled')=='true' and r['box'][0]>=right
                   and top<=(r['box'][1]+r['box'][3])//2<=bottom]
        if len(buttons)!=1:
            raise InteractionBlocked('dm_send_control_unverified')
        return buttons[0]

    async def clear_owned_draft(self, source, text):
        """Recover a draft whose exact input/readback preceded an unsubmitted claim."""
        await self.observe()
        if not self._chat_header(source['name']):
            return
        values=message_rows(self.root)
        if not values or values[-1]['direction']!='incoming' or values[-1]['text']!=source['incoming_text']:
            return
        editors=[r for r in self.rows if r['class']=='android.widget.EditText' and r['text']==text]
        if len(editors)!=1:
            return
        await self.tap({'box':values[-1]['avatar']})
        from types import SimpleNamespace
        peer=identity(SimpleNamespace(text='\n'.join(r['text'] for r in self.rows)))
        await self.refresh_after_back()
        editors=[r for r in self.rows if r['class']=='android.widget.EditText' and r['text']==text]
        if peer==source['thread'] and self._chat_header(source['name']) and len(editors)==1:
            await self.tap(editors[0])
            editors=[r for r in self.rows if r['class']=='android.widget.EditText'
                     and r['text']==text and r.get('focused')=='true']
            if len(editors)!=1:
                raise InteractionBlocked('dm_owned_draft_uncleared')
            if not await self.device.clear_text(editors[0]['box']):
                raise InteractionBlocked('dm_owned_draft_uncleared')
            await self.observe()
            await self.leave_verified_chat()

    async def submit_direct_reply(self):
        await self.observe()
        await self.tap(self.send_button())

    async def verify_direct_reply(self, source, text):
        if source['account'] != account_config():
            return False
        await self.recipient_chat(source['account'], {'profile_id':source['thread'],'note_author':source['name']})
        values = message_rows(self.root)
        verified = (len(values)>=2 and values[-2]['direction']=='incoming'
                and values[-2]['text']==source['incoming_text'] and values[-1]['direction']=='outgoing'
                and values[-1]['text']==text and not any(self.matching_rows(x) for x in ('发送失败','重新发送','重发')))
        await self.leave_verified_chat()
        return verified


def _receipt(value):
    return {'state': value['state'], 'source': value['source'], 'error': value.get('error', '')}


def same_account(source, account):
    """Keep an approved source with the platform account that actually received it."""
    return bool(source and account.get('account_id')
                and (source.get('account') or {}).get('account_id') == account['account_id'])


async def resolve_pending_identities(session, *, phone, account, store):
    """Privately resolve visible inbox rows before deciding whether AI may see them.

    This is deliberately a package-internal pass: it records a native,
    stable profile ID and frozen source locally, but never returns unknown
    text to AI.  A bound contact (including the configured owner account) is
    admitted only after this verification; every other source remains a
    token-bound owner-review batch.
    """
    for pending in list(store.review_pending(session)):
        if pending.get('state') != 'pending':
            continue
        thread = pending['thread']
        try:
            if pending.get('review_source'):
                # The red dot has already been consumed. Use the batch frozen when it was visible.
                if not same_account(store.source(session, pending['review_source']), account):
                    continue
                profile = pending.get('profile_id')
                from xhs_interactions.native import config as interaction_config
                if profile and not store.is_blocked(profile) and (profile == interaction_config().get('owner_account_id')
                        or (store.contact(profile) or {}).get('state') == 'bound'):
                    store.approve_batch(session, thread, pending['batch_token'])
                continue
            material = await phone.read_direct(account, thread, pending['name'],
                                               pending.get('unread_count', 1), pending.get('seen_messages', ()))
            profile_id = material.get('thread', '')
            contact = store.contact(profile_id)
            from xhs_interactions.native import config as interaction_config
            is_owner = bool(profile_id and profile_id == interaction_config().get('owner_account_id'))
            source_value = {**material, 'inbox_thread': thread, 'owner_preview': True,
                            'previewed_at': now(), 'identity_checked': True}
            stored = store.put_source(session, source_value)
            store.freeze_preview(session, thread, pending['batch_token'], stored)
            if (pending.get('previous_rejected_source')
                    and pending['previous_rejected_source'] == stored['source']):
                # Same immutable incoming material as the rejected batch:
                # retain the rejection quietly.  A same-sized but different
                # source falls through to a new owner-review batch.
                store.reject_batch(session, thread, pending['batch_token'])
            # The token binds both owner review and automatic admission to the
            # exact native source, rather than to a volatile unread count.
            elif is_owner or (contact or {}).get('state') == 'bound':
                store.approve_batch(session, thread, pending['batch_token'])
            if isinstance(phone, DirectMessagesPhone):
                await phone.leave_verified_chat()
        except (InteractionBlocked, ValueError):
            # A changed UI / ambiguous profile simply remains non-interactive
            # until its owner can inspect it; it must never become a pass.
            if isinstance(phone, DirectMessagesPhone):
                try:
                    await phone.leave_verified_chat()
                except InteractionBlocked:
                    pass


async def dm_request(session, action, thread='', source='', text='', scope='new', batch_token='', device=None, store=None):
    """Scan/read/reply, returning compact durable receipts for the action package."""
    if not session:
        raise ValueError('session_id_required')
    if action not in {'scan', 'inspect', 'read', 'reply', 'reconcile', 'leave'}:
        raise ValueError('invalid_dm_action')
    account = account_config()
    store = store or DirectMessageStore()
    phone = device or DirectMessagesPhone()
    if scope not in {'new','history'}:
        raise ValueError('dm_scope_invalid')
    if action == 'scan' and scope == 'history':
        with store.connect() as db:
            past = db.execute("SELECT record FROM dm_sources WHERE session=? AND json_extract(record,'$.identity_binding.delivered_at') IS NOT NULL ORDER BY rowid DESC", (session,)).fetchall()
        latest = {}
        for row, in past:
            item = json.loads(row)
            peer = item['thread']
            if same_account(item, account) and peer not in latest and not store.is_blocked(peer):
                latest[peer] = {'thread':peer,'source':item['source'],'name':item['name'],'unread':False,'pending':False,'unread_count':0}
        return {'status':'observed','items':list(latest.values())}
    if action == 'read' and scope == 'history' and source:
        value = store.source(session, source) or {}
        if (not same_account(value, account) or not (value.get('identity_binding') or {}).get('delivered_at') or value.get('thread') != thread
                or store.is_blocked(thread)):
            raise ValueError('dm_source_not_admitted')
        return {'status':'read','source':source,'thread':thread,'name':value['name'],'text':value['incoming_text'],
                'messages':value.get('incoming_messages') or [{'text':value['incoming_text']}],
                'recognition':_recognition(value)}
    if action == 'scan':
        with await phone.device.lease_async('xhs_dm_scan'):
            items = await phone.scan_direct(account, scope)
            for item in items:
                store.put_thread(session, item['thread'], item['name'])
                if item['unread']:
                    store.mark_pending(session, item['thread'], item['name'], item.get('unread_count', 1))
        with await phone.device.lease_async('xhs_dm_identity'):
            await resolve_pending_identities(session, phone=phone, account=account, store=store)
        pending = [row for row in store.interactive_pending(session)
                   if same_account(store.source(session, row['review_source']), account)]
        unread_threads = {item['thread'] for item in items if item['unread']}
        return {'status': 'observed', 'items': [{'thread': item['thread'], 'name': item['name'],
                'unread': item['thread'] in unread_threads, 'pending': True,
                'unread_count': item.get('unread_count', 1),
                'truncated': bool(item.get('source') and item.get('unread_count', 1) > 0)} for item in pending]}
    if action in {'inspect', 'read'}:
        if not thread:
            raise ValueError('dm_thread_required')
        name = store.thread_name(session, thread)
        if not name:
            raise ValueError('dm_name_required')
        pending = next((row for row in store.pending(session) if row['thread'] == thread), None)
        if not pending:
            raise ValueError('dm_owner_approval_required')
        if pending.get('review_source') and not same_account(store.source(session, pending['review_source']), account):
            raise ValueError('dm_account_changed')
        if action == 'inspect':
            if pending.get('admission') == 'approved':
                raise ValueError('dm_batch_already_approved')
            if not secrets.compare_digest(str(pending.get('batch_token','')), str(batch_token or '')):
                raise ValueError('dm_batch_changed')
            if pending.get('review_source'):
                stored = store.source(session, pending['review_source'])
                if stored:
                    return {'status': 'previewed', 'source': stored['source'], 'thread': stored['thread'],
                            'name': stored['name'], 'text': stored['incoming_text'],
                            'messages': stored.get('incoming_messages') or [{'text': stored['incoming_text'], 'clock': ''}],
                            'truncated': bool(stored.get('truncated')),
                            'unread_remaining': stored.get('unread_remaining', 0)}
        elif pending.get('admission') != 'approved':
            raise ValueError('dm_owner_approval_required')
        if action == 'read' and pending.get('review_source'):
            stored = store.promote_preview(session, thread, pending.get('batch_token'), pending['review_source'])
            return {'status': 'read', 'source': stored['source'], 'thread': stored['thread'], 'name': stored['name'],
                    'text': stored['incoming_text'], 'messages': stored.get('incoming_messages') or [{'text': stored['incoming_text'], 'clock': ''}],
                    'truncated': bool(stored.get('truncated')), 'unread_remaining': stored.get('unread_remaining', 0),
                    'recognition': _recognition(stored),
                    'prior_messages': stored.get('prior_messages', []) if stored['identity_binding']['binding_status'] == 'bound' else []}
        if action == 'read':
            raise ValueError('dm_preview_required')
        with await phone.device.lease_async('xhs_dm_read'):
            material = await phone.read_direct(account, thread, name, pending.get('unread_count', 1),
                                               pending.get('seen_messages', ()))
            if store.is_blocked(material.get('thread')):
                raise ValueError('dm_contact_blocked')
            stored = store.put_source(session, {**material, 'inbox_thread':thread, 'owner_preview':True,
                                                'previewed_at':now(), 'identity_checked':True})
            store.freeze_preview(session, thread, batch_token, stored)
            if isinstance(phone, DirectMessagesPhone):
                await phone.leave_verified_chat()
        return {'status':'previewed','source':stored['source'],'thread':stored['thread'],'name':stored['name'],
                'text':stored['incoming_text'],'messages':stored.get('incoming_messages') or [{'text':stored['incoming_text']}],
                'truncated':bool(stored.get('truncated')), 'unread_remaining':stored.get('unread_remaining',0)}
    if not source:
        raise ValueError('dm_source_required')
    material = store.source(session, source) or {}
    if material and not same_account(material, account):
        raise ValueError('dm_account_changed')
    if not (material.get('identity_binding') or {}).get('delivered_at'):
        raise ValueError('dm_source_not_admitted')
    if store.is_blocked(material.get('thread')):
        raise ValueError('dm_contact_blocked')
    prior = store.reply(session, source)
    if action == 'leave':
        store.complete_source(session, source)
        return {'status': 'left', 'source': source}
    if action == 'reconcile':
        if not prior or prior['state'] not in {'submitting', 'submitted_unverified'}:
            return {'status': prior['state'] if prior else 'not_found'}
        material = store.source(session, source)
        with await phone.device.lease_async('xhs_dm_reconcile'):
            verified = await phone.verify_direct_reply(material, prior['text'])
        if verified:
            prior = store.transition(session, source, {prior['state']}, 'published', error='')
            store.complete_source(session, source)
        return {'status': prior['state'], 'dm_reply': _receipt(prior)}
    item = prior or store.claim(session, source, text)
    if prior and prior['text'] != text:
        raise ValueError('dm_reply_already_claimed')
    if item['state'] in {'published','submitting','submitted_unverified'}:
        return {'status': item['state'], 'dm_reply': _receipt(item)}
    material = store.source(session, source)
    with await phone.device.lease_async('xhs_dm_reply'):
        item = store.reply(session,source)
        if item['state']=='blocked':
            # Only failures before the durable submitting transition are blocked.
            if item.get('error')=='dm_send_control_unverified' and isinstance(phone,DirectMessagesPhone):
                await phone.clear_owned_draft(material,item['text'])
            item = store.transition(session,source,{'blocked'},'preparing')
        if item['state']!='preparing':
            return {'status':item['state'],'dm_reply':_receipt(item)}
        try:
            await phone.prepare_direct_reply(account, material, item['text'])
            # The owner may have blocked this stable account while AI was composing.
            if store.is_blocked(material.get('thread', '')):
                raise ValueError('dm_contact_blocked')
            item = store.transition(session, source, {'preparing'}, 'submitting')
            await phone.submit_direct_reply()
            item = store.transition(session, source, {'submitting'}, 'submitted_unverified')
            if await phone.verify_direct_reply(material, item['text']):
                item = store.transition(session, source, {'submitted_unverified'}, 'published', error='')
                store.complete_source(session, source)
        except Exception as exc:
            current = store.reply(session, source)
            state = 'submitted_unverified' if current and current['state'] in {'submitting', 'submitted_unverified'} else 'blocked'
            item = store.transition(session, source, {current['state']}, state,
                                    error=str(exc) if isinstance(exc, (InteractionBlocked, ValueError))
                                    and str(exc) in {'dm_contact_blocked'} else 'dm_execution_error')
    return {'status': item['state'], 'dm_reply': _receipt(item)}
