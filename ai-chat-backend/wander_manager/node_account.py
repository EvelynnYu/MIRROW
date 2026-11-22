"""Known domain-authored accounts; external summaries are not AI's feelings."""
from copy import deepcopy


def account_material(activity_type, payload):
    view = deepcopy(payload)
    path = None
    kind = '本节点已有自述'
    if activity_type == 'browse_bookmarks':
        path = ('reflection',)
    elif activity_type == 'memory_fetch' and payload.get('iceberg_recall') is True:
        path = ('reflection',)
        reflection = payload.get('reflection')
        if isinstance(reflection, str) and view.get('content') == reflection[:300]:
            view.pop('content')
    elif activity_type == 'keyword_expansion':
        path = ('expansion',)
        if payload.get('essay_version') == 1:
            kind = '本节点写成的随笔作品；故事情节不等于现实经历'
    elif activity_type == 'play_game' and payload.get('action') == 'save_chapter_reflection':
        path = ('action_params', 'reflection')
    elif activity_type == 'self_reflection':
        path = ('overall_reflection',)
    if path is None:
        return view, None
    parent = view
    for key in path[:-1]:
        parent = parent.get(key)
        if not isinstance(parent, dict):
            return view, None
    text = parent.get(path[-1])
    if not isinstance(text, str) or not text.strip():
        return view, None
    parent.pop(path[-1])
    return view, {'text': text.strip(), 'kind': kind, 'source_field': '.'.join(path)}
