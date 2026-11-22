"""One source-aware bookmark decision. Reading availability is not an experience."""
from dataclasses import dataclass, field
import json

from .flash_structured import parse_json_object


@dataclass
class Selection:
    material: dict | None = None
    receipt: dict = field(default_factory=dict)


def outcome(status, reason, *, mode='', availability=None, selected=None, error_type=''):
    return Selection(receipt={
        'status': status, 'reason': reason, 'source_mode': mode,
        'availability': availability or {}, 'selected': selected or {},
        **({'error_type': error_type} if error_type else {}),
    })


def describe(reason):
    return {
        'chosen_stop': 'AI 选择结束这次翻阅，尚未阅读新材料。',
        'no_readable_material': '本次读取范围内没有找到可回看的材料。',
        'selected_material_empty': '所选来源本次未取得可读材料，尚未阅读。',
        'source_unavailable': '材料来源暂时不可用，尚未阅读。',
        'selection_call_failed': '选材请求失败，尚未阅读。',
        'selection_invalid': '选材回复未给出有效选择，尚未阅读。',
        'selection_target_invalid': '选材回复指向本次索引之外的收藏，尚未阅读。',
        'material_read_failed': '读取所选材料时出错，尚未完成阅读。',
        'reading_call_failed': '材料已取到，但阅读请求未成功返回。',
        'material_evidence_missing': '阅读回执缺少有效的原文来源。',
    }.get(reason, '本次翻阅未完成。')


async def select(handler, exclude_ids=(), source_mode=None):
    """Prepare optional historical excerpts once, then let AI choose or stop.

    Unselected excerpts remain local and are never marked read. Empty or
    failed acquisition returns to the existing settlement/planning chain;
    this function neither retries the model nor substitutes another source.
    """
    from .historical_reader import load_iceberg_memory_buckets
    excluded = {str(value) for value in (exclude_ids or ())}
    reader = handler._reader()
    requested = source_mode or handler._source_mode_override
    availability, prepared, candidates = {}, {}, []
    pools = {}
    for owner in ('k', 'user'):
        try:
            rows, total = handler._load_pool(owner)
            pools[owner] = [row for row in rows if row.get('original_msg_id')
                            and str(row['original_msg_id']) not in excluded]
            availability[owner] = {'status': 'available' if pools[owner] else 'empty',
                                   'candidate_count': len(pools[owner]), 'total': total}
            candidates.extend({
                'message_id': str(row['original_msg_id']), 'collected_by': owner,
                'role': row.get('role') or '', 'message_time': row.get('original_timestamp') or '',
                'collected_at': row.get('created') or row.get('created_at') or '',
                'preview': row.get('content') or '',
            } for row in pools[owner])
        except Exception as exc:
            availability[owner] = {'status': 'unavailable', 'error_type': type(exc).__name__}
    bookmarked = None
    try:
        bookmarked = handler._all_bookmarked_ids() | excluded
    except Exception as exc:
        bookmark_error = type(exc).__name__
    for mode in ('unbookmarked_anchor', 'random_day'):
        try:
            if bookmarked is None:
                availability[mode] = {'status': 'unavailable', 'error_type': bookmark_error}
                continue
            if mode == 'unbookmarked_anchor':
                if reader.memory_buckets is None and reader.scene_picker is None:
                    reader.memory_buckets = await load_iceberg_memory_buckets()
                    reader.memory_buckets_unavailable = reader.memory_buckets is None
                if reader.memory_buckets_unavailable:
                    availability[mode] = {'status': 'unavailable', 'reason': 'anchor_source_unavailable'}
                    continue
                material = reader.read_unbookmarked_anchor(bookmarked)
            else:
                material = reader.read_random_day(bookmarked)
            if material:
                prepared[mode] = material
            availability[mode] = {'status': 'available' if material else 'empty',
                                  'scope': '本次读取范围',
                                  **({'active_date': material.get('active_date', '')} if material else {})}
        except Exception as exc:
            availability[mode] = {'status': 'unavailable', 'error_type': type(exc).__name__}

    from .bookshelf_roam import catalog, material as shelf_material
    shelf_sources, shelf_entries = await catalog(handler) if not requested else ({}, [])
    shelf_entries = [row for row in shelf_entries if row['id'] not in excluded]
    availability.update(shelf_sources)
    selected = None
    if requested:
        mode = requested
        if mode == 'existing_bookmark':
            selected = candidates[0] if candidates else None
    else:
        if not candidates and not prepared and not shelf_entries:
            failed = any(item['status'] == 'unavailable' for item in availability.values())
            return outcome('failed' if failed else 'empty',
                           'source_unavailable' if failed else 'no_readable_material', availability=availability)
        while True:
            prompt = ('[MIRROW 本次书柜目录]\n'
                      'k、user 分别是 AI 与人类伙伴的收藏索引；role 是原发言人，collected_by 是收藏者。'
                      '历史片段是本次找到的可读材料，尚未阅读。\n'
                      '随笔目录只有索引。目录可翻页查找。\n'
                      + json.dumps({'sources': availability, 'bookmarks': candidates, 'shelf': shelf_entries}, ensure_ascii=False)
                      + '\n[本轮选择]\n想翻哪些收藏或随笔？数量和精细程度由你选，也可以回看历史片段或停下。\n'
                        '选书柜条目输出 JSON {"source_mode":"bookshelf","items":[{"source":"bookmarks|essays",'
                        '"id":"目录原ID","collected_by":"收藏所属k或user，其余留空","depth":"skim|deep"}]}。'
                        'skim 看收藏原句或随笔开头，deep 看收藏周围对话或随笔全文。\n'
                        '继续找目录可输出 {"source_mode":"catalog","source":"essays|k|user","offset":0,"query":"搜索词，可空"}；随笔每页100项，收藏每页200项，offset 从0累加。\n'
                        '回看历史、单条收藏或停下也可以输出 '
                        'JSON {"source_mode":"existing_bookmark|unbookmarked_anchor|random_day|stop",'
                        '"message_id":"选收藏时的原ID","collected_by":"选收藏时的k或user"}。')
            try:
                choice = parse_json_object(await handler._call_llm(prompt))
            except Exception as exc:
                return outcome('failed', 'selection_call_failed', availability=availability,
                               error_type=type(exc).__name__)
            if not isinstance(choice, dict):
                return outcome('failed', 'selection_invalid', availability=availability)
            mode = choice.get('source_mode')
            if mode != 'catalog':
                break
            source = choice.get('source')
            offset = choice.get('offset', 0)
            if source not in ('essays', 'k', 'user') or type(offset) is not int or offset < 0:
                return outcome('failed', 'selection_invalid', availability=availability)
            try:
                if source in ('k', 'user'):
                    rows, total = handler._chronicle().list_bookmarks(collected_by=source, offset=offset, limit=200)
                    page = [{'message_id': str(row['original_msg_id']), 'collected_by': source,
                             'role': row.get('role', ''), 'message_time': row.get('original_timestamp', ''),
                             'collected_at': row.get('created', ''), 'preview': row.get('content', '')}
                            for row in rows if str(row.get('original_msg_id') or '') not in excluded]
                    candidates = [row for row in candidates if row['collected_by'] != source] + page
                    availability[source] = {'status': 'available' if page else 'empty',
                                            'candidate_count': len(page), 'total': total, 'offset': offset}
                else:
                    states, page = await catalog(handler, source=source, offset=offset,
                                                 query=str(choice.get('query') or ''))
                    shelf_entries = [row for row in shelf_entries if row['source'] != source] + [
                        row for row in page if row['id'] not in excluded]
                    availability.update(states)
            except Exception as exc:
                availability[source] = {'status': 'unavailable', 'error_type': type(exc).__name__}
        if mode == 'bookshelf':
            result = await shelf_material(handler, choice.get('items'), shelf_entries, candidates, excluded)
            result.receipt['availability'] = availability
            return result
        if mode == 'stop':
            return outcome('stopped', 'chosen_stop', mode='stop', availability=availability)
        if mode == 'existing_bookmark':
            selected = next((row for row in candidates if row['message_id'] == choice.get('message_id')
                             and row['collected_by'] == choice.get('collected_by')), None)
            if selected is None:
                return outcome('failed', 'selection_target_invalid', mode=mode, availability=availability)
    if mode not in ('existing_bookmark', 'unbookmarked_anchor', 'random_day'):
        return outcome('failed', 'selection_invalid', availability=availability)
    selected_ref = {key: selected[key] for key in ('message_id', 'collected_by')} if selected else {}
    try:
        material = handler._existing_material(excluded, selected) if mode == 'existing_bookmark' else prepared.get(mode)
    except Exception as exc:
        return outcome('failed', 'material_read_failed', mode=mode, availability=availability,
                       selected=selected_ref, error_type=type(exc).__name__)
    if not material:
        state = availability.get(mode, {})
        failed = state.get('status') == 'unavailable'
        return outcome('failed' if failed else 'empty',
                       'source_unavailable' if failed else 'selected_material_empty',
                       mode=mode, availability=availability, selected=selected_ref)
    result = outcome('ready', '', mode=mode, availability=availability, selected=selected_ref)
    result.material = material
    return result
