"""One entity, an overview and optional independently retrieved impressions."""
import re

FACETS = ('关系认知', '性格认知', '行事习惯', '兴趣偏好', '表达方式')


def split(body):
    parts = re.split(r'^##\s+(.+?)\s*$', body.strip(), flags=re.M)
    return parts[0].strip(), [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def validate(body):
    from .books import BookError
    overview, facets = split(body)
    if not overview or len(overview) > 240 or '\n' in overview:
        raise BookError('他者书先用一句简洁的话表达总体印象')
    titles = [title for title, _ in facets]
    if len(set(titles)) != len(titles) or any(not text or len(text) > 240 or '\n' in text or len(title) > 24 for title, text in facets):
        raise BookError('每个认识维度用一句话，不重复或填写空栏目')


def project(entry, query, embedder=None, vectors=None):
    overview, facets = split(entry['body'])
    if not facets or not query:
        return overview
    scores = {}
    q = embedder.embed_text(query) if embedder is not None and vectors else None
    for title, text in facets:
        if title.casefold() in query.casefold():
            scores[title] = 2.0
        elif q is not None and title in vectors:
            import numpy as np
            v = vectors[title]
            score = float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-8))
            if score > .6:
                scores[title] = score
    selected = set(sorted(scores, key=scores.get, reverse=True)[:2])
    return overview + ''.join(f'\n\n## {title}\n{text}' for title, text in facets if title in selected)
