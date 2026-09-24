"""Relevant prior clues, not a queue to drain or independent new evidence."""
import json
from . import maintenance, other_book
from .entity_names import mentions, names


def related(evidence, session_id, source_date):
    text = '\n'.join(e['text'] for e in evidence)
    selected = {'human'} if any(e.get('speaker') == 'human' for e in evidence) else set()
    selected.update(sid for sid, entity in other_book.entities().items() if any(mentions(text,n) for n in names(entity)))
    result, seen = [], set()
    paths = sorted((maintenance.folder()/'other_runs').glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
    for path in paths:
        state=json.loads(path.read_text('utf-8'))
        if state.get('session_id') != session_id or state.get('source_date','') >= source_date or state.get('status') != 'completed': continue
        analysis=state.get('analysis',{}); refs={e['id']:e for e in analysis.get('evidence',[])}
        for item in analysis.get('observations',[]):
            key=(item.get('subject_id'),item.get('text'))
            if key in seen or key[0] not in selected: continue
            source=[refs[r] for r in item.get('reference_ids',[]) if r in refs]
            if not source: continue
            seen.add(key); result.append({**item,'source_date':state['source_date'],'evidence':source})
            if len(result)>=12: return result
    return result
