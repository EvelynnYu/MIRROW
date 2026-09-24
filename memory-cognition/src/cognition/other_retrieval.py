"""Semantic impressions, using the existing embedder and recalled memories."""
from . import books

_embedder = None
_vectors = {}
_facet_vectors = {}


def set_embedder(embedder):
    global _embedder
    _embedder = embedder
    refresh()


def refresh():
    global _vectors, _facet_vectors
    if _embedder is None:
        return
    entries = books.catalog('other')
    from .other_facets import split
    old_facets = _facet_vectors
    _facet_vectors = {e['name']: (e['revision'], old_facets[e['name']][1]
        if e['name'] in old_facets and old_facets[e['name']][0] == e['revision']
        else {title: _embedder.embed_text(f'{title} {body}') for title, body in split(e['body'])[1]}) for e in entries}
    previous = _vectors
    _vectors = {e['name']: (e['revision'], previous[e['name']][1] if e['name'] in previous and previous[e['name']][0] == e['revision']
                            else _embedder.embed_text(f"{e['name']} {' '.join(e['keywords'])} {e['body']}")) for e in entries}


def selected_entries(query, interlocutors, *, expand_entities=True):
    from .other_book import entities, effective
    known = entities()
    mentioned = set(interlocutors)
    text = query.casefold()
    from .entity_names import mentions, names
    for sid, entity in known.items():
        if any(mentions(text, term) for term in names(entity)):
            mentioned.add(sid)
    items = [e for e in books.catalog('other') if effective(e)]
    scores = {}
    if expand_entities and query and _embedder is not None and _vectors:
        import numpy as np
        q = _embedder.embed_text(query)
        for e in items:
            cached = _vectors.get(e['name'])
            if not cached or cached[0] != e['revision']:
                continue
            vector = cached[1]
            score = float(np.dot(q, vector) / (np.linalg.norm(q) * np.linalg.norm(vector) + 1e-8))
            if score > .6: scores[e['name']] = score
    selected = [e for e in items if e['subject_id'] in mentioned]
    # Some explicit non-chat consumers (for example the cognition lounge) use
    # semantic discovery.  The main private-chat path disables this expansion:
    # unrelated people must not enter Agent's context merely because a recalled
    # event happens to mention them.
    if expand_entities:
        selected += sorted(
            [e for e in items if e not in selected and e['name'] in scores],
            key=lambda e: -scores[e['name']],
        )[:3]
    return selected


def recalled_query(user_message='', memories=None):
    parts = [user_message]
    for memory in memories or []:
        text = memory.get('content', '') if isinstance(memory, dict) else getattr(memory, 'content', '')
        if isinstance(text, str): parts.append(text)
    return '\n'.join(parts)
