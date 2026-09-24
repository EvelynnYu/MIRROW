"""Shared read cursors, separate from authoritative book content."""
import json
from . import books


def cursors():
    path = books.ROOT / 'data/cognition_read.json'
    value = json.loads(path.read_text('utf-8')) if path.exists() else {}
    if not isinstance(value, dict): raise books.BookError('阅读状态暂时不可用')
    return value


def key(entry):
    return entry['domain'] + ':' + entry['entry_id']


def decorate(entries):
    seen = cursors()
    return [{**e, 'unread': bool(e['updated_at']) and seen.get(key(e)) != e['revision']} for e in entries]


def counts():
    return {domain: sum(e['unread'] for e in decorate(books.catalog(domain))) for domain in books.DOMAINS}


def mark(domain, name, revision):
    with books.LOCK:
        entry = books._read(books.locate(domain, name), domain)
        if not revision or entry['revision'] != revision:
            raise books.RevisionConflict('词条已更新，未清除新版本的未读标记')
        seen = cursors()
        seen[key(entry)] = revision
        books.atomic_text(books.ROOT / 'data/cognition_read.json', json.dumps(seen, ensure_ascii=False))
        return {'read': True, 'revision': revision}
