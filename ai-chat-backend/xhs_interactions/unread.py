"""A native badge is a count observed before entry. The ledger owns progress."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re


_visit = ContextVar('xhs_visible_unread', default=None)


@contextmanager
def watch_visit():
    observed = _visit.get()
    if observed is None:
        observed = {}
    token = _visit.set(observed)
    try:
        yield observed
    finally:
        _visit.reset(token)


def observe_labels(descriptions):
    visit = _visit.get()
    if visit is None:
        return
    clear = False
    for desc in descriptions:
        match = re.fullmatch(r'消息[，,]\s*(?:(\d+\+?)条)?未读', desc or '')
        if match:
            visit['notice'] = '小红书消息页有' + ((match[1] + '条') if match[1] else '') + '未读。'
            return
        clear |= desc == '消息'
    if clear:
        visit.pop('notice', None)


def comment_badge(root):
    """Only the comment category's own accessibility node and its children."""
    candidates = [n for n in root.iter('node') if re.fullmatch(
        r'评论和[@＠](?:按钮|[，,].*未读(?:按钮)?)', n.get('content-desc', ''))]
    if len(candidates) != 1:
        return None
    category = candidates[0]
    match = re.search(r'(\d+)(\+?)条未读', category.get('content-desc', ''))
    if match:
        return int(match[1]), bool(match[2])
    for node in category.iter('node'):
        label = node.get('text', '')
        if re.fullmatch(r'\d+\+?', label):
            return int(label.rstrip('+')), label.endswith('+')
    if '未读' in category.get('content-desc', ''):
        return 1, True
    return 0, False


class InboxWindow:
    def __init__(self, store, session, account):
        self.store, self.session, self.account = store, session, account

    def _save(self, db, value):
        db.execute('INSERT OR REPLACE INTO inbox_windows VALUES (?,?,?)',
                   (self.session, self.account['account_id'], json.dumps(value)))

    def before_entry(self, badge):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM inbox_windows WHERE session=? AND account=?',
                             (self.session, self.account['account_id'])).fetchone()
            value = json.loads(row[0]) if row else {}
            if badge and badge[0]:
                count, lower_bound = badge
                if not value:
                    value = {'expected': count, 'refs': [], 'pages': 0, 'entered': False}
                elif value['entered']:
                    value['expected'] += count
                else:
                    value['expected'] = max(value['expected'], count)
                value.update(entered=False, lower_bound=bool(value.get('lower_bound') or lower_bound))
                self._save(db, value)
            return value

    def entered(self, value):
        value['entered'] = True
        with self.store.connect() as db:
            self._save(db, value)

    def record_page(self, value, items, page):
        refs = set(value['refs'])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in items:
                self.store.ingest(self.session, self.account, item, db=db)
                refs.add(item['source_ref'])
            value.update(refs=sorted(refs), pages=max(value['pages'], page))
            self._save(db, value)

    def finish(self):
        with self.store.connect() as db:
            db.execute('DELETE FROM inbox_windows WHERE session=? AND account=?',
                       (self.session, self.account['account_id']))


async def collect_on_visit(session, visit, scan=None):
    """One scan after a real foreground read with a visible badge, never a timer."""
    if not session or not visit.get('notice'):
        return ''
    if visit.get('checked'):
        return visit.get('notice', '')
    visit['checked'] = True
    if scan is None:
        from .native import available
        from .service import request
        if not available():
            return visit['notice']
        scan = request
    try:
        observed = await scan(session_id=session, action='scan')
    except Exception:
        # A secondary inbox visit cannot invalidate the already acquired post.
        return '看到了小红书消息红点，未核对完消息分类。'
    pending = len(observed.get('pending_items', ()))
    dms = observed.get('visible_dm_unread', 0)
    parts = []
    if pending:
        parts.append(f'有 {pending} 条待处理的评论或@')
    if dms:
        parts.append(f'有 {dms} 个个人会话可查看')
    if observed.get('coverage') in {'unread_gap', 'badge_unavailable'}:
        parts.append('评论和@还有未核对部分')
    # A follow/like-only badge carries no social task for AI.
    visit['notice'] = ('小红书：' + '，'.join(parts) + '。') if parts else ''
    return visit['notice']
