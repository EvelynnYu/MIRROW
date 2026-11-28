"""Hand newly captured notification pages to the decision already reading that thread."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import json

from . import images
from wander_manager.xhs_gallery_review import observations, prepare_pages
from xhs_publish.store import now

_active = ContextVar('xhs_interaction_gallery', default=None)


@contextmanager
def collect():
    batch = DecisionGallery()
    token = _active.set(batch)
    try:
        yield batch
    finally:
        _active.reset(token)


def active():
    return _active.get()


class DecisionGallery:
    def __init__(self):
        self.groups = []
        self.records = {}

    def stage(self, store, item, target, captured):
        frames = captured['frames']
        pages = prepare_pages([{'label':f"image_{f['page']}_of_{f['total']}",
                                'screenshot_base64':base64.b64encode(f['pixels']).decode('ascii')} for f in frames])
        identity = images.key(item, target)
        record = {'id':identity, 'interaction_id':item['id'], 'session_id':item['session_id'],
                  'created_at':now(), 'version':'decision-gallery-v1', 'state':'awaiting_decision',
                  'capture_complete':captured.get('capture_complete') is True, 'capture_status':captured['status'],
                  'pages':[{'page':f['page'], 'total':f['total'], 'state':'awaiting_decision',
                            'captured_at':f['captured_at']} for f in frames]}
        self.groups.append((identity, target['note_title'], pages))
        self.records[identity] = (store, record)
        return images.project(record)

    def claim(self):
        """Reserve all images for this model dispatch, after context and source selection."""
        if not self.records:
            return
        store, _ = next(iter(self.records.values()))
        with store.connect() as db:
            for identity, (_, record) in self.records.items():
                record['state'] = 'decision_dispatched'
                won = db.execute('INSERT OR IGNORE INTO image_reads VALUES (?,?,?,?,?)',
                    (identity, record['interaction_id'], record['session_id'], record['state'],
                     json.dumps(record,ensure_ascii=False))).rowcount
                if not won:
                    raise ValueError('gallery_already_claimed')

    def record_xhs_page_reading(self, identity, readings):
        store, record = self.records[identity]
        for page, reading in zip(record['pages'], readings, strict=True):
            page.update(state='analyzed', observation=reading['observation'])
        record['state'] = 'completed'
        with store.connect() as db:
            db.execute('UPDATE image_reads SET state=?,record=? WHERE id=? AND state=?',
                       ('completed', json.dumps(record,ensure_ascii=False), identity, 'decision_dispatched'))

    def accept(self, raw, materials):
        pages = [page for _, _, group in self.groups for page in group]
        readings = iter(observations(raw, pages))
        for identity, _, group in self.groups:
            self.record_xhs_page_reading(identity, [next(readings) for _ in group])
            _, record = self.records[identity]
            for item in materials:
                if item['id'] == record['interaction_id']:
                    item['image_reading'] = images.project(record)
        self.groups.clear()
