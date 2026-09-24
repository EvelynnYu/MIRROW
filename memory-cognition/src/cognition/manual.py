"""Manual edits and recoverable deletion share the cognition history ledger."""
from datetime import datetime
import frontmatter
from . import books, maintenance as ledger


def record(domain, name, data, before, action):
    rid = ledger.digest({'manual': action, 'domain': domain, 'name': name, 'data': data})
    stamp = datetime.now().astimezone().isoformat()
    return {'id': rid, 'status': 'manual_applying', 'created_at': stamp, 'source_date': stamp[:10],
            'source_kind': 'manual', 'manual_action': action, 'automatic': False,
            'proposal': {'domain': domain, 'name': data.get('name', name), 'target_entry': data.get('name', name),
                         'body': data.get('body', ''), 'evidence': []},
            'plan': {**data, 'domain': domain, 'name': data.get('name', name), 'before': before,
                     'after': data.get('body', ''), 'operation': action}, 'review': {}, 'reason': '人类伙伴手动修改'}


def save(domain, name, data):
    with books.LOCK:
        old = next((e for e in books.catalog(domain) if e['name'] == name), None)
        item = record(domain, name, data, old['body'] if old else '', 'edit')
        rid = item['id']
        path = ledger.record_path(rid)
        if path.exists():
            item = ledger.get(rid)
            if item['status'] == 'applied':
                # A repeated request cannot rewind later changes.
                return books._read(books.locate(domain, data.get('name', name)), domain)
        else:
            ledger.write(item)
        try:
            if books.receipt_present(domain, data.get('name', name), 'manual:' + rid):
                entry = books._read(books.locate(domain, data.get('name', name)), domain)
            else:
                entry = books.save(domain, name, data, receipt='manual:' + rid, editor='人类伙伴手动修改',
                                   provenance={'maintenance_item_id': rid, 'source_date': item['source_date'], 'automatic': False})
        except books.BookError:
            item['status'] = 'manual_error'; ledger.write(item)
            raise
        item.update(status='applied', result_revision=entry['revision'], completed_at=datetime.now().astimezone().isoformat())
        ledger.write(item)
        return entry


def delete(domain, name, revision):
    if domain not in {'self', 'world'}:
        raise books.BookError('该书不开放删除')
    with books.LOCK:
        marker = {'revision': revision, 'name': name}
        rid = record(domain, name, marker, '', 'delete')['id']
        old_record = ledger.get(rid) if ledger.record_path(rid).exists() else None
        backup = books.ROOT / 'data/cognition_deleted' / (rid + '.md')
        if old_record and old_record['status'] == 'applied':
            return {'status': 'deleted', 'id': rid}
        if not old_record:
            path = books.locate(domain, name)
            raw = path.read_text('utf-8')
            if not revision or books.revision(raw) != revision:
                raise books.RevisionConflict('词条已变化，请刷新后再删除')
            entry = books._read(path, domain)
            old_record = record(domain, name, marker, entry['body'], 'delete')
            old_record['original_path'] = path.relative_to(books.folder(domain)).as_posix()
            books.atomic_text(backup, raw)
            ledger.write(old_record)
        path = books.folder(domain) / old_record['original_path']
        if not path.resolve().is_relative_to(books.folder(domain).resolve()):
            raise books.BookError('删除路径无效')
        if not backup.exists() or books.revision(backup.read_text('utf-8')) != revision:
            raise books.BookError('删除备份未核验，未移除词条')
        if path.exists():
            if books.revision(path.read_text('utf-8')) != revision:
                raise books.RevisionConflict('词条已变化，未删除')
            path.unlink()  # Exact, CAS-checked file; verified recoverable copy above.
        old_record.update(status='applied', completed_at=datetime.now().astimezone().isoformat())
        ledger.write(old_record)
        books.reload_domain(domain)
        return {'status': 'deleted', 'id': rid}


def restore(rid):
    with books.LOCK:
        item = ledger.get(rid)
        if item.get('manual_action') != 'delete' or item['status'] != 'applied':
            raise books.BookError('不是可恢复的删除记录')
        domain, name = item['plan']['domain'], item['plan']['name']
        path = books.folder(domain) / item['original_path']
        if not path.resolve().is_relative_to(books.folder(domain).resolve()):
            raise books.BookError('恢复路径无效')
        backup = books.ROOT / 'data/cognition_deleted' / (rid + '.md')
        raw = backup.read_text('utf-8')
        if books.revision(raw) != item['plan']['revision']:
            raise books.BookError('恢复备份校验失败')
        post = frontmatter.loads(raw)
        post['cognition_receipts'] = list(dict.fromkeys([*post.get('cognition_receipts', []), 'restore:' + rid]))
        restored_raw = frontmatter.dumps(post) + '\n'
        if path.exists() or any(e['name'].casefold() == name.casefold() for e in books.catalog(domain)):
            if item.get('restore_pending') and path.exists() and path.read_text('utf-8') == restored_raw:
                item['restored_at'] = datetime.now().astimezone().isoformat()
                item.pop('restore_pending', None)
                ledger.write(item)
                books.reload_domain(domain)
                return books._read(path, domain)
            if item.get('restored_at') and path.exists():
                return books._read(path, domain)
            raise books.RevisionConflict('已有同名或同路径条目，未覆盖')
        item['restore_pending'] = True
        ledger.write(item)
        books.atomic_text(path, restored_raw)
        item['restored_at'] = datetime.now().astimezone().isoformat()
        item.pop('restore_pending', None)
        ledger.write(item)
        books.reload_domain(domain)
        return books._read(path, domain)
