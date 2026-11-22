"""Lossless human-facing views of experience; no new authority or inference.

Opaque runtime identities remain in the source envelope. Observations, their
times, failures and original personal accounts are retained in the text.
Memory's settled source projection keeps its existing versioned bytes.
"""
import json
from collections.abc import Mapping

from context_builder.text_utils import _redact_tool_fact


def _text(value):
    return _redact_tool_fact(str(value or ''))


def state_text(value):
    return {'success': '成功', 'succeeded': '成功', 'failure': '失败', 'failed': '失败',
            'partial': '部分完成', 'completed': '完成', 'settled': '本次已结束',
            'running': '进行中', 'stopped': '已停止', 'interrupted': '已中断',
            'aborted': '已中止', 'skipped': '已跳过', 'unknown': '未知',
            'uncertain': '尚不确定', 'confirmed': '已取得来源记录',
            'attempted': '已尝试', 'not_attempted': '尚未尝试',
            'not_assessed': '尚未评估'}.get(str(value), _text(value) or '未知')


def compact_checkup(value, *, retained_impression=''):
    stamps = []
    for key, label in (('started_at', '开始'), ('observed_at', '观察'), ('completed_at', '结束')):
        if value.get(key):
            stamps.append(label + '于 ' + _text(value[key]))
    parts = [f"第 {value.get('round_index') or '?'} 次查岗。" + '；'.join(stamps)
             + f"；执行情况：{state_text(value.get('execution_status'))}"
             + f"；来源取得情况：{state_text(value.get('source_status') or value.get('conclusion'))}。"]
    if value.get('intent'):
        parts.append('当时想了解：' + _text(value['intent']))
    from checkup_assessment import goal_assessment_text
    assessment = goal_assessment_text(value.get('goal_assessment'))
    if assessment:
        parts.append(assessment)
    items = value.get('items') or []
    if not items:
        parts.append('这次没有取得感知结果，人类伙伴当时在做什么尚不清楚。')
    for item in items:
        source, tool = _text(item.get('description') or item.get('tool') or '感知来源'), _text(item.get('tool'))
        line = source + (f'（{tool}）' if tool and tool != source else '')
        line += '：' + state_text(item.get('status'))
        for key, label in (('attempted', '尝试'), ('captured', '采集'), ('analyzed', '分析')):
            if key in item:
                line += '；' + ({True: '已' + label, False: '未' + label}.get(item[key], label + '情况未知'))
        parts.append(line)
        if item.get('result'):
            parts.append(_text(item['result']))
        if item.get('error'):
            parts.append('失败／不确定：' + _text(item['error']))
        extra = {k:v for k,v in item.items() if k not in {
            'tool','description','status','attempted','captured','analyzed','result','error'}}
        if extra:
            parts.append('来源补充：' + _text(json.dumps(extra, ensure_ascii=False, separators=(',', ':'))))
    if value.get('selection_errors'):
        parts.append('选择未执行：' + _text(json.dumps(value['selection_errors'], ensure_ascii=False)))
    reflection = value.get('recorded_reflection') or ''
    if reflection and not value.get('reflection_is_fact_projection') and not value.get('reflection_is_retained') and reflection != retained_impression:
        parts.append('AI 在这一步留下的感想（原话）：' + _text(reflection))
    return '\n'.join(parts)


def frozen_text(value):
    """AI-facing transport only. The frozen receipt and day-end stay unchanged."""
    from memory_v2.experience_contract import project, impression_text
    shell = dict(value, materials=[], retained_impression='', settlement_reflection='')
    shell.pop('purpose_outcome', None)
    parts = [project(shell) or '']
    shell.pop('stop_reason', None)
    shell.pop('memory_lookup_attempts', None)
    retained = impression_text(value.get('retained_impression'), node_ids=value.get('retained_node_ids') or [])
    for item in value.get('materials') or []:
        if item.get('kind') == 'checkup' and item.get('version') == 1:
            parts.append(compact_checkup(item, retained_impression=retained))
        else:
            parts.append(project(dict(shell, materials=[item], phone_task=None)) or '')
    if retained:
        parts.append('AI 当时留下的感受（原话）：' + retained)
    settlement = value.get('settlement_reflection')
    if settlement and settlement != retained:
        parts.append('AI 当时的回顾（原话）：' + _text(settlement))
    from .activity_purpose import outcome_text
    parts.append(outcome_text(value.get('purpose_outcome')))
    return '\n'.join(part for part in parts if part)


def runtime_text(value):
    """Natural continuity view, distinct from current execution evidence."""
    from memory_v2.experience_contract import impression_text
    overview = value.get('execution_overview') or {}
    activity = value.get('activity') or {}
    parts = ['当时活动状态：' + state_text(activity.get('state') or overview.get('state')) + '。']
    if overview.get('stop_reason'):
        parts.append(_text(overview['stop_reason']))
    parts.extend(_text(item) for item in overview.get('memory_lookup_attempts') or [])
    status = overview.get('execution_status_counts') or {}
    if status:
        parts.append('当时执行：' + '、'.join(f'{state_text(k)} {v} 次' for k,v in status.items()) + '。')
    if overview.get('completion_signals'):
        parts.append('完成回执：' + '、'.join(f'{k} {v}次' for k,v in overview['completion_signals'].items()) + '。')
    for row in overview.get('music_materials') or []:
        receipt = row.get('music_execution') or {}
        parts.append('《' + _text(row.get('title') or '曲名未记录') + '》'
                     + ('（' + _text(row['artist']) + '）' if row.get('artist') else '')
                     + '；' + _text(receipt.get('fact') or row.get('completion_signal') or '完成状态未知'))
        if receipt.get('execution_status'):
            parts.append('该曲执行=' + _text(receipt['execution_status'])
                         + '；完成信号=' + _text(receipt.get('completion_signal') or row.get('completion_signal') or '无'))
        timing = [label + '=' + _text(receipt[key]) for key,label in (
            ('duration_sec','曲长秒'),('started_at','开始'),('completes_at','听完到点')) if receipt.get(key) is not None]
        if row.get('completed_at'):
            timing.append('节点结束=' + _text(row['completed_at']))
        # Device execution facts remain distinct, including legacy unplayed
        # analysis. Identity hashes are not perceptual facts.
        for key in ('played','playback_requested','playback_target','analysis_source'):
            if key in receipt:
                timing.append(key + '=' + json.dumps(receipt[key], ensure_ascii=False))
        if timing:
            parts.append('；'.join(timing))
    for row in overview.get('checkup_observations') or []:
        parts.append(compact_checkup(dict(row, items=row.get('checkup_items') or [],
                                          recorded_reflection='')))
    for key, label in (('game_progress','游戏进度'),('phone_task','手机任务')):
        if overview.get(key):
            parts.append(label + '：' + json.dumps(overview[key], ensure_ascii=False, separators=(',', ':')))
    for row in value.get('source_material_descriptions') or []:
        if row.get('description'):
            parts.append('当时取得：' + _text(row['description']))
    subjective = value.get('subjective_experience') or {}
    from .activity_purpose import outcome_text
    purpose = outcome_text(subjective.get('purpose_outcome'))
    if purpose:
        parts.append(purpose)
    retained = impression_text(subjective.get('retained_impression'))
    source_ids = set(subjective.get('source_node_ids') or [])
    if retained:
        parts.append('你当时留下的感想（原话）：「' + retained + '」')
    for index, row in enumerate(subjective.get('node_reflections') or subjective.get('legacy_node_reflections') or [], 1):
        feeling = impression_text(row.get('reflection'), node_ids=[row.get('node_id')])
        if feeling and not (feeling == retained and row.get('node_id') in source_ids):
            parts.append(f'第 {index} 步留下的感想（原话）：「' + feeling + '」')
    settlement = impression_text(subjective.get('settlement_reflection'))
    if settlement:
        parts.append('你当时的回顾（原话）：「' + settlement + '」')
    thought = subjective.get('continuation_thought_at_settlement')
    if thought:
        parts.append('当时续行想法：' + _text(thought.get('note')))
        if thought.get('disposition'):
            parts.append('该想法的后续记录：' + json.dumps(thought['disposition'], ensure_ascii=False, separators=(',', ':')))
    # Preserve future/unknown objective fields rather than silently losing
    # new domain facts when a producer adds a field to the overview.
    extra = {k:v for k,v in overview.items() if k not in {
        'activity_id','run_id','state','execution_status_counts','completion_signals','stop_reason','memory_lookup_attempts',
        'music_materials','checkup_observations','game_progress','phone_task'}}
    if extra:
        parts.append('其他执行事实：' + json.dumps(extra, ensure_ascii=False, separators=(',', ':')))
    return '\n'.join(parts) or ('当时活动状态：' + _text(activity.get('state') or overview.get('state') or '未知'))
