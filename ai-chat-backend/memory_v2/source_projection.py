"""Stable Memory V2 text derived from the one conversation-row authority.

The projection is deliberately limited to facts persisted *in that row*.
Side databases may enrich the live context, but their mutable contents cannot
silently change a settled batch's source digest or quotation spans.
"""
from __future__ import annotations
import json
from typing import Any, Mapping
from context_builder.notification_projection import build_notification_context_fact
from .tool_source_projection import project_tool_event, _redact_tool_fact, _tool_fact_status
SOURCE_PROJECTION_VERSION = 2
SOURCE_PROJECTION_START_ACTIVE_DATE = '2026-09-24'
RICH_DOMAIN_EVENT_START_ACTIVE_DATE = '2026-09-26'
SOURCE_SCOPE_START_ACTIVE_DATE = '2026-10-07'

def _calls(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

def _count(value: Any) -> int:
    try:
        return max(0, min(int(value), 10000))
    except (TypeError, ValueError, OverflowError):
        return 0

def long_text_card_fact(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ''
    if not isinstance(raw, Mapping):
        return ''
    title = _redact_tool_fact(raw.get('title') or '无标题')
    if raw.get('kind') == 'xhs':
        return f'[小红书链接卡片] 人类伙伴分享了一篇帖子《{title}》的链接；仅凭此卡不能证明 AI 已读帖子。链接保存在原消息附件中。'
    if not str(raw.get('body') or '').strip():
        return ''
    if raw.get('kind') == 'music':
        return f'[音乐卡片] 人类伙伴发送了一张音乐卡片《{title}》。卡片原文保存在原消息附件中。'
    return f'[长文卡片] 人类伙伴发送了一张长文卡片《{title}》。卡片全文保存在原消息附件中。'

def _card_fact(call: Mapping[str, Any], *, rich_domain_event: bool=False) -> str:
    extra = call.get('extra_data') or {}
    if isinstance(extra, dict) and extra.get('wander_projection_version') == 2:
        from wander_manager.essays import shared_work_fact
        shared = shared_work_fact(call)
        if shared:
            return shared
    extra = call.get('extra_data')
    if not isinstance(extra, Mapping):
        return ''
    status = _tool_fact_status(dict(call))
    if isinstance(extra.get('music_card'), Mapping):
        card = extra['music_card']
        title = _redact_tool_fact(card.get('name') or card.get('title') or '一首歌曲')
        artist = _redact_tool_fact(card.get('artist'))
        return f'[音乐事件] AI处理了《{title}》' + (f'（{artist}）' if artist else '') + f'。结果：{status}。'
    if isinstance(extra.get('xhs_post_card'), Mapping):
        card = extra['xhs_post_card']
        title = _redact_tool_fact(card.get('title') or '一篇帖子')
        verb = '邀请人类伙伴手动评论' if card.get('comment_draft') else '分享给人类伙伴'
        return f'[小红书分享卡片] AI将已读帖子《{title}》{verb}。结果：{status}。卡片不证明人类伙伴已打开或发表评论。'
    if extra.get('attachment_kind') == 'music':
        return f'[音乐事件] AI发起了一项音乐操作。结果：{status}。'
    if str(call.get('tool') or '') == 'send_voice':
        return f'[语音事件] AI发送语音。结果：{status}。'
    return ''

def memory_source_action_actors(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Read typed local action receipts without inferring an actor from prose."""
    if row.get('role') == 'event' and row.get('event_type') == 'workshop_event':
        from .cabinet_action_source import action_actors as cabinet_actors
        actors = cabinet_actors(row)
        if actors:
            return actors
        from workshop.context import action_actors
        return action_actors(row)
    return ()

def recorded_music_occurrences(row: Mapping[str, Any]) -> tuple[dict, ...]:
    """Typed counts from new immutable snapshots, never from generated prose."""
    if row.get('role') != 'event' or row.get('event_type') != 'wander_activity':
        return ()
    records = []
    for call in _calls(row.get('tool_calls')):
        extra = call.get('extra_data') or {}
        value = extra.get('memory_experience') if isinstance(extra, Mapping) else None
        if not isinstance(value, Mapping) or value.get('version') not in {1, 2}:
            continue
        for item in value.get('music_occurrences', []):
            if isinstance(item, Mapping) and item.get('subject_id') == 'k' and (item.get('temporal_status') == 'occurred') and isinstance(item.get('pattern_key'), str) and item['pattern_key'].startswith('music:') and isinstance(item.get('occurrence_key'), str) and item['occurrence_key']:
                records.append({key: item[key] for key in ('subject_id', 'pattern_key', 'occurrence_key', 'temporal_status')})
    return tuple(records)

def memory_source_content(row: Mapping[str, Any]) -> str | None:
    """Return projected text, or None when a notification is not a fact source.

    Older assistant rows without receipt event IDs keep their original digest.
    The ambient notification already entered V2 before this change, so its
    existing source text must also remain byte-for-byte stable.
    """
    role = str(row.get('role') or '')
    content = str(row.get('content') or '')
    event_type = str(row.get('event_type') or '')
    active_date = str(row.get('active_date') or '')
    rich_domain_event = active_date >= RICH_DOMAIN_EVENT_START_ACTIVE_DATE
    scope_fact = ''
    if role == 'event':
        if event_type == 'wander_activity':
            from context_builder.wander_event_projection import format_wander_activity_event
            calls = _calls(row.get('tool_calls'))
            first = calls[0] if calls else {}
            extra = first.get('extra_data') if isinstance(first, dict) else None
            current_receipt = isinstance(extra, dict) and (extra.get('wander_projection_version') == 2
                or isinstance(extra.get('emotion_effect'), dict) or extra.get('interrupted_review_version') == 1)
            if current_receipt:
                from context_builder.wander_event_projection import RETENTION_WEAK, retention_tier
                if retention_tier(calls) == RETENTION_WEAK:
                    return format_wander_activity_event(content, calls, level='action',
                        include_interrupted_review=extra.get('interrupted_review_version') == 1)
            if isinstance(extra, dict) and isinstance(extra.get('memory_experience'), dict):
                from .experience_contract import project, project_legacy_public
                render = project if current_receipt else project_legacy_public
                return render(extra['memory_experience'])
            level = 'experience' if isinstance(extra, dict) and 'retained_experience' in extra else 'detail'
            return format_wander_activity_event(content, calls, level=level)
        if event_type == 'tool_event' and any((call.get('tool') == 'xiaohongshu_interactions' for call in _calls(row.get('tool_calls')))):
            return '\n'.join(filter(None, (project_tool_event(call) for call in _calls(row.get('tool_calls'))))) or None
        from context_builder.event_history_adapter import event_context_fact
        return event_context_fact(row) or None
    if role == 'notification':
        if event_type == 'ambient_listening_observation':
            return content
        if active_date < SOURCE_PROJECTION_START_ACTIVE_DATE:
            return None
        return build_notification_context_fact(row) or None
    if role == 'system':
        return content if content.strip() else None
    if role == 'user' and active_date >= SOURCE_PROJECTION_START_ACTIVE_DATE:
        if event_type == 'ui_action:play_game':
            from wander_manager.game_invitation import invitation_fact
            return '[界面快捷邀请] ' + invitation_fact(content)
        if event_type == 'ui_action:visit_workshop':
            from .workshop_invitation_projection import invitation_fact
            return '[界面快捷邀请] ' + invitation_fact(content)
        card_fact = long_text_card_fact(row.get('long_text_card'))
        return '\n'.join((part for part in (content, card_fact) if part)) if card_fact else content
    if role != 'assistant':
        return content
    if active_date < SOURCE_PROJECTION_START_ACTIVE_DATE:
        return content
    facts: list[str] = []
    seen: set[str] = set()
    for ordinal, call in enumerate(_calls(row.get('tool_calls'))):
        event_id = str(call.get('event_id') or '')
        occurred_at = str(call.get('completed_at') or row.get('timestamp') or '')
        receipt_key = event_id if event_id.startswith('tool_event:') else f'row:{ordinal}'
        if receipt_key in seen:
            continue
        seen.add(receipt_key)
        fact = _card_fact(call, rich_domain_event=rich_domain_event) or project_tool_event(call)
        if fact:
            facts.append(f'[{occurred_at}] {fact}')
    if not facts:
        return content
    if scope_fact:
        facts.append(scope_fact)
    return '\n'.join([*facts, content] if content.strip() else facts)
__all__ = ['SOURCE_PROJECTION_VERSION', 'SOURCE_PROJECTION_START_ACTIVE_DATE', 'RICH_DOMAIN_EVENT_START_ACTIVE_DATE', 'SOURCE_SCOPE_START_ACTIVE_DATE', 'long_text_card_fact', 'memory_source_content']
