"""Bounded primary-message lookup, read-only and scoped to one private session."""
import asyncio
import sqlite3
from contextlib import closing
from . import books
from .maintenance_sources import sources


def lookup(query, *, session_id, source_date):
    entity_id = query.get('entity_id') if isinstance(query, dict) else None
    if isinstance(query, dict):
        query = query.get('query', '')
    if not isinstance(query, str) or not query.strip() or len(query) > 160:
        raise books.BookError('历史查询需为简短具体话题')
    terms = list(dict.fromkeys(query.split()))[:6]
    db = books.ROOT / 'events/event_chronicle.db'
    if not db.exists(): return []
    conditions = ' OR '.join("content LIKE ? ESCAPE '\\'" for _ in terms)
    escaped = ['%' + t.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for t in terms]
    entity_clause, entity_args = '', []
    if entity_id and entity_id != 'human':
        from .other_book import entities
        from .entity_names import names
        entity = entities().get(entity_id)
        if entity:
            entity_terms = names(entity)
            entity_clause = ' AND (' + ' OR '.join("content LIKE ? ESCAPE '\\'" for _ in entity_terms) + ')'
            entity_args = ['%' + t.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for t in entity_terms]
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f'''SELECT message_id, role, content, timestamp, active_date,
            is_wander,is_sentinel,is_reminder,event_type FROM conversation_messages
            WHERE session_id=? AND active_date<? AND role IN ('user','assistant')
            AND COALESCE(is_wander,0)=0 AND COALESCE(is_sentinel,0)=0
            AND COALESCE(is_reminder,0)=0 AND COALESCE(event_type,'')='' AND ({conditions}){entity_clause}
            ORDER BY active_date DESC,timestamp DESC LIMIT 40''', [session_id, source_date, *escaped, *entity_args]).fetchall()
    results = sources([dict(r) for r in rows])
    days = {r['message_id']:r['active_date'] for r in rows}
    results = [{**e, 'active_date': days.get(e['id'], '')} for e in results]
    selected, size = [], 0
    for row in results:
        # Never split a statement; report only complete evidence units.
        if len(selected) >= 12 or size + len(row['text']) > 14000: continue
        selected.append(row); size += len(row['text'])
    return selected


def searcher(session_id, source_date):
    async def search(query):
        return await asyncio.to_thread(lookup, query, session_id=session_id, source_date=source_date)
    return search
