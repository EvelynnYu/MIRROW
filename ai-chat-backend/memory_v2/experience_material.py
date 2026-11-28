"""Pure, bounded material capture. Never copies audits or decision reasoning."""
from collections.abc import Mapping

from context_builder.text_utils import _redact_tool_fact


def text(value, limit=4000):
    return _redact_tool_fact(value)[:limit] if isinstance(value, str) else ''


def capture_reading(material):
    """Freeze excerpts of the exact historical messages supplied to AI."""
    rows = material.get('snapshot') or []
    captured, remaining, shortened = [], 16000, 0
    for row in rows[:64]:
        content = text(row.get('content'), 16000)
        if not content or not remaining:
            continue
        excerpt = content[:remaining]
        shortened += len(excerpt) < len(content) or len(str(row.get('content') or '')) > 16000
        captured.append({'message_id': str(row.get('message_id') or ''),
                         'role': str(row.get('role') or ''),
                         'timestamp': text(row.get('timestamp') or row.get('date'), 80),
                         'text': excerpt})
        remaining -= len(excerpt)
    return {'version': 1, 'source_mode': text(material.get('source_mode'), 80),
            'scene_id': str(material.get('scene_id') or ''),
            'active_date': text(material.get('active_date'), 10), 'messages': captured,
            'omitted_message_count': len(rows) - len(captured),
            'truncated_message_count': shortened}


def prepare_reading(material, role_label):
    """Build the legacy 300/3000-character input and its exact source view.

    The source snapshot cannot contain a tail that AI never received. Keep
    the provider-facing prompt bytes unchanged, including its row labels.
    """
    candidates = {str(row.get('message_id') or ''): index
                  for index, row in enumerate(material.get('candidates') or [], 1)
                  if row.get('message_id')}
    lines, visible, remaining, shortened = [], [], 3000, 0
    rows = material.get('snapshot') or []
    for row in rows:
        message_id = str(row.get('message_id') or '')
        marker = f' 候选{candidates[message_id]}' if message_id in candidates else ''
        date = str(row.get('date') or row.get('timestamp') or '')[:10] or '未知日期'
        prefix = f"[{role_label(row.get('role'))} · {date}{marker}] "
        raw = str(row.get('content') or '')
        body = raw[:300]
        if lines:
            remaining -= 1
        excerpt = body[:max(0, remaining - len(prefix))]
        if excerpt:
            visible.append({**row, 'content': excerpt})
            shortened += len(excerpt) < len(raw)
        lines.append(prefix + body)
        remaining -= len(prefix) + len(body)
    frozen = capture_reading({**material, 'snapshot': visible})
    frozen['omitted_message_count'] = len(rows) - len(visible)
    frozen['truncated_message_count'] = shortened
    return '\n'.join(lines)[:3000], frozen


def reading_receipt(material):
    frozen = material.get('_reading_snapshot') if material.get('_reading_received') else None
    rows = (frozen.get('messages') or []) if isinstance(frozen, dict) else []
    visible_ids = {row.get('message_id') for row in rows}
    return {'reading_received': bool(material.get('_reading_received')), 'reading_snapshot': frozen,
            'displayed_message_count': len(rows),
            'displayed_char_count': sum(len(row['text']) for row in rows),
            'candidate_message_ids': [str(row['message_id']) for row in material.get('candidates') or []
                                      if row.get('message_id') in visible_ids]}


def node_material(payload, *, execution_status='succeeded', k_reflection='', k_received=False):
    """Only domain-produced observed inputs qualify as rich experience."""
    shelf = payload.get('bookshelf_snapshot')
    if isinstance(shelf, Mapping) and shelf.get('version') == 1 and shelf.get('items'):
        from wander_manager.bookshelf_actions import brief
        return {'kind': 'bookshelf', 'description': brief(shelf), 'reading': shelf,
                'reflection': text(payload.get('reflection'))}
    if payload.get('interaction_version') == 1 and (payload.get('materials') or payload.get('xhs_interactions')):
        from xhs_interactions.context import material
        return material(payload)
    if payload.get('xhs_publication'):
        from xhs_publish.service import experience
        return {'kind':'xhs_publication','description':experience(payload['xhs_publication']),
                'publication':dict(payload['xhs_publication'])}
    reading = payload.get('reading_snapshot')
    if isinstance(reading, Mapping) and reading.get('version') == 1 and reading.get('messages'):
        return {'kind': 'reading', 'reading': reading,
                'reflection': text(payload.get('reflection'))}
    workshop = payload.get('experience')
    if isinstance(workshop, Mapping) and workshop.get('version') in (1, 2):
        # Memory/day-end keeps a bounded account plus stable references.  The
        # full plan, room transcript and artifact body stay in the workshop
        # authority and are read on demand, never copied into every event card.
        from workshop.experience import summarize
        fact, refs = summarize(workshop, include_identifiers=False)
        if fact:
            material = {'kind': 'workshop', 'description': fact, 'summary_version': 2}
            if refs:
                material['refs'] = refs
            return material
    if execution_status == 'succeeded' and payload.get('source_id') and payload.get('content_summary'):
        from wander_manager.xhs_request import material_for_ai
        return {'kind': 'xhs', 'title': text(payload.get('title'), 300),
                'content_summary': text(payload.get('content_summary')),
                'evidence_excerpt': text(payload.get('evidence_excerpt') or payload.get('source_ui_text'), 6000),
                'media_observation': text(payload.get('media_observation')),
                'coverage': dict(payload.get('coverage') or {}),
                **({'received_material': material_for_ai(payload)}
                   if payload.get('material_version') == 1 else {}),
                **({'material_delivery': 'K_review' if k_received else 'executor_only'} if payload.get('material_version') == 1 else {}),
                'reflection': (text(k_reflection) if k_received else '') if payload.get('material_version') == 1 else text(payload.get('reflection'))}
    return None


def game_material(nodes):
    """Reuse confirmed chapter facts, excluding internal choice reasoning."""
    from wander_manager.game_experience import chapter_facts
    chapters = []
    for chapter in chapter_facts(nodes):
        chapters.append({key: chapter[key] for key in (
            'playthrough_key', 'number', 'title', 'protagonist', 'ending', 'reflection', 'source_node_ids')})
        chapters[-1]['choices'] = [{key: choice[key] for key in ('node_id', 'label', 'text')}
                                  for choice in chapter['choices']]
    from wander_manager.game_results import result_facts
    results = result_facts(nodes)
    return {'kind': 'game', 'chapters': chapters, **({'results': results} if results else {})} if chapters or results else None


def project_material(item):
    kind = item.get('kind')
    if kind == 'xhs_interactions' and item.get('context_version') == 2:
        from xhs_interactions.context import brief
        return brief(item)
    if kind == 'reading':
        reading = item['reading']
        lines = ['AI当时回看的历史对话摘录' + (f"（{reading['active_date']}）" if reading.get('active_date') else '') + '：']
        for row in reading.get('messages') or []:
            speaker = {'user': '人类伙伴原话', 'assistant': 'AI原话'}.get(row.get('role'), '历史材料')
            lines.append(f"{speaker}：{row['text']}")
        if reading.get('omitted_message_count') or reading.get('truncated_message_count'):
            lines.append('这里只保留了部分当时收到的原文；完整来源仍在历史对话。')
    elif kind == 'game':
        lines = ['AI在虚构游戏中实际推进的章节：']
        if item.get('results'):
            from wander_manager.game_results import result_text
            lines = [result_text(fact) for fact in item['results']]
        for chapter in item.get('chapters') or []:
            lines.append(f"第{chapter.get('number')}章《{chapter.get('title') or '未命名'}》")
            choices = chapter.get('choices') or []
            if choices:
                lines.append('AI的已选路径：' + ' → '.join(
                    (choice.get('label') or '') + '：' + (choice.get('text') or '') for choice in choices))
            if chapter.get('ending'):
                lines.append('该章节记录的结局：' + chapter['ending'])
            if chapter.get('reflection'):
                lines.append('AI保存的章末感想（AI原述）：' + chapter['reflection'])
    elif kind == 'xhs':
        label = '专用手机取得了' if item.get('material_delivery') == 'executor_only' else 'AI当时读取了'
        lines = [f"{label}小红书帖子《{item.get('title') or '未标注标题'}》。",
                 '已读材料的摘要：' + item['content_summary']]
        if item.get('material_delivery') == 'executor_only':
            lines.append('该节点没有AI实际复核材料的成功回执。')
        if item.get('evidence_excerpt'):
            lines.append('帖子／页面原文摘录：' + item['evidence_excerpt'])
        if item.get('media_observation'):
            lines.append('当时取得的媒体观察：' + item['media_observation'])
    else:
        lines = [item.get('description') or '']
    if item.get('reflection'):
        lines.append('AI当时的感想（AI原述）：' + item['reflection'])
    return '\n'.join(line for line in lines if line)
