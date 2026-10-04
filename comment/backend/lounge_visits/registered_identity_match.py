"""Exact, owner-reviewable other-book matches for a social registration name."""

from __future__ import annotations

import unicodedata


def _key(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def candidates(registered_name: str, known: dict, visitor_kind: str) -> list[dict]:
    target = _key(registered_name) if registered_name else ''
    if not target:
        return []
    expected_type = 'human' if visitor_kind == 'human' else 'silicon'
    matches = []
    for entity in known.values():
        if entity.get('id') in {'aning', 'k'}:
            continue
        names = [('name', entity.get('name', '')), *[('alias', alias) for alias in entity.get('aliases', [])]]
        hit = next((source for source, name in names if isinstance(name, str) and _key(name) == target), None)
        if hit:
            matches.append({'id': entity['id'], 'name': entity['name'], 'type': entity.get('type', 'other'),
                            'matched_by': hit})
    return sorted(matches, key=lambda item: (item['type'] != expected_type, item['matched_by'] != 'name', item['name'], item['id']))
