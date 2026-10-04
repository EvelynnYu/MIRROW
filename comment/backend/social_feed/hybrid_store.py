"""Local feed view across the private and public authorities.

The private SQLite file owns private posts.  The public wall SQLite file owns
public posts.  A visibility transition moves the post and its interactions in
one SQLite ATTACH transaction, rather than leaving two authoritative bodies.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from .public_wall import PublicWall, PublicWallError
from .store import SocialFeedError, SocialFeedStore, _decode_feed_cursor, _encode_feed_cursor


class HybridSocialFeedStore:
    def __init__(self, private: SocialFeedStore, public: PublicWall):
        self.private = private
        self.public = public
        self._initialized = False

    def __getattr__(self, name: str):
        return getattr(self.private, name)

    async def shelf_facts(self):
        from .decor_store import _store_at
        from .decor_context import shelf_facts
        path = self.public.path.with_name('social_decor.db')
        return shelf_facts({'home':_store_at(path).home() if path.exists() else {}})

    async def gift_delivery_facts(self, since=0.0):
        from .decor_store import _store_at
        path = self.public.path.with_name('social_decor.db')
        if not path.exists():
            return {'status':'no_receipts','total':0,'new_count':0,'observed_at':time.time(),'items':[]}
        report = _store_at(path).delivery_report(since=since,limit=50)
        items = []
        for row in report['items']:
            identity_name = self.public.registered_name(row['actor'][8:]) if row['actor'].startswith('visitor:') else ''
            nickname = self.public.profile(row['actor']).get('nickname','')
            items.append({'identity_name':identity_name or '未登记访客', 'nickname':nickname,
                          'kind':row['kind'],'gift_name':row['snapshot']['name'],
                          'received_at':row['received'],'new_since_last_observation':row['new']})
        return {**{key:report[key] for key in ('total','new_count','since','observed_at','has_more')},'items':items,
                'source':'本家共域的已发放赠礼收据；最近 50 条，计数覆盖全部收据'}

    async def initialize(self):
        if self._initialized:
            return
        await self.private.initialize()
        self.public.initialize()
        # Additive private-side archive for interactions that cannot satisfy
        # the original two-author CHECK constraints after public -> private.
        with sqlite3.connect(self.private.db_path, timeout=5) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('CREATE TABLE IF NOT EXISTS social_private_public_interactions '
                       '(moment_id TEXT PRIMARY KEY REFERENCES social_moments(id) ON DELETE CASCADE, '
                       'comments_json TEXT NOT NULL, reactions_json TEXT NOT NULL)')
            legacy_public = [(row[0], row[1]) for row in db.execute(
                "SELECT id,author FROM social_moments WHERE visibility='public' ORDER BY created_at,id")]
        # Old builds wrote public rows to the private file before there was a
        # shared wall.  Move them through the same atomic transition.  This is
        # empty on the 2026-09-28 production migration but protects retries.
        for moment_id, owner in legacy_public:
            try:
                self._to_public(moment_id, owner, False)
            except SocialFeedError as exc:
                if str(exc) != 'moment_not_found' or not self.public.get_moment(moment_id):
                    raise
        self._initialized = True

    def _archive(self, moment_id: str) -> tuple[list[dict], list[dict]] | None:
        with sqlite3.connect(self.private.db_path, timeout=5) as db:
            row = db.execute('SELECT comments_json,reactions_json FROM social_private_public_interactions WHERE moment_id=?',
                             (moment_id,)).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def _append_archive(self, item: dict) -> dict:
        archive = self._archive(item['id'])
        if archive:
            merged = {row['id']: row for row in item['comments'] + archive[0]}
            item['comments'] = sorted(merged.values(), key=lambda row: (row['created_at'], row['id']))
            item['reactions'] = sorted(item['reactions'] + archive[1], key=lambda row: row['created_at'])
        from .mentions import project
        for comment in item.get('comments', []):
            if 'mentions_json' in comment:
                project(comment)
        return item

    async def create_moment(self, author: str, content: str, *, source_run_id: str = '',
                            source_activity_id: str = '', source_key: str | None = None,
                            visibility: str = 'private', created_at: float | None = None,
                            mention_actor_ids: list[str] | None = None, forward_ref=None) -> dict[str, Any]:
        await self.initialize()
        from .forwarding import normalize_forward_ref
        try:
            ref = normalize_forward_ref(forward_ref)
        except ValueError as exc:
            raise SocialFeedError(str(exc)) from None
        if ref and not ref['origin']:
            with self.public._connect() as db:
                from .forwarding import same_home_projection
                if same_home_projection(db, ref)['status'] != 'available':
                    raise SocialFeedError('forward_source_unavailable')
        if visibility == 'public':
            await self.initialize()
            try:
                return self.public.create_moment(author, content, created_at=created_at, source_key=source_key,
                                                 mention_actor_ids=mention_actor_ids, forward_ref=forward_ref)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        result = await self.private.create_moment(author, content, source_run_id=source_run_id,
            source_activity_id=source_activity_id, source_key=source_key,
            visibility=visibility, created_at=created_at, mention_actor_ids=mention_actor_ids,
            forward_ref=forward_ref)
        projected = await self.get_moment(result['id'])
        return {**(projected or result), **({'idempotent_replay': True} if result.get('idempotent_replay') else {})}

    async def list_moments(self, *, day=None, limit: int = 50, visibility: str | None = None,
                           cursor: str | None = None, anchor_day=None) -> dict[str, Any]:
        await self.initialize()
        limit = max(1, min(100, int(limit)))
        if visibility not in {None, 'private', 'public'}:
            raise SocialFeedError('invalid_visibility')
        decoded = _decode_feed_cursor(cursor)
        private_page = {'items': [], 'has_more': False}
        if visibility != 'public':
            private_page = await self.private.list_moments(day=day, limit=limit,
                visibility='private', cursor=cursor, anchor_day=anchor_day)
        start = end = None
        if day is not None:
            start, end, _ = self.private._date_bounds(day)
        elif anchor_day is not None and decoded is None:
            _, end, _ = self.private._date_bounds(anchor_day)
        public_page = {'items': [], 'has_more': False}
        if visibility != 'private':
            public_page = self.public.list_moments(limit=limit, before=decoded, start=start, end=end)
        combined = sorted([self._append_archive(item) for item in private_page['items']] + public_page['items'],
                          key=lambda item: (item['created_at'], item['id']), reverse=True)
        items = [self._project_forward(item) for item in combined[:limit]]
        try:
            from lounge_reception.runtime import current_runtime
            from .public_gateway import _decorate
            runtime = current_runtime()
            if runtime:
                items = [_decorate(runtime, item, viewer='k') for item in items]
        except Exception:
            pass
        has_more = (len(combined) > limit or bool(private_page.get('has_more')) or bool(public_page.get('has_more')))
        return {'date': str(day or ''), 'anchor_day': str(anchor_day or ''), 'items': items,
                'total': len(items), 'has_more': has_more,
                'next_cursor': _encode_feed_cursor(items[-1]['created_at'], items[-1]['id']) if has_more and items else None}

    async def get_moment(self, moment_id: str) -> dict[str, Any] | None:
        await self.initialize()
        private = await self.private.get_moment(moment_id)
        item = self._append_archive(private) if private else self.public.get_moment(moment_id)
        if item:
            item = self._project_forward(item)
        if item:
            try:
                from lounge_reception.runtime import current_runtime
                from .public_gateway import _decorate
                runtime = current_runtime()
                if runtime:
                    item = _decorate(runtime, item, viewer='k')
            except Exception:
                pass
        return item

    def _project_forward(self, item: dict[str, Any]) -> dict[str, Any]:
        """Resolve private rows against the current local public wall only."""
        from .forwarding import decode_forward_ref, external_projection, same_home_projection
        ref = decode_forward_ref(item.get('forward_json', ''))
        item.pop('forward_json', None)
        if ref:
            with self.public._connect() as db:
                item['forward'] = same_home_projection(db, ref) if not ref['origin'] else external_projection(ref)
        return item

    async def search_moments(self, query: str, *, limit: int = 10, visibility: str | None = None) -> list[dict[str, Any]]:
        """Bounded local text/author search; never move private rows to the wall."""
        await self.initialize()
        if visibility not in {None,'private','public'}:
            raise SocialFeedError('invalid_visibility')
        query = ' '.join(str(query or '').split())[:80]
        if not query:
            return []
        limit = max(1, min(10, int(limit)))
        author_ids: set[str] = set()
        if query.casefold() in {'k', '站主', 'aning'}:
            author_ids.add('k' if query.casefold() == 'k' else 'aning')
        try:
            from lounge_reception.runtime import current_runtime
            from .public_gateway import _display
            runtime = current_runtime()
            if runtime:
                with self.public._connect() as db:
                    actors = {str(row[0]) for row in db.execute('SELECT actor_id FROM wall_profiles')}
                    actors.update('visitor:' + str(row[0]) for row in db.execute('SELECT visitor_id FROM wall_contacts'))
                for actor in actors:
                    try:
                        if query.casefold() in _display(runtime, actor, viewer='k')['name'].casefold():
                            author_ids.add(actor)
                    except (KeyError, ValueError, OSError):
                        continue
        except (OSError, sqlite3.Error, ValueError):
            pass
        args = (query, query, *sorted(author_ids))
        author_clause = f" OR m.author IN ({','.join('?' for _ in author_ids)})" if author_ids else ''
        with sqlite3.connect(self.private.db_path, timeout=5) as db:
            private_ids = [row[0] for row in db.execute(
                'SELECT m.id FROM social_moments m WHERE m.visibility=? AND '
                '(instr(lower(m.content),lower(?))>0 OR EXISTS('
                'SELECT 1 FROM social_comments c WHERE c.moment_id=m.id '
                'AND instr(lower(c.content),lower(?))>0)' + author_clause + ') '
                'ORDER BY m.created_at DESC,m.id DESC LIMIT ?',
                ('private', *args, limit))]
        with self.public._connect() as db:
            public_ids = [row[0] for row in db.execute(
                'SELECT m.id FROM wall_moments m WHERE m.withdrawn_at IS NULL AND '
                '(instr(lower(m.content),lower(?))>0 OR EXISTS('
                'SELECT 1 FROM wall_comments c WHERE c.moment_id=m.id '
                'AND instr(lower(c.content),lower(?))>0)' + author_clause + ') '
                'ORDER BY m.created_at DESC,m.id DESC LIMIT ?',
                (*args, limit))]
        found = [await self.get_moment(moment_id) for moment_id in [
            *(private_ids if visibility!='public' else []), *(public_ids if visibility!='private' else [])]]
        return sorted((item for item in found if item),
                      key=lambda item: (item['created_at'], item['id']), reverse=True)[:limit]

    def _connect_both(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.private.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('ATTACH DATABASE ? AS public_wall', (str(self.public.path),))
        if db.execute('PRAGMA main.journal_mode').fetchone()[0].lower() != 'delete' or \
           db.execute('PRAGMA public_wall.journal_mode').fetchone()[0].lower() != 'delete':
            db.close()
            raise SocialFeedError('atomic_transfer_unavailable')
        db.execute('BEGIN IMMEDIATE')
        return db

    def _to_public(self, moment_id: str, actor: str, override: bool) -> str:
        db = self._connect_both()
        try:
            moment = db.execute('SELECT * FROM social_moments WHERE id=?', (moment_id,)).fetchone()
            if not moment:
                raise SocialFeedError('moment_not_found')
            owner = moment['author']
            if owner != actor and not (override and actor == 'aning'):
                raise SocialFeedError('forbidden')
            comments = [dict(r) for r in db.execute('SELECT * FROM social_comments WHERE moment_id=? ORDER BY created_at', (moment_id,))]
            likes = [dict(r) for r in db.execute('SELECT * FROM social_reactions WHERE moment_id=? ORDER BY created_at', (moment_id,))]
            archived = db.execute('SELECT comments_json,reactions_json FROM social_private_public_interactions WHERE moment_id=?',
                                  (moment_id,)).fetchone()
            if archived:
                comments.extend(json.loads(archived['comments_json']))
                likes.extend(json.loads(archived['reactions_json']))
            comments = list({row['id']: row for row in comments}.values())
            now = time.time()
            db.execute('INSERT INTO public_wall.wall_moments(id,author,content,created_at,updated_at,mentions_json,forward_json) VALUES(?,?,?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET author=excluded.author,content=excluded.content,'
                       'created_at=excluded.created_at,updated_at=excluded.updated_at,mentions_json=excluded.mentions_json,forward_json=excluded.forward_json,withdrawn_at=NULL,revision=revision+1',
                       (moment_id, owner, moment['content'], moment['created_at'], now, moment['mentions_json'], moment['forward_json']))
            # Two passes keep reply FKs valid even when old timestamps tie.
            for comment in comments:
                db.execute('INSERT INTO public_wall.wall_comments(id,moment_id,author,content,reply_to_id,created_at,mentions_json) '
                           'VALUES(?,?,?,?,?,?,?)', (comment['id'], moment_id, comment['author'], comment['content'],
                           None, comment['created_at'], comment.get('mentions_json') or json.dumps(comment.get('mention_actor_ids', []))))
            for comment in comments:
                if comment.get('reply_to_id'):
                    db.execute('UPDATE public_wall.wall_comments SET reply_to_id=? WHERE id=?',
                               (comment['reply_to_id'], comment['id']))
            for like in likes:
                db.execute('INSERT OR IGNORE INTO public_wall.wall_likes(moment_id,actor,created_at) VALUES(?,?,?)',
                           (moment_id, like.get('author', like.get('actor')), like['created_at']))
            for notice in db.execute('SELECT * FROM social_notifications WHERE moment_id=?', (moment_id,)):
                db.execute('INSERT INTO public_wall.wall_notifications '
                           '(id,recipient,actor,kind,moment_id,comment_id,created_at,announced_at,'
                           'announce_source,announce_source_id,read_at,read_source,read_source_id) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           tuple(notice[key] for key in ('id','recipient','actor','kind','moment_id','comment_id',
                               'created_at','announced_at','announce_source','announce_source_id','read_at',
                               'read_source','read_source_id')))
            db.execute('INSERT INTO public_wall.wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                       (moment_id, 'upsert', now))
            db.execute('DELETE FROM social_moments WHERE id=?', (moment_id,))
            db.commit()
            return owner
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _to_private(self, moment_id: str, actor: str, override: bool) -> str:
        db = self._connect_both()
        try:
            moment = db.execute('SELECT * FROM public_wall.wall_moments WHERE id=? AND withdrawn_at IS NULL',
                                (moment_id,)).fetchone()
            if not moment:
                raise SocialFeedError('moment_not_found')
            owner = moment['author']
            if owner not in {'aning', 'k'} or (owner != actor and not (override and actor == 'aning')):
                raise SocialFeedError('forbidden')
            comments = [dict(r) for r in db.execute('SELECT * FROM public_wall.wall_comments WHERE moment_id=? ORDER BY created_at', (moment_id,))]
            likes = [{'author': r['actor'], 'reaction': 'like', 'created_at': r['created_at']}
                     for r in db.execute('SELECT * FROM public_wall.wall_likes WHERE moment_id=?', (moment_id,))]
            now = time.time()
            db.execute("INSERT INTO social_moments(id,author,content,visibility,created_at,mentions_json,forward_json) VALUES(?,?,?,'private',?,?,?)",
                       (moment_id, owner, moment['content'], moment['created_at'], moment['mentions_json'], moment['forward_json']))
            db.execute('INSERT INTO social_private_public_interactions(moment_id,comments_json,reactions_json) VALUES(?,?,?)',
                       (moment_id, json.dumps(comments, ensure_ascii=False), json.dumps(likes, ensure_ascii=False)))
            # Clear *all* public body-bearing rows before the transaction is
            # committed; change feed clients receive only the tombstone.
            db.execute("UPDATE public_wall.wall_moments SET mentions_json='[]',content='',forward_json='', withdrawn_at=?, updated_at=?, revision=revision+1 WHERE id=?",
                       (now, now, moment_id))
            db.execute('DELETE FROM public_wall.wall_comments WHERE moment_id=?', (moment_id,))
            db.execute('DELETE FROM public_wall.wall_likes WHERE moment_id=?', (moment_id,))
            db.execute('DELETE FROM public_wall.wall_notifications WHERE moment_id=?', (moment_id,))
            db.execute('DELETE FROM public_wall.wall_mention_notifications WHERE moment_id=?', (moment_id,))
            db.execute('INSERT INTO public_wall.wall_changes(moment_id,kind,created_at) VALUES(?,?,?)',
                       (moment_id, 'withdrawn', now))
            db.commit()
            return owner
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def set_moment_visibility(self, moment_id: str, actor: str, visibility: str,
                                    *, allow_owner_override: bool = False, source_key: str | None = None) -> dict[str, Any]:
        await self.initialize()
        if visibility not in {'private', 'public'}:
            raise SocialFeedError('invalid_visibility')
        current = await self.get_moment(moment_id)
        if current is None:
            raise SocialFeedError('moment_not_found')
        previous = current['visibility']
        if current['author'] != actor and not (allow_owner_override and actor == 'aning'):
            raise SocialFeedError('forbidden')
        if previous != visibility:
            if visibility == 'public':
                self._to_public(moment_id, actor, allow_owner_override)
            else:
                self._to_private(moment_id, actor, allow_owner_override)
        return {'ok': True, 'action': 'set_visibility', 'moment_id': moment_id,
                'author': current['author'], 'visibility': visibility,
                'previous_visibility': previous, 'changed': previous != visibility}

    async def delete_moment(self, moment_id: str, actor: str, *, source_key: str | None = None,
                            allow_owner_override: bool = False):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                self.public.withdraw(actor, moment_id, owner_override=allow_owner_override)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
            return {'ok': True, 'action': 'delete', 'moment_id': moment_id, 'author': actor, 'deleted': True}
        return await self.private.delete_moment(moment_id, actor, source_key=source_key)

    async def edit_moment(self, moment_id: str, actor: str, content: str, revision: int = 1):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                return self.public.edit_moment(actor, moment_id, content, revision)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        content = self.private._content(content, limit=1200)
        with sqlite3.connect(self.private.db_path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT author FROM social_moments WHERE id=?', (moment_id,)).fetchone()
            if not row:
                raise SocialFeedError('moment_not_found')
            if row[0] != actor:
                raise SocialFeedError('forbidden')
            db.execute('UPDATE social_moments SET content=? WHERE id=?', (content, moment_id))
        return await self.get_moment(moment_id)

    async def add_comment(self, moment_id: str, author: str, content: str, *,
                          reply_to_id: str | None = None, source_key: str | None = None,
                          created_at: float | None = None, mention_actor_ids: list[str] | None = None):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                return self.public.add_comment(author, moment_id, content, reply_to_id, source_key,
                                               mention_actor_ids=mention_actor_ids)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        archive = self._archive(moment_id)
        if archive and reply_to_id and any(row['id'] == reply_to_id for row in archive[0]):
            from .mentions import normalise, encode
            author = self.private._author(author)
            try:
                mentions = normalise(mention_actor_ids, private=True)
            except ValueError as exc:
                raise SocialFeedError(str(exc)) from None
            content = self.public._text(content, 600)
            key = self.private._normalise_source_key(source_key)
            with sqlite3.connect(self.private.db_path, timeout=5) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                if key:
                    receipt = db.execute('SELECT action,result_json FROM social_action_receipts WHERE source_key=?',
                                         (key,)).fetchone()
                    if receipt:
                        if receipt[0] != 'comment':
                            raise SocialFeedError('source_key_conflict')
                        return {**json.loads(receipt[1]), 'idempotent_replay': True}
                row = db.execute('SELECT comments_json FROM social_private_public_interactions WHERE moment_id=?', (moment_id,)).fetchone()
                if not row:
                    raise SocialFeedError('moment_not_found')
                result = {'id': 'sc_' + uuid.uuid4().hex, 'moment_id': moment_id,
                          'author': author, 'content': content, 'reply_to_id': reply_to_id,
                          'created_at': created_at or time.time(), 'mention_actor_ids': mentions}
                comments = json.loads(row[0]); comments.append(result)
                db.execute('UPDATE social_private_public_interactions SET comments_json=? WHERE moment_id=?',
                           (json.dumps(comments, ensure_ascii=False), moment_id))
                # The archive preserves a historical parent; the real row supplies
                # a foreign-key-backed notice body and deletion lifecycle.
                db.execute('INSERT INTO social_comments(id,moment_id,author,content,reply_to_id,created_at,mentions_json) '
                           'VALUES(?,?,?,?,NULL,?,?)',
                           (result['id'], moment_id, author, content, result['created_at'], encode(mentions)))
                owner = db.execute('SELECT author FROM social_moments WHERE id=?', (moment_id,)).fetchone()[0]
                parent_author = next(entry['author'] for entry in comments if entry['id'] == reply_to_id)
                recipients = ({owner, parent_author} | set(mentions)) & {'aning', 'k'} - {author}
                for recipient in recipients:
                    db.execute('INSERT INTO social_notifications(id,recipient,actor,kind,moment_id,comment_id,created_at) '
                               "VALUES(?,?,?,'comment',?,?,?)",
                               ('sn_' + uuid.uuid4().hex, recipient, author, moment_id, result['id'], result['created_at']))
                if key:
                    db.execute('INSERT INTO social_action_receipts(source_key,action,result_json,created_at) VALUES(?,?,?,?)',
                               (key, 'comment', json.dumps(result, ensure_ascii=False), time.time()))
            return result
        return await self.private.add_comment(moment_id, author, content,
            reply_to_id=reply_to_id, source_key=source_key, created_at=created_at, mention_actor_ids=mention_actor_ids)

    async def delete_comment(self, moment_id: str, comment_id: str, actor: str):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                return self.public.delete_comment(actor, moment_id, comment_id)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        with sqlite3.connect(self.private.db_path, timeout=5) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT comments_json FROM social_private_public_interactions WHERE moment_id=?',
                             (moment_id,)).fetchone()
            if row:
                comments = json.loads(row[0])
                target = next((item for item in comments if item['id'] == comment_id), None)
                if target:
                    if actor != 'aning' and target['author'] != actor:
                        raise SocialFeedError('forbidden')
                    comments = [item for item in comments if item['id'] != comment_id]
                    for item in comments:
                        if item.get('reply_to_id') == comment_id:
                            item['reply_to_id'] = None
                    db.execute('UPDATE social_private_public_interactions SET comments_json=? WHERE moment_id=?',
                               (json.dumps(comments, ensure_ascii=False), moment_id))
                    db.execute('DELETE FROM social_comments WHERE id=? AND moment_id=?', (comment_id, moment_id))
                    db.commit()
                    return {'id': comment_id, 'moment_id': moment_id, 'status': 'deleted'}
        return await self.private.delete_comment(moment_id, comment_id, actor)

    async def toggle_like(self, moment_id: str, author: str):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                return self.public.toggle_like(author, moment_id)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        archive = self._archive(moment_id)
        if archive:
            with sqlite3.connect(self.private.db_path, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT reactions_json FROM social_private_public_interactions WHERE moment_id=?', (moment_id,)).fetchone()
                reactions = json.loads(row[0]) if row else []
                existing = any(r['author'] == author for r in reactions)
                reactions = [r for r in reactions if r['author'] != author]
                if not existing:
                    reactions.append({'author': author, 'reaction': 'like', 'created_at': time.time()})
                db.execute('UPDATE social_private_public_interactions SET reactions_json=? WHERE moment_id=?',
                           (json.dumps(reactions), moment_id))
            return {'ok': True, 'active': not existing, 'moment_id': moment_id, 'author': author}
        return await self.private.toggle_like(moment_id, author)

    async def ensure_like(self, moment_id: str, author: str, *, source_key: str | None = None):
        await self.initialize()
        if self.public.get_moment(moment_id):
            try:
                return self.public.ensure_like(author, moment_id, source_key)
            except PublicWallError as exc:
                raise SocialFeedError(str(exc)) from exc
        if self._archive(moment_id):
            key = self.private._normalise_source_key(source_key)
            with sqlite3.connect(self.private.db_path, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                if key:
                    receipt = db.execute('SELECT action,result_json FROM social_action_receipts WHERE source_key=?',
                                         (key,)).fetchone()
                    if receipt:
                        if receipt[0] != 'like':
                            raise SocialFeedError('source_key_conflict')
                        return {**json.loads(receipt[1]), 'idempotent_replay': True}
                row = db.execute('SELECT reactions_json FROM social_private_public_interactions WHERE moment_id=?',
                                 (moment_id,)).fetchone()
                reactions = json.loads(row[0]) if row else []
                existing = any(r['author'] == author for r in reactions)
                if not existing:
                    reactions.append({'author': author, 'reaction': 'like', 'created_at': time.time()})
                    db.execute('UPDATE social_private_public_interactions SET reactions_json=? WHERE moment_id=?',
                               (json.dumps(reactions), moment_id))
                result = {'ok': True, 'active': True, 'moment_id': moment_id,
                          'author': author, 'existing': existing, 'inserted': not existing}
                if key:
                    db.execute('INSERT INTO social_action_receipts(source_key,action,result_json,created_at) VALUES(?,?,?,?)',
                               (key, 'like', json.dumps(result), time.time()))
            return result
        return await self.private.ensure_like(moment_id, author, source_key=source_key)

    async def commit_daily_summary_decision(self, target_date, action: str, content: str = '',
                                            visibility: str = 'private', reason: str = ''):
        await self.initialize()
        result = await self.private.commit_daily_summary_decision(target_date, action, content, visibility, reason)
        if result.get('moment_id') and result.get('visibility') == 'public':
            if not self.public.get_moment(result['moment_id']):
                await self.initialize()
                self._to_public(result['moment_id'], 'k', False)
        return result

    async def unread_notifications(self, recipient: str, *, limit: int | None = 50):
        await self.initialize()
        private = await self.private.unread_notifications(recipient, limit=limit)
        public = self.public.notifications(recipient, limit=limit)
        try:
            from lounge_reception.runtime import current_runtime
            from .public_gateway import _display
            runtime = current_runtime()
            if runtime:
                for notice in public:
                    notice['actor_name'] = _display(runtime, notice['actor'], viewer=recipient)['name']
        except Exception:
            pass
        combined = sorted(private + public, key=lambda row: (row['created_at'], row['id']))
        return combined if limit is None else combined[:max(1, min(100, int(limit)))]

    async def unannounced_notifications(self, recipient: str, *, limit: int = 50):
        await self.initialize()
        private = await self.private.unannounced_notifications(recipient, limit=limit)
        public = self.public.notifications(recipient, unannounced=True, limit=limit)
        return sorted(private + public, key=lambda row: (row['created_at'], row['id']))[:max(1, min(100, int(limit)))]

    async def mark_notifications_read(self, recipient: str, notification_ids=None,
                                      *, read_source: str = 'ui', read_source_id: str = ''):
        await self.initialize()
        ids = None if notification_ids is None else list(dict.fromkeys(notification_ids))[:100]
        private = await self.private.mark_notifications_read(recipient, ids,
            read_source=read_source, read_source_id=read_source_id)
        public = self.public.mark_notifications_read(recipient, ids, read_source, read_source_id)
        return private + public

    def mark_notifications_announced_sync(self, recipient: str, notification_ids,
                                          *, announce_source: str = 'main_chat',
                                          announce_source_id: str = '') -> int:
        self.public.initialize()
        ids = list(dict.fromkeys(notification_ids))[:100]
        private = self.private.mark_notifications_announced_sync(recipient, ids,
            announce_source=announce_source, announce_source_id=announce_source_id)
        public = self.public.mark_notifications_announced(recipient, ids,
            announce_source, announce_source_id)
        return private + public

    def mark_notifications_read_sync(self, recipient: str, notification_ids,
                                     *, read_source: str = 'main_chat', read_source_id: str = '') -> int:
        self.public.initialize()
        ids = list(dict.fromkeys(notification_ids))[:100]
        private = self.private.mark_notifications_read_sync(recipient, ids,
            read_source=read_source, read_source_id=read_source_id)
        public = self.public.mark_notifications_read(recipient, ids, read_source, read_source_id)
        return private + public

    async def restore_main_chat_announcements(self, announce_source_id: str) -> int:
        await self.initialize()
        private = await self.private.restore_main_chat_announcements(announce_source_id)
        return private + self.public.restore_announcements(announce_source_id)

    async def restore_main_chat_notifications(self, read_source_id: str) -> int:
        return await self.restore_main_chat_announcements(read_source_id)
