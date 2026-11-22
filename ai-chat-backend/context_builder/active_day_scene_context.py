"""Shared read-only, source-attributed active-day context for internal consumers."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from time_utils import parse_ts

async def read_active_day_scene_material(session_id: str):
    """Resolve the active day once; reading cannot close or digest its boxes."""
    if not session_id:
        return None
    from memory_v2 import scene_digest_runtime
    from conversation_manager_v2 import resolve_active_date
    active_date = await asyncio.to_thread(resolve_active_date, session_id)
    cutoff = await scene_digest_runtime.completed_before(active_date)

    def read():
        from event_chronicle import get_global_chronicle
        from memory_v2.conversation_source import ConversationSource
        from memory_v2.scene_material import read_scene_material
        source = ConversationSource(get_global_chronicle().db_path, canonical_sessions_only=True)
        for attempt in range(2):
            try:
                return read_scene_material(source, active_date, completed_before=cutoff)
            except ValueError as exc:
                if attempt or str(exc) != 'scene_material_source_changed':
                    raise
    return await asyncio.to_thread(read)

@dataclass(frozen=True)
class _Unit:
    timestamp: str
    ordinal: int
    kind: str
    text: str

def _raw_text(message, source_ref, *, speaker):
    role = message['role']
    kind = message.get('source_kind') or source_ref.get('source_kind', 'chat')
    event_type = source_ref.get('source_event_type', '')
    label = {'user': '人类伙伴', 'assistant': speaker, 'event': '事件记录', 'notification': '系统通知', 'system': '系统'}.get(role, role)
    if role == 'assistant' and kind in {'wander', 'sentinel', 'reminder'}:
        label += '·' + {'wander': '漫想', 'sentinel': '哨兵', 'reminder': '提醒'}[kind]
        provenance = '[历史通信原话：AI 当时发给人类伙伴的主动消息]'
    elif event_type == 'wander_activity':
        provenance = '[已发生的漫想经历：事件回执与选留感受]'
    else:
        provenance = ''
    clock = parse_ts(message['timestamp']).strftime('%Y-%m-%d %H:%M')
    return '\n'.join((part for part in (provenance, f"[{clock}｜{label}] {message['content']}") if part))

def render_active_day_scene_context(material, *, speaker='AI', exclude_message_ids=(), source_rows=None, source_db_path=None, wander_db_path=None, evidence_index=None):
    """Chronological summaries/raw, preserving AI's communication and uncovered acts.

    Summary source IDs describe actual impressions, not the box's full source
    partition. AI-only rows may be non-events in the mainline memory encoder;
    those receipts still belong to the short-term background of AI's life.
    """
    from memory_v2.scene_material import render_scene_material
    import sqlite3
    excluded = set(exclude_message_ids)
    units = []
    raw_units = {}
    for box in material.get('boxes', ()):
        for segment in box.get('segments', ()):
            refs = {ref['message_id']: ref for ref in segment.get('source_refs', ())}
            summary_ids = set(segment.get('summary_source_message_ids', refs))
            use_summary = box['state'] == 'closed' and segment['kind'] == 'summary' and (not excluded.intersection(summary_ids))
            if use_summary and segment.get('summary'):
                stage = '日结印象' if segment['stage'] == 'settled' else '初步印象'
                text = render_scene_material({'boxes': [{'segments': [segment]}]}, include_communications=False)
                units.append(_Unit(segment['start_ts'], len(units), 'summary', f'[已关闭盒·{stage}，派生摘要]\n{text}'))
            label = '开放盒原文' if box['state'] == 'open' else '已关闭盒保留来源' if use_summary else '已关闭盒原文兜底·摘要缺失、待消化、已失效或当前活动另行提供'
            communications = {item['message_id']: item['summary'] for item in segment.get('communication_summaries', ())}
            for message in segment.get('messages', ()):
                mid = message.get('message_id', '')
                if mid in excluded or not str(message.get('content') or '').strip():
                    continue
                ref = refs.get(mid, {})
                communication = message['role'] in {'user', 'assistant'} and (not ref.get('source_event_type') or (message.get('source_kind') or ref.get('source_kind')) in {'wander', 'sentinel', 'reminder'})
                if use_summary and communication and (mid in communications):
                    author = '人类伙伴' if message['role'] == 'user' else speaker
                    clock = parse_ts(message['timestamp']).strftime('%Y-%m-%d %H:%M')
                    kind = message.get('source_kind') or ref.get('source_kind')
                    if message['role'] == 'assistant' and kind in {'wander', 'sentinel', 'reminder'}:
                        author += '·' + {'wander': '漫想', 'sentinel': '哨兵', 'reminder': '提醒'}[kind]
                    units.append(_Unit(message['timestamp'], len(units), 'communication_summary', f'[已关闭盒·通信摘要｜{clock}｜{author}] {communications[mid]}'))
                    continue
                uncovered_activity = ref.get('source_event_type') == 'wander_activity' and mid not in summary_ids
                if use_summary and (not (communication or uncovered_activity)):
                    continue
                raw_label = label
                if evidence_index is not None and message['role'] in {'user', 'assistant'} and (not ref.get('source_event_type') or communication):
                    short_id = 'H' + str(len(evidence_index) + 1)
                    author = '人类伙伴' if message['role'] == 'user' else 'AI'
                    evidence_index[short_id] = {'message_id': mid, 'role': message['role'], 'speaker': author, 'timestamp': message['timestamp'], 'content': message['content'], 'content_digest': ref.get('content_digest', ''), 'source_kind': message.get('source_kind') or ref.get('source_kind', 'chat')}
                    raw_label += f'｜原话索引 {short_id}｜说话人 {author}'
                text = _raw_text(message, ref, speaker=speaker)
                raw_units[len(units)] = (message, ref, raw_label)
                units.append(_Unit(message['timestamp'], len(units), 'raw', f'[{raw_label}]\n{text}'))
    units.sort(key=lambda unit: (parse_ts(unit.timestamp), unit.ordinal))
    if any((ref.get('source_event_type') in {'wander_activity'} for _, ref, _ in raw_units.values())):
        from pathlib import Path
        ordered = [raw_units[unit.ordinal][0] for unit in units if unit.kind == 'raw']
        by_id = {}
        try:
            if source_rows is None:
                from contextlib import closing
                path = Path(source_db_path or Path(__file__).resolve().parents[1] / 'events' / 'event_chronicle.db')
                source_rows = []
                if path.is_file():
                    ids = [row['message_id'] for row in ordered]
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as conn:
                        conn.row_factory = sqlite3.Row
                        conn.execute('PRAGMA query_only=ON')
                        for start in range(0, len(ids), 400):
                            batch = ids[start:start+400]
                            source_rows.extend(dict(row) for row in conn.execute(
                                'SELECT message_id,session_id,role,event_type,timestamp,content,tool_calls FROM conversation_messages WHERE message_id IN ('
                                + ','.join('?' for _ in batch) + ')', batch))
            by_id = {row['message_id']: row for row in source_rows}
            from memory_v2.source_projection import memory_source_content
            selected = {message['message_id']: (message, ref) for message, ref, _ in raw_units.values()}
            by_id = {mid: row for mid, row in by_id.items() if mid in selected and (
                selected[mid][1].get('source_event_type') != 'wander_activity' or (
                    row.get('timestamp') == selected[mid][0].get('timestamp')
                    and row.get('role') == selected[mid][0].get('role')
                    and row.get('event_type') == selected[mid][1].get('source_event_type')
                    and (not selected[mid][1].get('session_id') or row.get('session_id') == selected[mid][1]['session_id'])
                    and memory_source_content(row) == selected[mid][0].get('content')))}
            raw = [by_id[row['message_id']] for row in ordered if row['message_id'] in by_id]
            from .wander_event_projection import inner_event_text
            overrides = {row['message_id']: inner_event_text(row, frozen_only=True)
                         for row in raw if row.get('event_type') == 'wander_activity'}
        except (OSError, ValueError, TypeError, sqlite3.Error):
            overrides = {}  # Original admitted material is the safe fallback.
        for index, unit in enumerate(units):
            if unit.kind != 'raw':
                continue
            message, ref, label = raw_units[unit.ordinal]
            replacement = overrides.get(message['message_id'])
            if replacement:
                if ref.get('source_event_type') == 'wander_activity':
                    label = label.replace('原文', '来源记录') + '｜漫想简记，详情可查'
                view = {**message, 'content': replacement}
                units[index] = _Unit(unit.timestamp, unit.ordinal, 'raw',
                                     f'[{label}]\n' + _raw_text(view, ref, speaker=speaker))
        from .wander_narration import identity, connection
        previous = None
        for index, unit in enumerate(units):
            current = (identity(by_id.get(raw_units[unit.ordinal][0]['message_id'], {}))
                       if unit.kind == 'raw' else None)
            prefix = connection(previous, current)
            if prefix:
                units[index] = _Unit(unit.timestamp, unit.ordinal, unit.kind, prefix + '\n' + unit.text)
            previous = current
    if not units:
        return ''
    raw_count = sum((unit.kind == 'raw' for unit in units))
    summary_count = sum((unit.kind in {'summary', 'communication_summary'} for unit in units))
    coverage = f'[材料覆盖：来源记录 {raw_count} 条；关闭盒摘要 {summary_count} 段。上述投影未按长度裁剪。]'
    return '\n\n'.join([f"当前活跃日 {material['active_date']} 的盒子材料（按来源时间排序）：", *(unit.text for unit in units), coverage])
