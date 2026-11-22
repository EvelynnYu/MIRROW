"""Read old conversation rows and new event rows through one context boundary.

``conversation_messages`` remains the only authority.  Legacy receipts and
notifications are projected at read time; a new ``role=event`` row is already
an independent occurrence.  Provider-facing callers receive only supported
chat roles, never the internal event role or raw event payload.
"""
from __future__ import annotations
import json
from typing import Any, Mapping
from .notification_projection import build_notification_context_fact, project_notification_message
from .tool_event_projection import merge_tool_receipt_events, project_tool_event

def _calls(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

def event_context_fact(row: Mapping[str, Any], *, wander_level: str='experience') -> str:
    """Return a bounded fact for a durable event, or empty when not eligible."""
    event_type = str(row.get('event_type') or row.get('eventType') or '')
    if event_type == 'workshop_event':
        from workshop.context import event_fact
        return event_fact(row)
    if event_type == 'tool_event':
        return '\n'.join(filter(None, (project_tool_event(call) for call in _calls(row.get('tool_calls') or row.get('toolCalls')))))
    if event_type == 'wander_activity':
        from .wander_event_projection import format_wander_activity_event
        return format_wander_activity_event(str(row.get('content') or ''), row.get('tool_calls') or row.get('toolCalls'), str(row.get('message_id') or row.get('id') or ''), level=wander_level, timestamp=str(row.get('timestamp') or ''), live_reading=True)
    known = build_notification_context_fact(row)
    if known:
        return known
    content = str(row.get('content') or '').strip()
    if content:
        from .text_utils import _redact_tool_fact
        return f'[事件记录] {_redact_tool_fact(content)}'
    return ''

def project_event_for_context(row: Mapping[str, Any], *, wander_level: str='experience') -> dict[str, Any] | None:
    """Project an internal event into a provider-safe system history item."""
    if row.get('event_type') == 'model_switch':
        from model_provenance import annotate_history
        return annotate_history(dict(row))
    fact = event_context_fact(row, wander_level=wander_level)
    if not fact:
        return None
    projected = dict(row)
    projected.update(role='system', content=fact, tool_calls=None, tool_summary=None, notification_projection=True)
    return projected

def adapt_history_for_context(rows: list[dict[str, Any]], *, wander_level: str='experience', for_provider: bool=False, compact_linked_sources: bool=True) -> list[dict[str, Any]]:
    """One read adapter for old and new rows; never mutate the stored rows."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.setdefault('message_id', item.get('id'))
        item.setdefault('event_type', item.get('eventType'))
        item.setdefault('tool_calls', item.get('toolCalls'))
        normalized.append(item)
    projected: list[dict[str, Any]] = []
    explicit_ids = {str(row.get('message_id') or '') for row in normalized if row.get('role') == 'event'}
    merged = merge_tool_receipt_events(normalized)
    for row in merged:
        role = str(row.get('role') or '')
        if role == 'event':
            item = project_event_for_context(row, wander_level=wander_level)
        elif role == 'notification':
            item = project_notification_message(row)
        elif role == 'user' and row.get('event_type') == 'ui_action:visit_workshop':
            from workshop.ui_action import invitation_fact
            item = {**row, 'role': 'system', 'content': '[界面快捷邀请] ' + invitation_fact(row.get('content', ''), row.get('timestamp', ''))}
        elif role == 'divider':
            item = None
        elif role == 'system' and str(row.get('message_id') or '') in explicit_ids:
            item = None
        elif role in {'user', 'assistant', 'system'}:
            item = dict(row)
        else:
            item = None
        if item:
            projected.append(item)
    if for_provider:
        from pathlib import Path
        from .provider_presentation import fact_message, experience_text
        originals = {str(row.get('message_id') or ''): row for row in merged if row.get('message_id')}
        selected = [originals[mid] for item in projected if (mid := str(item.get('message_id') or '')) in originals]
        texts = {str(item.get('message_id') or ''): str(item.get('content') or '') for item in projected}
        overrides = {}
        if compact_linked_sources and wander_level == 'detail':
            from .workshop_material_projection import projection_overrides as workshop_overrides
            overrides.update(workshop_overrides(selected, db_path=Path(__file__).resolve().parents[1] / 'events' / 'wander_runtime.db', provided_texts=texts))
        for item in projected:
            mid = str(item.get('message_id') or '')
            from .wander_narration import identity
            source = originals.get(mid, item)
            item['_wander_identity'] = identity(source)
            if item.get('event_type') == 'wander_activity' and wander_level in {'daily', 'experience'}:
                from .wander_event_projection import inner_event_text
                item['content'] = inner_event_text(source, frozen_only=wander_level == 'experience',
                    include_completed_review=wander_level == 'daily')
            elif mid in overrides:
                item['content'] = overrides[mid]
            if item.get('notification_projection') or item.get('event_type') == 'tool_event':
                view = fact_message(item, str(item.get('content') or ''))
                item.update(view)
                item['_provider_internal'] = view.get('name') == 'mirrow_inner_record'
    return projected
__all__ = ['adapt_history_for_context', 'event_context_fact', 'project_event_for_context']
