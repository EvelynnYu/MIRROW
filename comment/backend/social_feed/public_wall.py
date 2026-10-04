"""Single authoritative public social wall, separate from the private feed.

External credentials are resolved at the gateway.  This module receives only
stable actor IDs; it never stores a visitor Key or trusts a display name as an
identity.  Tombstones retain no post/comment body after withdrawal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class PublicWallError(ValueError):
    pass


class PublicWall:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wall_profiles (
                    actor_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL DEFAULT '',
                    avatar TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wall_registered_names (
                    visitor_id TEXT PRIMARY KEY,
                    registered_name TEXT NOT NULL,
                    registered_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wall_contacts (
                    visitor_id TEXT PRIMARY KEY,
                    first_social_login_at REAL NOT NULL,
                    linked_friend_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS wall_removed_contacts (
                    visitor_id TEXT PRIMARY KEY,
                    removed_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS wall_contacts_friend_link
                    ON wall_contacts(linked_friend_id) WHERE linked_friend_id != '';
                CREATE TABLE IF NOT EXISTS wall_household_links (
                    ai_visitor_id TEXT PRIMARY KEY,
                    human_visitor_id TEXT NOT NULL,
                    linked_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wall_household_human
                    ON wall_household_links(human_visitor_id);
                CREATE TABLE IF NOT EXISTS wall_remarks (
                    viewer_id TEXT NOT NULL,
                    target_actor_id TEXT NOT NULL,
                    remark TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (viewer_id, target_actor_id)
                );
                CREATE TABLE IF NOT EXISTS wall_moments (
                    id TEXT PRIMARY KEY,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    withdrawn_at REAL,
                    forward_json TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS wall_moments_timeline
                    ON wall_moments(created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS wall_comments (
                    id TEXT PRIMARY KEY,
                    moment_id TEXT NOT NULL REFERENCES wall_moments(id) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reply_to_id TEXT REFERENCES wall_comments(id) ON DELETE SET NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wall_comments_by_moment
                    ON wall_comments(moment_id, created_at);
                CREATE TABLE IF NOT EXISTS wall_likes (
                    moment_id TEXT NOT NULL REFERENCES wall_moments(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (moment_id, actor)
                );
                CREATE TABLE IF NOT EXISTS wall_changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    moment_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('upsert', 'withdrawn')),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wall_moderation_actions (
                    id TEXT PRIMARY KEY,
                    moment_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    moderator TEXT NOT NULL CHECK (moderator = 'aning'),
                    action TEXT NOT NULL CHECK (action = 'host_removed'),
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wall_moderation_moment
                    ON wall_moderation_actions(moment_id, created_at);
                CREATE TABLE IF NOT EXISTS wall_notifications (
                    id TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL CHECK (recipient IN ('aning','k')),
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('moment','comment','like')),
                    moment_id TEXT NOT NULL REFERENCES wall_moments(id) ON DELETE CASCADE,
                    comment_id TEXT,
                    created_at REAL NOT NULL,
                    announced_at REAL,
                    announce_source TEXT NOT NULL DEFAULT '',
                    announce_source_id TEXT NOT NULL DEFAULT '',
                    read_at REAL,
                    read_source TEXT NOT NULL DEFAULT '',
                    read_source_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS wall_notifications_recipient
                    ON wall_notifications(recipient,read_at,announced_at,created_at);
                CREATE TABLE IF NOT EXISTS wall_sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    visitor_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wall_sessions_expiry ON wall_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS wall_receipts (
                    source_key TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            from .mentions import initialize_public
            initialize_public(db)
            # Additive and idempotent: normal startup never touches transfer
            # schema, but new forward metadata is safe for old wall rows.
            try:
                db.execute("ALTER TABLE wall_moments ADD COLUMN forward_json TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA secure_delete=ON')
        return db

    @staticmethod
    def _text(value: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise PublicWallError('invalid_content')
        clean = value.strip()
        if not clean or len(clean) > maximum or any(ord(ch) < 32 and ch not in '\n\t' for ch in clean):
            raise PublicWallError('invalid_content')
        return clean

    @staticmethod
    def _actor(actor: str) -> str:
        if actor in {'aning', 'k'} or (isinstance(actor, str) and actor.startswith('visitor:') and len(actor) <= 100):
            return actor
        raise PublicWallError('invalid_actor')

    def migrate_transfers(self) -> None:
        """Explicit, backup-gated schema migration; never called by initialize()."""
        from .migration import migrate_schema
        self.initialize()
        with self._connect() as db:
            migrate_schema(db)

    def transfer_person(self, actor: str) -> str:
        if not isinstance(actor, str) or not actor.startswith('archive:') or len(actor) != 40:
            return ''
        with self._connect() as db:
            from .migration import ready
            if not ready(db):
                return ''
            row = db.execute('SELECT display_name FROM wall_transfer_people WHERE actor_id=?', (actor,)).fetchone()
        return str(row['display_name']) if row else ''

    def profile(self, actor: str) -> dict[str, str]:
        actor = self._actor(actor)
        with self._connect() as db:
            row = db.execute('SELECT nickname, avatar FROM wall_profiles WHERE actor_id=?', (actor,)).fetchone()
        from .profile_identity import ProfileIdentityStore
        return ProfileIdentityStore(self).overlay(actor, {
            'actor_id': actor, 'nickname': row['nickname'] if row else '', 'avatar': row['avatar'] if row else ''})

    def registered_name(self, visitor_id: str) -> str:
        if not isinstance(visitor_id, str) or not visitor_id or len(visitor_id) > 100:
            raise PublicWallError('invalid_visitor')
        with self._connect() as db:
            row = db.execute('SELECT registered_name FROM wall_registered_names WHERE visitor_id=?',
                             (visitor_id,)).fetchone()
        return str(row['registered_name']) if row else ''

    def registered_names(self) -> dict[str, str]:
        with self._connect() as db:
            return {str(row['visitor_id']): str(row['registered_name']) for row in db.execute(
                'SELECT visitor_id,registered_name FROM wall_registered_names')}

    @staticmethod
    def _register_name(db: sqlite3.Connection, visitor_id: str, name: str, now: float) -> None:
        name = name.strip()
        if not name or len(name) > 40:
            raise PublicWallError('invalid_registered_name')
        current = db.execute('SELECT registered_name FROM wall_registered_names WHERE visitor_id=?',
                             (visitor_id,)).fetchone()
        if current and current['registered_name'] != name:
            raise PublicWallError('registered_name_conflict')
        if not current:
            db.execute('INSERT INTO wall_registered_names(visitor_id,registered_name,registered_at) VALUES(?,?,?)',
                       (visitor_id, name, now))

    def register_human_name(self, visitor_id: str, name: str, *, profile_source=None,
                            profile_origin='') -> str:
        if not isinstance(visitor_id, str) or not visitor_id or len(visitor_id) > 100:
            raise PublicWallError('invalid_visitor')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if profile_origin:
                from .profile_registration import enroll_in
                enroll_in(db, visitor_id, profile_source, profile_origin)
            self._register_name(db, visitor_id, name, time.time())
        return name.strip()

    def note_contact(self, visitor_id: str) -> bool:
        """Record first authenticated social use, never infer a person from a name."""
        if not isinstance(visitor_id, str) or not visitor_id or len(visitor_id) > 100:
            raise PublicWallError('invalid_visitor')
        with self._connect() as db:
            row = db.execute('INSERT OR IGNORE INTO wall_contacts(visitor_id,first_social_login_at) VALUES(?,?)',
                             (visitor_id, time.time()))
            return row.rowcount == 1

    def contacts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT visitor_id,first_social_login_at,linked_friend_id '
                'FROM wall_contacts WHERE visitor_id NOT IN (SELECT visitor_id FROM wall_removed_contacts) '
                'ORDER BY first_social_login_at DESC,visitor_id DESC')]

    def removed_contacts(self) -> set[str]:
        with self._connect() as db:
            return {row[0] for row in db.execute('SELECT visitor_id FROM wall_removed_contacts')}

    def remove_contact(self, visitor_id: str) -> None:
        """Hide the roster entry, retaining names on historical posts and receipts."""
        self.remove_contacts([visitor_id])

    def remove_contacts(self, visitor_ids: list[str]) -> None:
        """Atomically remove an explicit household set, never its Key records."""
        visitor_ids = list(dict.fromkeys(visitor_ids))
        if not visitor_ids or any(not isinstance(value,str) or not value or len(value)>100 for value in visitor_ids):
            raise PublicWallError('invalid_visitor')
        with self._connect() as db:
            db.executemany('INSERT INTO wall_removed_contacts VALUES(?,?) ON CONFLICT(visitor_id) DO UPDATE SET removed_at=excluded.removed_at',
                           [(visitor_id,time.time()) for visitor_id in visitor_ids])
            db.executemany('DELETE FROM wall_household_links WHERE ai_visitor_id=? OR human_visitor_id=?',
                           [(visitor_id,visitor_id) for visitor_id in visitor_ids])
            db.executemany('DELETE FROM wall_sessions WHERE visitor_id=?',[(visitor_id,) for visitor_id in visitor_ids])

    def prepared_visitor_ids(self) -> list[str]:
        """Owner-seeded identity cards that may not have visited the wall yet."""
        with self._connect() as db:
            return [row[0][8:] for row in db.execute(
                "SELECT actor_id FROM wall_profiles WHERE substr(actor_id,1,8)='visitor:' "
                "UNION SELECT 'visitor:' || visitor_id FROM wall_registered_names")]

    def linked_friend(self, visitor_id: str) -> str:
        with self._connect() as db:
            row = db.execute('SELECT linked_friend_id FROM wall_contacts WHERE visitor_id=?', (visitor_id,)).fetchone()
            return str(row['linked_friend_id']) if row else ''

    def linked_visitor(self, friend_id: str) -> str:
        with self._connect() as db:
            row = db.execute('SELECT visitor_id FROM wall_contacts WHERE linked_friend_id=?', (friend_id,)).fetchone()
            return str(row['visitor_id']) if row else ''

    def household_human(self, ai_visitor_id: str) -> str:
        with self._connect() as db:
            row = db.execute('SELECT human_visitor_id FROM wall_household_links WHERE ai_visitor_id=?',
                             (ai_visitor_id,)).fetchone()
        return str(row['human_visitor_id']) if row else ''

    def household_links(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT ai_visitor_id,human_visitor_id,linked_at FROM wall_household_links ORDER BY linked_at DESC')]

    def link_household(self, human_visitor_id: str, ai_visitor_id: str, *,
                       human_name: str = '', ai_name: str = '') -> dict[str, Any]:
        result = self.link_households(human_visitor_id, [(ai_visitor_id, ai_name)], human_name=human_name)
        return {'human_visitor_id': human_visitor_id, 'ai_visitor_id': ai_visitor_id,
                'already_linked': result['linked'][0]['already_linked']}

    def link_households(self, human_visitor_id: str, ais: list[tuple[str, str]], *,
                        human_name: str = '', profile_sources=None, profile_origin='') -> dict[str, Any]:
        """Bind one human to distinct AIs atomically; never store names as identity or raw Keys."""
        ai_ids = [visitor_id for visitor_id, _ in ais]
        if (not isinstance(human_visitor_id, str) or not human_visitor_id or len(human_visitor_id) > 100
                or not 1 <= len(ais) <= 8 or len(set(ai_ids)) != len(ai_ids)
                or any(not isinstance(visitor_id, str) or not visitor_id or len(visitor_id) > 100
                       or visitor_id == human_visitor_id for visitor_id in ai_ids)):
            raise PublicWallError('invalid_household_link')
        if any(not isinstance(name, str) or len(name.strip()) > 40 for name in [human_name, *(name for _, name in ais)]):
            raise PublicWallError('invalid_nickname')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {ai_id: db.execute('SELECT human_visitor_id FROM wall_household_links WHERE ai_visitor_id=?',
                                          (ai_id,)).fetchone() for ai_id in ai_ids}
            if any(row and row['human_visitor_id'] != human_visitor_id for row in existing.values()):
                raise PublicWallError('ai_already_linked_to_other_human')
            for ai_id in ai_ids:
                if not existing[ai_id]:
                    db.execute('INSERT INTO wall_household_links(ai_visitor_id,human_visitor_id,linked_at) VALUES(?,?,?)',
                               (ai_id, human_visitor_id, now))
            for visitor_id, name in [(human_visitor_id, human_name), *ais]:
                if name.strip():
                    if profile_sources is not None:
                        from .profile_registration import enroll_in
                        enroll_in(db, visitor_id, profile_sources.get(visitor_id), profile_origin)
                    self._register_name(db, visitor_id, name, now)
        return {'human_visitor_id': human_visitor_id,
                'linked': [{'ai_visitor_id': ai_id, 'already_linked': bool(existing[ai_id])} for ai_id in ai_ids]}

    def rebind_household(self, ai_visitor_id: str, previous_human_visitor_id: str,
                         new_human_visitor_id: str) -> dict[str, str]:
        """Move an existing AI link only when its current human still matches the owner's view."""
        values = (ai_visitor_id, previous_human_visitor_id, new_human_visitor_id)
        if (any(not isinstance(value, str) or not value or len(value) > 100 for value in values)
                or ai_visitor_id == new_human_visitor_id
                or previous_human_visitor_id == new_human_visitor_id):
            raise PublicWallError('invalid_household_link')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT human_visitor_id FROM wall_household_links WHERE ai_visitor_id=?',
                             (ai_visitor_id,)).fetchone()
            if not row or row['human_visitor_id'] != previous_human_visitor_id:
                raise PublicWallError('household_link_changed')
            db.execute('UPDATE wall_household_links SET human_visitor_id=?,linked_at=? WHERE ai_visitor_id=?',
                       (new_human_visitor_id, time.time(), ai_visitor_id))
        return {'ai_visitor_id': ai_visitor_id, 'previous_human_visitor_id': previous_human_visitor_id,
                'human_visitor_id': new_human_visitor_id}

    @staticmethod
    def _guardian_can_manage(db: sqlite3.Connection, viewer: str, author: str) -> bool:
        if viewer == author:
            return True
        if not viewer.startswith('visitor:') or not author.startswith('visitor:'):
            return False
        return bool(db.execute('SELECT 1 FROM wall_household_links WHERE human_visitor_id=? AND ai_visitor_id=?',
                               (viewer[8:], author[8:])).fetchone())

    def can_manage_moment(self, viewer: str, author: str) -> bool:
        viewer, author = self._actor(viewer), self._actor(author)
        with self._connect() as db:
            return self._guardian_can_manage(db, viewer, author)

    def can_delete_comment(self, viewer: str, author: str) -> bool:
        viewer, author = self._actor(viewer), self._actor(author)
        with self._connect() as db:
            from .profile_registration import state_in
            return state_in(db, viewer)['can_interact'] and (viewer == 'aning' or self._guardian_can_manage(db, viewer, author))

    @staticmethod
    def _remark_viewer(viewer: str) -> str:
        viewer = PublicWall._actor(viewer)
        return 'home' if viewer in {'aning', 'k'} else viewer

    def known_actor(self, actor: str) -> bool:
        actor = self._actor(actor)
        if actor in {'aning', 'k'}:
            return True
        visitor_id = actor[8:]
        with self._connect() as db:
            return bool(db.execute('SELECT 1 FROM wall_contacts WHERE visitor_id=? '
                                   'UNION SELECT 1 FROM wall_profiles WHERE actor_id=? '
                                   'UNION SELECT 1 FROM wall_registered_names WHERE visitor_id=? '
                                   'UNION SELECT 1 FROM wall_moments WHERE author=? '
                                   'UNION SELECT 1 FROM wall_comments WHERE author=? '
                                   'UNION SELECT 1 FROM wall_likes WHERE actor=? LIMIT 1',
                                   (visitor_id, actor, visitor_id, actor, actor, actor)).fetchone())

    def remark(self, viewer: str, target: str, *, home_default: str = '') -> str:
        scope = self._remark_viewer(viewer)
        target = self._actor(target)
        with self._connect() as db:
            row = db.execute('SELECT remark FROM wall_remarks WHERE viewer_id=? AND target_actor_id=?',
                             (scope, target)).fetchone()
        return row['remark'] if row else home_default if scope == 'home' else ''

    def set_remark(self, viewer: str, target: str, value: str) -> str:
        scope = self._remark_viewer(viewer)
        target = self._actor(target)
        if target == viewer or (scope == 'home' and target in {'aning', 'k'}):
            raise PublicWallError('invalid_remark_target')
        if not isinstance(value, str) or len(value) > 40 or any(ord(char) < 32 for char in value):
            raise PublicWallError('invalid_remark')
        if not self.known_actor(target):
            raise PublicWallError('person_not_found')
        clean = value.strip()
        with self._connect() as db:
            count = db.execute('SELECT COUNT(*) FROM wall_remarks WHERE viewer_id=?', (scope,)).fetchone()[0]
            existing = db.execute('SELECT 1 FROM wall_remarks WHERE viewer_id=? AND target_actor_id=?',
                                  (scope, target)).fetchone()
            if not existing and count >= 300:
                raise PublicWallError('remark_limit')
            db.execute('INSERT INTO wall_remarks(viewer_id,target_actor_id,remark,updated_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(viewer_id,target_actor_id) DO UPDATE SET remark=excluded.remark,updated_at=excluded.updated_at',
                       (scope, target, clean, time.time()))
        return clean

    def link_contact(self, visitor_id: str, friend_id: str) -> None:
        if not isinstance(visitor_id, str) or not visitor_id or not isinstance(friend_id, str) or len(friend_id) > 100:
            raise PublicWallError('invalid_contact_link')
        with self._connect() as db:
            try:
                row = db.execute('UPDATE wall_contacts SET linked_friend_id=? WHERE visitor_id=?',
                                 (friend_id, visitor_id))
            except sqlite3.IntegrityError as exc:
                raise PublicWallError('friend_already_linked') from exc
            if row.rowcount != 1:
                raise PublicWallError('contact_not_found')

    def set_profile(self, actor: str, nickname: str | None = None,
                    avatar: str | None = None) -> dict[str, str]:
        actor = self._actor(actor)
        from .profile_identity import ProfileIdentityStore
        ProfileIdentityStore(self).guard_write(actor)
        if nickname is None and avatar is None:
            raise PublicWallError('invalid_profile_update')
        if nickname is not None and (not isinstance(nickname, str) or len(nickname.strip()) > 40):
            raise PublicWallError('invalid_nickname')
        if avatar is not None and (not isinstance(avatar, str) or len(avatar) > 2048
                                   or (avatar and not avatar.startswith('https://'))):
            raise PublicWallError('invalid_avatar')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT nickname,avatar FROM wall_profiles WHERE actor_id=?', (actor,)).fetchone()
            from .profile_registration import guard_in
            guard_in(db, actor)
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_identity_links'").fetchone() and
                    db.execute('SELECT 1 FROM profile_identity_links WHERE actor=?',(actor,)).fetchone()):
                raise PublicWallError('profile_edit_at_source')
            nickname = nickname.strip() if nickname is not None else (previous['nickname'] if previous else '')
            avatar = avatar if avatar is not None else (previous['avatar'] if previous else '')
            db.execute('INSERT INTO wall_profiles(actor_id,nickname,avatar,updated_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(actor_id) DO UPDATE SET nickname=excluded.nickname,avatar=excluded.avatar,updated_at=excluded.updated_at',
                       (actor, nickname, avatar, time.time()))
        return {'actor_id': actor, 'nickname': nickname, 'avatar': avatar}

    def _item(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        moment_id = row['id']
        from .migration import ready
        has_transfers = ready(db)
        if row['withdrawn_at'] is not None:
            migrated = (db.execute("SELECT 1 FROM wall_transfer_out WHERE moment_id=? AND state='committed'",
                                   (moment_id,)).fetchone() if has_transfers else None)
            if migrated:
                return {'id': moment_id, 'author': row['author'],
                        'content': '该动态已迁移至私人服务器', 'visibility': 'public',
                        'created_at': row['created_at'], 'updated_at': row['updated_at'],
                        'revision': row['revision'], 'comments': [], 'reactions': [],
                        'migrated': True}
        from .mentions import project, decode
        comments = [project(dict(r)) for r in db.execute(
            'SELECT id,moment_id,author,content,reply_to_id,created_at,mentions_json FROM wall_comments WHERE moment_id=? ORDER BY created_at,id',
            (moment_id,))]
        reactions = [{'author': r['actor'], 'reaction': 'like', 'created_at': r['created_at']}
                     for r in db.execute('SELECT actor,created_at FROM wall_likes WHERE moment_id=? ORDER BY created_at', (moment_id,))]
        arrived = (db.execute("SELECT 1 FROM wall_transfer_in WHERE destination_moment_id=? AND state='active'",
                              (moment_id,)).fetchone() if has_transfers else None)
        from .forwarding import decode_forward_ref, same_home_projection, external_projection
        forward_ref = decode_forward_ref(row['forward_json'])
        item = {'id': moment_id, 'author': row['author'], 'content': row['content'], 'visibility': 'public',
                'created_at': row['created_at'], 'updated_at': row['updated_at'], 'revision': row['revision'],
                'mention_actor_ids': decode(row['mentions_json']),
                'comments': comments, 'reactions': reactions, 'arrived_from_other_home': bool(arrived)}
        if forward_ref:
            item['forward'] = same_home_projection(db, forward_ref) if not forward_ref['origin'] else external_projection(forward_ref)
        return item

    @staticmethod
    def _notify(db: sqlite3.Connection, recipient: str, actor: str, kind: str,
                moment_id: str, comment_id: str | None, now: float) -> None:
        if recipient not in {'aning', 'k'} or recipient == actor:
            return
        db.execute('INSERT INTO wall_notifications(id,recipient,actor,kind,moment_id,comment_id,created_at) '
                   'VALUES(?,?,?,?,?,?,?)', ('wn_' + uuid.uuid4().hex, recipient, actor, kind,
                                              moment_id, comment_id, now))

    @staticmethod
    def _mentions(db, values):
        from .mentions import validate_public
        try:
            return validate_public(db, values)
        except ValueError as exc:
            raise PublicWallError(str(exc)) from None

    def list_moments(self, *, limit: int = 30, before: tuple[float, str] | None = None,
                     start: float | None = None, end: float | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            from .migration import ready
            has_transfers = ready(db)
        where = ("WHERE (withdrawn_at IS NULL OR EXISTS (SELECT 1 FROM wall_transfer_out t "
                 "WHERE t.moment_id=wall_moments.id AND t.state='committed'))" if has_transfers else
                 'WHERE withdrawn_at IS NULL')
        params: list[Any] = []
        if start is not None:
            where += ' AND created_at>=?'
            params.append(start)
        if end is not None:
            where += ' AND created_at<?'
            params.append(end)
        if before:
            where += ' AND (created_at<? OR (created_at=? AND id<?))'
            params.extend((before[0], before[0], before[1]))
        with self._connect() as db:
            rows = db.execute(f'SELECT * FROM wall_moments {where} ORDER BY created_at DESC,id DESC LIMIT ?',
                              (*params, limit + 1)).fetchall()
            items = [self._item(db, r) for r in rows[:limit]]
        more = len(rows) > limit
        return {'items': items, 'has_more': more,
                'next_cursor': [items[-1]['created_at'], items[-1]['id']] if more and items else None}

    def get_moment(self, moment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            from .migration import ready
            if ready(db):
                row = db.execute("SELECT * FROM wall_moments WHERE id=? AND (withdrawn_at IS NULL OR EXISTS "
                                 "(SELECT 1 FROM wall_transfer_out t WHERE t.moment_id=wall_moments.id "
                                 "AND t.state='committed'))", (moment_id,)).fetchone()
            else:
                row = db.execute('SELECT * FROM wall_moments WHERE id=? AND withdrawn_at IS NULL',
                                 (moment_id,)).fetchone()
            return self._item(db, row) if row else None

    def search_moments(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        needle = str(query or '').strip()[:80]
        if not needle:
            return []
        with self._connect() as db:
            rows = db.execute('SELECT * FROM wall_moments WHERE withdrawn_at IS NULL '
                              'AND instr(content, ?) > 0 ORDER BY created_at DESC,id DESC LIMIT ?',
                              (needle, max(1, min(int(limit), 10)))).fetchall()
            return [self._item(db, row) for row in rows]

    def removal_status(self, viewer: str, moment_id: str) -> dict[str, Any]:
        viewer = self._actor(viewer)
        with self._connect() as db:
            row = db.execute('SELECT author,withdrawn_at FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
            if not row:
                raise PublicWallError('moment_not_found')
            if viewer != 'aning' and not self._guardian_can_manage(db, viewer, row['author']):
                raise PublicWallError('forbidden')
            moderation = db.execute('SELECT created_at FROM wall_moderation_actions '
                                    'WHERE moment_id=? ORDER BY created_at DESC LIMIT 1', (moment_id,)).fetchone()
            from .migration import ready
            transfer = (db.execute("SELECT destination_origin,destination_moment_id,committed_at FROM wall_transfer_out "
                                   "WHERE moment_id=? AND state='committed'", (moment_id,)).fetchone()
                        if ready(db) else None)
            return {'id': moment_id, 'status': 'withdrawn' if row['withdrawn_at'] else 'active',
                    'reason': 'migrated' if transfer else 'host_removed' if moderation else 'author_withdrawn' if row['withdrawn_at'] else '',
                    'acted_at': transfer['committed_at'] if transfer else moderation['created_at'] if moderation else row['withdrawn_at'],
                    'message': '该动态已迁移至私人服务器' if transfer else '',
                    'destination_origin': transfer['destination_origin'] if transfer else '',
                    'destination_moment_id': transfer['destination_moment_id'] if transfer else ''}

    def create_moment(self, actor: str, content: str, *, created_at: float | None = None,
                      moment_id: str | None = None, source_key: str | None = None,
                      mention_actor_ids: list[str] | None = None, forward_ref=None) -> dict[str, Any]:
        actor = self._actor(actor)
        content = self._text(content, 1200)
        now = time.time()
        created = now if created_at is None else float(created_at)
        identifier = moment_id or 'sw_' + uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            if source_key:
                row = db.execute("SELECT result_json FROM wall_receipts WHERE source_key=? AND action='post'",
                                 (source_key,)).fetchone()
                if row:
                    prior_id = json.loads(row['result_json'])['id']
                    prior = db.execute('SELECT * FROM wall_moments WHERE id=? AND withdrawn_at IS NULL',
                                       (prior_id,)).fetchone()
                    return ({**self._item(db, prior), 'idempotent_replay': True} if prior else
                            {'id': prior_id, 'status': 'withdrawn', 'idempotent_replay': True})
            mentions = self._mentions(db, mention_actor_ids)
            from .forwarding import encode_forward_ref, decode_forward_ref, same_home_projection
            try:
                forward_json = encode_forward_ref(forward_ref)
            except ValueError as exc:
                raise PublicWallError(str(exc)) from None
            ref = decode_forward_ref(forward_json)
            if ref and not ref['origin']:
                source = same_home_projection(db, ref)
                if source['status'] != 'available':
                    raise PublicWallError('forward_source_unavailable')
            from .mentions import encode, deliver
            db.execute('INSERT INTO wall_moments(id,author,content,created_at,updated_at,mentions_json,forward_json) VALUES(?,?,?,?,?,?,?)',
                       (identifier, actor, content, created, now, encode(mentions), forward_json))
            db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (identifier, 'upsert', now))
            for recipient in ('aning', 'k'):
                self._notify(db, recipient, actor, 'moment', identifier, None, now)
            deliver(db, mentions, actor, identifier, None, now)
            result = self._item(db, db.execute('SELECT * FROM wall_moments WHERE id=?', (identifier,)).fetchone())
            if source_key:
                db.execute('INSERT INTO wall_receipts(source_key,action,result_json,created_at) VALUES(?,?,?,?)',
                           (source_key, 'post', json.dumps({'id': identifier}), now))
        return result

    def edit_moment(self, actor: str, moment_id: str, content: str, revision: int) -> dict[str, Any]:
        actor = self._actor(actor)
        content = self._text(content, 1200)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            row = db.execute('SELECT author,revision FROM wall_moments WHERE id=? AND withdrawn_at IS NULL', (moment_id,)).fetchone()
            if not row:
                raise PublicWallError('moment_not_found')
            if not self._guardian_can_manage(db, actor, row['author']):
                raise PublicWallError('forbidden')
            guard_in(db, row['author'])
            if row['revision'] != revision:
                raise PublicWallError('revision_conflict')
            db.execute('UPDATE wall_moments SET content=?,revision=revision+1,updated_at=? WHERE id=?',
                       (content, now, moment_id))
            db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (moment_id, 'upsert', now))
        return self.get_moment(moment_id) or {}

    def add_comment(self, actor: str, moment_id: str, content: str, reply_to_id: str | None = None,
                    source_key: str | None = None, *, mention_actor_ids: list[str] | None = None) -> dict[str, Any]:
        actor = self._actor(actor)
        content = self._text(content, 600)
        now = time.time()
        identifier = 'wc_' + uuid.uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            if source_key:
                row = db.execute("SELECT result_json FROM wall_receipts WHERE source_key=? AND action='comment'",
                                 (source_key,)).fetchone()
                if row:
                    prior_id = json.loads(row['result_json'])['id']
                    prior = db.execute('SELECT c.id,c.moment_id,c.author,c.content,c.reply_to_id,c.created_at,c.mentions_json '
                                       'FROM wall_comments c JOIN wall_moments m ON m.id=c.moment_id '
                                       'WHERE c.id=? AND m.withdrawn_at IS NULL', (prior_id,)).fetchone()
                    from .mentions import project
                    return ({**project(dict(prior)), 'idempotent_replay': True} if prior else
                            {'id': prior_id, 'status': 'withdrawn', 'idempotent_replay': True})
            if not db.execute('SELECT 1 FROM wall_moments WHERE id=? AND withdrawn_at IS NULL', (moment_id,)).fetchone():
                raise PublicWallError('moment_not_found')
            if reply_to_id and not db.execute('SELECT 1 FROM wall_comments WHERE id=? AND moment_id=?',
                                              (reply_to_id, moment_id)).fetchone():
                raise PublicWallError('reply_not_found')
            mentions = self._mentions(db, mention_actor_ids)
            from .mentions import encode, deliver
            db.execute('INSERT INTO wall_comments(id,moment_id,author,content,reply_to_id,created_at,mentions_json) VALUES(?,?,?,?,?,?,?)',
                       (identifier, moment_id, actor, content, reply_to_id, now, encode(mentions)))
            db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (moment_id, 'upsert', now))
            moment = db.execute('SELECT author FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
            self._notify(db, moment['author'], actor, 'comment', moment_id, identifier, now)
            if reply_to_id:
                parent = db.execute('SELECT author FROM wall_comments WHERE id=?', (reply_to_id,)).fetchone()
                if parent and parent['author'] != moment['author']:
                    self._notify(db, parent['author'], actor, 'comment', moment_id, identifier, now)
            deliver(db, mentions, actor, moment_id, identifier, now)
            result = {'id': identifier, 'moment_id': moment_id, 'author': actor, 'content': content,
                      'reply_to_id': reply_to_id, 'created_at': now, 'mention_actor_ids': mentions}
            if source_key:
                db.execute('INSERT INTO wall_receipts(source_key,action,result_json,created_at) VALUES(?,?,?,?)',
                           (source_key, 'comment', json.dumps({'id': identifier, 'moment_id': moment_id}), now))
        return result

    def delete_comment(self, actor: str, moment_id: str, comment_id: str) -> dict[str, Any]:
        actor = self._actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            row = db.execute('SELECT c.author FROM wall_comments c JOIN wall_moments m ON m.id=c.moment_id '
                             'WHERE c.id=? AND c.moment_id=? AND m.withdrawn_at IS NULL',
                             (comment_id, moment_id)).fetchone()
            if not row:
                raise PublicWallError('comment_not_found')
            if actor != 'aning' and not self._guardian_can_manage(db, actor, row['author']):
                raise PublicWallError('forbidden')
            # Replies remain in the conversation, but ON DELETE SET NULL severs
            # their pointer to the removed text. Pending alerts no longer quote it.
            db.execute('DELETE FROM wall_notifications WHERE comment_id=?', (comment_id,))
            db.execute('DELETE FROM wall_comments WHERE id=?', (comment_id,))
            db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                       (moment_id, 'upsert', now))
        return {'id': comment_id, 'moment_id': moment_id, 'status': 'deleted'}

    def toggle_like(self, actor: str, moment_id: str) -> dict[str, Any]:
        actor = self._actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            if not db.execute('SELECT 1 FROM wall_moments WHERE id=? AND withdrawn_at IS NULL', (moment_id,)).fetchone():
                raise PublicWallError('moment_not_found')
            existing = db.execute('SELECT 1 FROM wall_likes WHERE moment_id=? AND actor=?', (moment_id, actor)).fetchone()
            if existing:
                db.execute('DELETE FROM wall_likes WHERE moment_id=? AND actor=?', (moment_id, actor))
            else:
                db.execute('INSERT INTO wall_likes(moment_id,actor,created_at) VALUES(?,?,?)', (moment_id, actor, now))
                moment = db.execute('SELECT author FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
                self._notify(db, moment['author'], actor, 'like', moment_id, None, now)
            db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (moment_id, 'upsert', now))
        return {'ok': True, 'active': not bool(existing), 'moment_id': moment_id, 'author': actor}

    def ensure_like(self, actor: str, moment_id: str, source_key: str | None = None) -> dict[str, Any]:
        actor = self._actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            if source_key:
                row = db.execute("SELECT result_json FROM wall_receipts WHERE source_key=? AND action='like'",
                                 (source_key,)).fetchone()
                if row:
                    return {**json.loads(row['result_json']), 'idempotent_replay': True}
            if not db.execute('SELECT 1 FROM wall_moments WHERE id=? AND withdrawn_at IS NULL', (moment_id,)).fetchone():
                raise PublicWallError('moment_not_found')
            cursor = db.execute('INSERT OR IGNORE INTO wall_likes(moment_id,actor,created_at) VALUES(?,?,?)',
                                (moment_id, actor, now))
            inserted = cursor.rowcount == 1
            if inserted:
                db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (moment_id, 'upsert', now))
                moment = db.execute('SELECT author FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
                self._notify(db, moment['author'], actor, 'like', moment_id, None, now)
            result = {'ok': True, 'active': True, 'moment_id': moment_id, 'author': actor,
                      'existing': not inserted, 'inserted': inserted}
            if source_key:
                db.execute('INSERT INTO wall_receipts(source_key,action,result_json,created_at) VALUES(?,?,?,?)',
                           (source_key, 'like', json.dumps(result), now))
        return result

    def set_like(self, actor: str, moment_id: str, liked: bool) -> dict[str, Any]:
        actor = self._actor(actor)
        if not isinstance(liked, bool):
            raise PublicWallError('invalid_like_state')
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .profile_registration import guard_in
            guard_in(db, actor)
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            if not db.execute('SELECT 1 FROM wall_moments WHERE id=? AND withdrawn_at IS NULL', (moment_id,)).fetchone():
                raise PublicWallError('moment_not_found')
            if liked:
                changed = db.execute('INSERT OR IGNORE INTO wall_likes(moment_id,actor,created_at) VALUES(?,?,?)',
                                     (moment_id, actor, now)).rowcount == 1
            else:
                changed = db.execute('DELETE FROM wall_likes WHERE moment_id=? AND actor=?',
                                     (moment_id, actor)).rowcount == 1
            if changed:
                db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)', (moment_id, 'upsert', now))
                if liked:
                    moment = db.execute('SELECT author FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
                    self._notify(db, moment['author'], actor, 'like', moment_id, None, now)
        return {'ok': True, 'active': liked, 'changed': changed, 'moment_id': moment_id, 'author': actor}

    def withdraw(self, actor: str, moment_id: str, *, owner_override: bool = False) -> dict[str, Any]:
        actor = self._actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .migration import assert_mutable
            assert_mutable(db, moment_id)
            row = db.execute('SELECT author,withdrawn_at FROM wall_moments WHERE id=?', (moment_id,)).fetchone()
            if not row:
                raise PublicWallError('moment_not_found')
            host_moderation = owner_override and actor == 'aning' and row['author'].startswith('visitor:')
            if not self._guardian_can_manage(db, actor, row['author']) and not host_moderation:
                raise PublicWallError('forbidden')
            if row['withdrawn_at'] is None:
                if host_moderation:
                    db.execute('INSERT INTO wall_moderation_actions(id,moment_id,author,moderator,action,created_at) '
                               'VALUES(?,?,?,?,?,?)',
                               ('wm_' + uuid.uuid4().hex, moment_id, row['author'], actor, 'host_removed', now))
                db.execute("UPDATE wall_moments SET mentions_json='[]',content=?,withdrawn_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                           ('', now, now, moment_id))
                db.execute('DELETE FROM wall_comments WHERE moment_id=?', (moment_id,))
                db.execute('DELETE FROM wall_likes WHERE moment_id=?', (moment_id,))
                db.execute('DELETE FROM wall_notifications WHERE moment_id=?', (moment_id,))
                db.execute('DELETE FROM wall_mention_notifications WHERE moment_id=?', (moment_id,))
                db.execute('INSERT INTO wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                           (moment_id, 'withdrawn', now))
            was_moderated = bool(db.execute('SELECT 1 FROM wall_moderation_actions WHERE moment_id=? LIMIT 1',
                                            (moment_id,)).fetchone())
        return {'id': moment_id, 'status': 'withdrawn',
                'reason': 'host_removed' if was_moderated else 'author_withdrawn',
                'message': '此动态已由站主下架' if was_moderated else '此动态已被发布人私密/删除'}

    def changes(self, after: int = 0, limit: int = 100) -> dict[str, Any]:
        after = max(0, int(after))
        limit = max(1, min(100, int(limit)))
        with self._connect() as db:
            rows = db.execute('SELECT seq,moment_id,kind FROM wall_changes WHERE seq>? ORDER BY seq LIMIT ?',
                              (after, limit)).fetchall()
            changes = []
            for row in rows:
                current = self.get_moment(row['moment_id']) if row['kind'] == 'upsert' else None
                changes.append({'seq': row['seq'], 'id': row['moment_id'],
                                'status': 'upsert' if current else 'withdrawn', 'moment': current})
        return {'items': changes, 'next_seq': changes[-1]['seq'] if changes else after}

    def notifications(self, recipient: str, *, unannounced: bool = False,
                      limit: int | None = 50) -> list[dict[str, Any]]:
        recipient = self._actor(recipient)
        where = 'n.recipient=? AND n.read_at IS NULL AND m.withdrawn_at IS NULL'
        if unannounced:
            where += ' AND n.announced_at IS NULL'
        suffix = '' if limit is None else ' LIMIT ?'
        params = (recipient,) if limit is None else (recipient, max(1, min(100, int(limit))))
        with self._connect() as db:
            rows = db.execute('SELECT n.id,n.actor,n.kind,n.moment_id,n.comment_id,n.created_at,'
                              'm.content AS moment_content,c.content AS comment_content,'
                              'CASE WHEN n.comment_id IS NULL THEN m.mentions_json ELSE c.mentions_json END AS mention_targets '
                              'FROM wall_notifications n JOIN wall_moments m ON m.id=n.moment_id '
                              'LEFT JOIN wall_comments c ON c.id=n.comment_id '
                              f'WHERE {where} ORDER BY n.created_at ASC{suffix}', params).fetchall()
            from .mentions import notices, notice_projection
            mentions = notices(db, recipient, unannounced=unannounced, limit=limit)
        combined = sorted([notice_projection(row, recipient) for row in rows] + mentions, key=lambda row: (row['created_at'], row['id']))
        return combined if limit is None else combined[:max(1, min(100, int(limit)))]

    def mark_notifications_read(self, recipient: str, ids: list[str] | None = None,
                                source: str = 'ui', source_id: str = '') -> int:
        recipient = self._actor(recipient)
        bounded = list(dict.fromkeys(ids or []))[:100]
        if ids is not None and not bounded:
            return 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if bounded:
                marks = ','.join('?' for _ in bounded)
                cursor = db.execute(f'UPDATE wall_notifications SET read_at=?,read_source=?,read_source_id=? '
                                    f'WHERE recipient=? AND read_at IS NULL AND id IN ({marks})',
                                    (time.time(), source[:40], source_id[:200], recipient, *bounded))
            else:
                cursor = db.execute('UPDATE wall_notifications SET read_at=?,read_source=?,read_source_id=? '
                                    'WHERE recipient=? AND read_at IS NULL',
                                    (time.time(), source[:40], source_id[:200], recipient))
            from .mentions import claim, notices
            mention_ids = bounded if ids is not None else [row['id'] for row in notices(db, recipient)]
            count = claim(db, recipient, mention_ids, source, source_id)
        return max(0, cursor.rowcount) + count

    def mark_notifications_announced(self, recipient: str, ids: list[str], source: str,
                                     source_id: str) -> int:
        if recipient not in {'aning', 'k'} or not source_id:
            raise PublicWallError('invalid_notification_claim')
        bounded = list(dict.fromkeys(ids))[:100]
        if not bounded:
            return 0
        marks = ','.join('?' for _ in bounded)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute(f'UPDATE wall_notifications SET announced_at=?,announce_source=?,announce_source_id=? '
                                f'WHERE recipient=? AND read_at IS NULL AND announced_at IS NULL AND id IN ({marks})',
                                (time.time(), source[:40], source_id[:200], recipient, *bounded))
            from .mentions import claim
            count = claim(db, recipient, bounded, source, source_id, announcement=True)
        return max(0, cursor.rowcount) + count

    def restore_announcements(self, source_id: str) -> int:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute("UPDATE wall_notifications SET announced_at=NULL,announce_source='',announce_source_id='' "
                                "WHERE announce_source='main_chat' AND announce_source_id=? AND read_at IS NULL",
                                (source_id,))
            mention_cursor = db.execute("UPDATE wall_mention_notifications SET announced_at=NULL,announce_source='',announce_source_id='' "
                                        "WHERE announce_source='main_chat' AND announce_source_id=? AND read_at IS NULL", (source_id,))
        return max(0, cursor.rowcount) + max(0, mention_cursor.rowcount)

    def issue_session(self, token: str, csrf: str, visitor_id: str, key_id: str, expires_at: float) -> None:
        with self._connect() as db:
            db.execute('DELETE FROM wall_sessions WHERE expires_at<?', (time.time(),))
            db.execute('INSERT INTO wall_sessions(token_hash,csrf_hash,visitor_id,key_id,expires_at) VALUES(?,?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), hashlib.sha256(csrf.encode()).hexdigest(),
                        visitor_id, key_id, expires_at))

    def resolve_session(self, token: str, csrf: str | None = None) -> tuple[str, str] | None:
        if not token or len(token) > 256:
            return None
        with self._connect() as db:
            row = db.execute('SELECT visitor_id,key_id,csrf_hash FROM wall_sessions WHERE token_hash=? AND expires_at>?',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row or (csrf is not None and not hmac.compare_digest(
                hashlib.sha256(csrf.encode()).hexdigest(), row['csrf_hash'])):
            return None
        return row['visitor_id'], row['key_id']

    def revoke_session(self, token: str) -> None:
        with self._connect() as db:
            db.execute('DELETE FROM wall_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))


_WALL: PublicWall | None = None


def read_linked_connection(connection_id: str) -> str:
    """Read an existing owner-confirmed contact link without initializing stores."""
    from contextlib import closing
    if connection_id.startswith('visitor:'):
        column, selector, value, prefix = 'linked_friend_id', 'visitor_id', connection_id[8:], 'friend:'
    elif connection_id.startswith('friend:'):
        column, selector, value, prefix = 'visitor_id', 'linked_friend_id', connection_id[7:], 'visitor:'
    else:
        return ''
    path = _WALL.path if _WALL is not None else Path(__file__).resolve().parents[1] / 'events/social_public.db'
    if not path.is_file():
        return ''
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute(f'SELECT {column} FROM wall_contacts WHERE {selector}=?', (value,)).fetchone()
    return prefix + row[0] if row and row[0] else ''


def get_public_wall() -> PublicWall:
    global _WALL
    if _WALL is None:
        _WALL = PublicWall(Path(__file__).resolve().parents[1] / 'events' / 'social_public.db')
        _WALL.initialize()
    return _WALL
