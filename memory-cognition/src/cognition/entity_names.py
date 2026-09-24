"""One local name vocabulary per stable entity, independent of connection authority."""
import hashlib
import json
import re
import unicodedata
from . import books


def normalize(value):
    return unicodedata.normalize('NFKC', str(value)).strip().casefold()


def names(entity):
    return list(dict.fromkeys([entity['name'], *entity.get('aliases', [])]))


def resolve_name(value, known=None):
    from .other_book import entities
    known = entities() if known is None else known
    matches = [sid for sid, e in known.items() if normalize(value) in {normalize(n) for n in names(e)}]
    if len(matches) > 1:
        raise books.BookError('名称对应多个实体，请使用实体编号确认；未自动合并')
    return matches[0] if matches else None


def mentions(query, term):
    text, word = normalize(query), normalize(term)
    if not word:
        return False
    # Latin names must not match inside longer words (e.g. Cove / discover).
    pattern = re.escape(word)
    if word[0].isascii() and word[0].isalnum():
        pattern = r'(?<![a-z0-9_])' + pattern
    if word[-1].isascii() and word[-1].isalnum():
        pattern += r'(?![a-z0-9_])'
    return re.search(pattern, text) is not None


def revision(entity):
    return hashlib.sha256(json.dumps(entity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_aliases(name, aliases, known, owner=None):
    if not isinstance(aliases, list) or len(aliases) > 16 or any(not isinstance(a, str) for a in aliases):
        raise books.BookError('别名最多 16 个，每个需为文字')
    from visitor_lounge.content_safety import detect_credential_category
    result, seen = [], {normalize(name)}
    for raw in aliases:
        alias = raw.strip()
        if not alias or normalize(alias) in seen:
            continue
        if len(alias) > 64 or any(ord(c) < 32 for c in alias) or normalize(alias) in {'agent', '镜影'} or detect_credential_category(alias):
            raise books.BookError('别名无效：请填写不含凭据或控制字符的人物称呼（最多 64 字）')
        if any(sid != owner and normalize(alias) in {normalize(n) for n in names(e)} for sid, e in known.items()):
            raise books.BookError('该别名已属于另一实体，未保存；请先核对身份')
        seen.add(normalize(alias)); result.append(alias)
    return result


def update(identifier, value):
    from .other_book import entities
    if not isinstance(value, dict) or set(value) != {'aliases', 'preferred_name', 'revision'}:
        raise books.BookError('请提供别名、常用称呼和当前版本')
    with books.LOCK:
        known = entities()
        if identifier not in known or identifier in {'agent','human','peer'}:
            raise books.BookError('请选择已登记的外部实体')
        current = known[identifier]
        if value['revision'] != revision(current):
            raise books.RevisionConflict('人物称呼已变化，请刷新后重新编辑')
        aliases = validate_aliases(current['name'], value['aliases'], known, identifier)
        preferred = value['preferred_name']
        if not isinstance(preferred, str) or preferred.strip() not in ['', current['name'], *aliases]:
            raise books.BookError('常用称呼需要是主名称或已填写的别名；也可留空')
        saved = {**current, 'aliases':aliases, 'preferred_name':preferred.strip()}
        path = books.ROOT / 'data/other_book_entities.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data[identifier] = saved
        books.atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return {**saved, 'revision':revision(saved)}


def expand_query(query, entity_ids=()):
    from .other_book import entities
    known = entities()
    selected = set(entity_ids)
    for sid, e in known.items():
        if any(mentions(query, n) for n in names(e)):
            selected.add(sid)
    # Append only configured synonyms; do not replace historical words or infer family members.
    terms = list(dict.fromkeys(n for sid, e in known.items() if sid in selected for n in names(e)
                              if not mentions(query, n)))
    return query + ('\n' + ' '.join(terms) if terms else '')
