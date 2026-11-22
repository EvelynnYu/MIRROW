"""Source-aware shelf selection and frozen reading receipts, not a second archive."""
import asyncio
import sqlite3
import json


async def catalog(handler, *, source='', offset=0, query=''):
    from .essays import read
    try:
        data = await asyncio.to_thread(read, limit=100, offset=offset, query=query)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {'essays': {'status': 'unavailable', 'error_type': type(exc).__name__}}, []
    entries = [dict(item, source='essays') for item in data['items']]
    return {'essays': {'status': 'available' if entries else 'empty',
                       'candidate_count': len(entries), 'offset': offset, 'query': query,
                       'scope': '随笔目录每页最多100篇，可按题目或关键词继续查找'}}, entries


async def material(handler, choices, entries, bookmarks, excluded):
    """Validate the complete selection before image/model work; retain partial failures."""
    from .bookmark_selection import outcome
    from .essays import read
    if not isinstance(choices, list) or not choices:
        return outcome('failed', 'selection_invalid', mode='bookshelf')
    selected, seen = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or choice.get('depth') not in ('skim', 'deep'):
            return outcome('failed', 'selection_invalid', mode='bookshelf')
        source, identity = choice.get('source'), choice.get('id')
        if source == 'bookmarks':
            target = next((row for row in bookmarks if row['message_id'] == identity
                           and row['collected_by'] == choice.get('collected_by')), None)
        else:
            target = next((row for row in entries if row['source'] == source and row['id'] == identity), None)
        if target is None:
            return outcome('failed', 'selection_target_invalid', mode='bookshelf')
        key = (source, identity, choice.get('collected_by'))
        if key not in seen:
            seen.add(key)
            selected.append((choice, target))
    items, snapshot, candidates, failures = [], [], [], []
    for choice, target in selected:
        source, depth = choice['source'], choice['depth']
        item = {'source': source, 'id': choice['id'], 'depth': depth,
                'title': target.get('title') or '收藏原话',
                'created_at': target.get('created_at') or target.get('message_time', '')}
        try:
            if source == 'bookmarks':
                found = handler._existing_material(excluded, dict(target, depth=depth))
                if not found:
                    raise LookupError('bookmark_unavailable')
                item.update(collected_by=target['collected_by'], messages=found['snapshot'])
                snapshot.extend(found['snapshot'])
                candidates.extend(found['candidates'])
            elif source == 'essays':
                work = await asyncio.to_thread(read, event_id=choice['id'])
                if not work:
                    raise LookupError('essay_unavailable')
                item.update(title=work['title'], keyword=work['keyword'], created_at=work['created_at'],
                            content=work['content'] if depth == 'deep' else work['content'][:300],
                            coverage='全文' if depth == 'deep' or len(work['content']) <= 300 else '开头 300 字',
                            kind='AI 的创作作品，故事情节不等于现实经历')
                picture = work.get('illustration') or {}
                if picture.get('status') == 'ready':
                    item['配图创作时选的画面'] = picture['scene']
            if source == 'essays':
                from .reading_illustrations import for_work
                drawings = await asyncio.to_thread(for_work, source, item['id'])
                visible = [p for p in drawings if p['end'] <= len(item['content'])]
                if visible:
                    item['reading_pictures'] = [{'quote': p['quote'], 'scene': p['scene']} for p in visible]
            items.append(item)
        except Exception as exc:
            failures.append({'source': source, 'id': choice['id'], 'error_type': type(exc).__name__})
    if not items:
        return outcome('failed', 'material_read_failed', mode='bookshelf', selected={'items': choices})
    result = outcome('ready', '', mode='bookshelf', selected={'items': choices})
    result.material = {'source_mode': 'bookshelf', 'browse_mode': 'selected', 'owner': '书柜',
                       'snapshot': snapshot, 'candidates': candidates,
                       'bookshelf_items': items, 'unread_items': failures}
    return result


def prompt_material(material):
    """Only this frozen input is later labelled as read; no rereading mutable sources."""
    items = material['bookshelf_items']
    candidates = {(row['message_id'], row.get('collected_by')): i
                  for i, row in enumerate(material['candidates'], 1)}
    lines = ['[本次书柜材料；旧原话与创作作品各有来源]']
    for item in items:
        header = {key: value for key, value in item.items() if key not in ('messages', 'content', 'observation', 'reading_pictures')}
        lines.append(json.dumps(header, ensure_ascii=False))
        for row in item.get('messages', []):
            label = {'user': '人类伙伴原话', 'assistant': 'AI原话'}.get(row.get('role'), '历史记录')
            index = candidates.get((row.get('message_id'), item.get('collected_by')))
            lines.append(f'[{label}｜{row.get("timestamp") or row.get("date", "")}｜候选{index or "无"}] {row.get("content", "")}')
        lines.append(item.get('content') or '')
        for picture in item.get('reading_pictures', []):
            lines.append(f'夹在原句「{picture["quote"]}」旁的读后插画，当时的构思是「{picture["scene"]}」。')
    if material.get('unread_items'):
        lines.append('未读到的条目：' + json.dumps(material['unread_items'], ensure_ascii=False))
    return '\n'.join(lines)


def receipt(material):
    received = bool(material.get('_reading_received'))
    return {'reading_received': received,
            **({'reading_picture': material['reading_picture']} if received and material.get('reading_picture') else {}),
            'bookshelf_snapshot': {'version': 1, 'items': material['bookshelf_items'],
                                    'unread_items': material.get('unread_items', [])} if received else None}


from .bookshelf_actions import brief
