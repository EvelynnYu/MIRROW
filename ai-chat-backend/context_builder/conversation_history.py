"""Shared projection of selected chronicle records into conversational history."""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import logging
from typing import Any, Dict

from message_roles import is_ui_only_role
from .text_utils import build_tool_history_fact, strip_internal_history_markers

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """记忆项"""
    id: str
    content: str
    similarity: float
    importance: float
    bucket: str
    source: str  # "ob_rev", "conversation"
    metadata: Dict[str, Any] = field(default_factory=dict)


def daily_memory_items(rows, session_id):
    """Keep the shared daily event view, long cards and message provenance."""
    # Main chat and push history use the same legacy/new event read
    # boundary.  The provider never receives role=event directly.
    from context_builder.event_history_adapter import adapt_history_for_context
    # Main chat reads the bounded daily-entry view; offline day-end and
    # cognitive maintenance keep their full experience projection.
    filtered_messages = adapt_history_for_context(
        rows, for_provider=True, wander_level="daily")

    for i, msg in enumerate(filtered_messages):
        msg_id = msg.get("message_id") or msg.get("id") or ""

        # 解析 voice_attachment（人类伙伴语音元数据 JSON 串 → dict，供上下文注入 （语音·tone） 标记）
        _va_raw = msg.get("voice_attachment")
        _va_meta = None
        if _va_raw:
            try:
                _va_meta = json.loads(_va_raw) if isinstance(_va_raw, str) else _va_raw
            except Exception:
                _va_meta = None

        _content = str(msg.get("content", ""))
        if msg.get("role") != "user" and not msg.get('notification_projection'):
            from tool_action_protocol import strip_tool_protocol_text
            _content = strip_tool_protocol_text(_content)
        if msg.get("role") == "user" and msg.get("long_text_card"):
            try:
                from long_text_cards import card_for_history
                from settings_manager import get_setting
                _card = msg.get("long_text_card")
                if isinstance(_card, str):
                    _card = json.loads(_card)
                _card_text = card_for_history(
                    _card,
                    bool(get_setting("longTextCardFullContext")),
                )
                if _card_text:
                    _content = f"{_content}\n{_card_text}" if _content else _card_text
            except Exception as _card_exc:
                logger.warning("长文卡片历史注入跳过: %s", type(_card_exc).__name__)
        if msg.get("event_type") == "rift_game":
            _content = f"[裂隙档案·虚构共同游玩] {_content}"
        yield MemoryItem(
            id=f"conversation_{session_id}_{i}_{hashlib.md5(str(msg.get('content', '')).encode()).hexdigest()[:8]}",
            content=f"{msg.get('role', 'unknown')}: {_content}",
            similarity=0.5,
            importance=0.6,  # 对话消息中等重要性
            bucket="short_term",
            source="conversation",
            metadata={
                "role": msg.get("role"),
                "timestamp": msg.get("timestamp"),
                "session_id": session_id,
                "message_id": msg_id,
                "is_active_topic": True,  # 标记为活跃话题
                "reasoning_content": msg.get("reasoning_content"),
                # 仅保留已持久化的短工具摘要；原始 tool_calls/参数/图片不进上下文。
                "tool_summary": msg.get("tool_summary"),
                # 只交给展示边界提取执行事实；helper 不读取参数/extra_data。
                "tool_calls": msg.get("tool_calls"),
                "is_wander": bool(msg.get("is_wander")) or str(msg_id).startswith("wander_"),
                "is_sentinel": bool(msg.get("is_sentinel")) or str(msg_id).startswith("sentinel_"),
                "is_reminder": bool(msg.get("is_reminder")) or str(msg_id).startswith("reminder_"),
                "event_type": str(msg.get("event_type") or ""),
                "notification_projection": bool(msg.get("notification_projection")),
                "provider_internal": bool(msg.get("_provider_internal")),
                "wander_identity": msg.get('_wander_identity'),
                "voice_attachment": _va_meta,  # 人类伙伴语音元数据 {path,text,duration,tone}
            }
        )

def render_history(mems, recipe_key, excluded_event_ids=()):
    """Format the selected history once, retaining roles and source ownership."""
    from .ingredients import format_relative_time
    result = []
    mems = [m for m in mems if getattr(m, 'metadata', {}).get('message_id') not in excluded_event_ids]
    from .workshop_material_projection import reproject_selected_messages
    mems = reproject_selected_messages(mems)
    from api_sources.model_context import labels_for_ids
    generation_models = labels_for_ids(
        (getattr(m, 'metadata', {}).get('message_id') or getattr(m, 'id', '') for m in mems))
    # Strong feelings may stay in context all day, but keep the full
    # prose bounded; older strong events degrade to a one-line brief.
    now = datetime.now().astimezone()
    full_strong_ids = set()
    if recipe_key == 'FULL_CHAT':
        from context_builder.wander_event_projection import RETENTION_STRONG, retention_tier
        strong_ids = []
        for event_message in mems:
            event_meta = getattr(event_message, 'metadata', {}) if hasattr(event_message, 'metadata') else {}
            if event_meta.get('event_type') != 'wander_activity':
                continue
            if retention_tier(event_meta.get('tool_calls')) == RETENTION_STRONG:
                strong_ids.append(str(event_meta.get('message_id') or getattr(event_message, 'id', '') or ''))
        full_strong_ids = set(strong_ids[-5:])
    for m in mems:
        meta = getattr(m, 'metadata', {}) if hasattr(m, 'metadata') else {}
        role = meta.get('role', 'user')
        if is_ui_only_role(role):
            continue  # 未投影的 divider/notification 仍只供 UI
        ts = meta.get('timestamp', '')
        if meta.get('provider_internal'):
            from .text_utils import strip_legacy_speaker_prefix
            result.append({'role': 'assistant', 'name': 'mirrow_inner_record',
                'content': strip_legacy_speaker_prefix(str(getattr(m, 'content', '') or ''), role),
                '_ts': ts, '_section': 'wander_activity_event' if meta.get('event_type') == 'wander_activity' else 'tool_history_fact',
                '_message_id': meta.get('message_id') or getattr(m, 'id', ''),
                '_wander_identity': meta.get('wander_identity')})
            continue
        if meta.get('event_type') == 'wander_activity' and role == 'system':
            from context_builder.wander_event_projection import (
                format_wander_activity_event, format_wander_activity_for_chat,
            )
            content = str(getattr(m, 'content', '') or '')
            message_id = str(meta.get('message_id') or getattr(m, 'id', '') or '')
            if meta.get('notification_projection'):
                fact_text = content
            elif recipe_key == 'FULL_CHAT':
                fact_text, _tier = format_wander_activity_for_chat(
                    content, meta.get('tool_calls'), message_id,
                    timestamp=str(ts or ''), now=now,
                    strong_full=message_id in full_strong_ids)
            else:
                fact_text = format_wander_activity_event(
                    content, meta.get('tool_calls'), message_id,
                    level='experience', timestamp=str(ts or ''), live_reading=True)
            from .provider_presentation import internal_event_message
            fact = internal_event_message({'timestamp': ts, 'event_type': 'wander_activity'}, fact_text)
            result.append({**fact, '_ts': ts,
                           '_message_id': meta.get('message_id') or getattr(m, 'id', ''),
                           '_section': 'wander_activity_event'})
            continue
        content = str(getattr(m, 'content', '')) if hasattr(m, 'content') else str(m)
        # 只剥离明确识别出的旧式说话人前缀。普通正文中的
        # ``: `` 可能承载否定、条件或引用，不能据此猜测说话人。
        from context_builder.text_utils import strip_legacy_speaker_prefix
        content = strip_legacy_speaker_prefix(content, role)
        # 近期错误版本可能把内部说话人标签写进了助手正文；不让污染继续回灌。
        if role == "assistant":
            content = strip_internal_history_markers(content)
            from tool_action_protocol import strip_tool_protocol_text
            content = strip_tool_protocol_text(content)
        time_label = format_relative_time(ts)
        is_wander = meta.get('is_wander')
        is_sentinel = meta.get('is_sentinel')
        is_reminder = meta.get('is_reminder')
        # 主聊天由 API role 区分说话人，正文不混入姓名标签；普通 assistant
        # 也不加时间，避免模型在回复中模仿时间/说话人前缀。
        if is_sentinel:
            label = f"[🛡️ {time_label}] " if time_label else "[🛡️] "
        elif is_reminder:
            label = f"[⏰ {time_label}] " if time_label else "[⏰] "
        elif is_wander:
            label = f"[💭 {time_label}] " if time_label else "[💭] "
        elif role == "user":
            label = f"[{time_label}] " if time_label else ""
            # 人类伙伴语音消息：附加 （语音·tone） 元信息（对齐昨日对话格式，trap 229 语义同 [🛡️]/[💭] label）
            _va = meta.get("voice_attachment")
            if _va and isinstance(_va, dict):
                _tone = _va.get("tone") if _va.get('input_preparation') != 'native' else None
                label += f"（语音·{_tone}）" if _tone else "（语音）"
        elif role == "assistant":
            label = ""
        elif time_label:
            label = f"[{time_label}] "
        else:
            label = ""
        generation_model = generation_models.get(str(meta.get('message_id') or getattr(m, 'id', '')))
        model_header = f'【生成模型：{generation_model}】\n' if role == 'assistant' and generation_model else ''
        result.append({"role": role, "content": f"{model_header}{label}{content}", "_ts": ts,
                       "_section": "conversation_messages", "_is_wander": bool(is_wander),
                       "_message_id": meta.get("message_id") or getattr(m, "id", ""),
                       "_spoken_content": content})
        fact = build_tool_history_fact(role, meta.get("tool_summary"), meta.get("tool_calls"), meta.get('event_type', ''))
        if fact:
            fact_role = 'assistant' if role == 'assistant' else 'system'
            # One source heading; legacy markers stay in the storage
            # adapter, not as another template for AI's natural reply.
            provider_fact = fact.removeprefix('[历史行为事实] ')
            provider_fact = '\n'.join('你' + line[2:] if line.startswith('AI') else line
                                      for line in provider_fact.splitlines())
            result.append({"role": fact_role, "content": '[内在活动记录]\n' + provider_fact,
                           "_ts": ts, "_section": "tool_history_fact",
                           "_message_id": meta.get('message_id') or getattr(m, 'id', ''),
                           **({'name': 'mirrow_inner_record'} if fact_role == 'assistant' else {})})
    return result


def fallback_history(rows, session_id):
    from .status_history import history_sort_key
    from .wander_narration import narrate
    items = render_history(list(daily_memory_items(rows, session_id)), 'FULL_CHAT')
    return narrate(sorted(items, key=history_sort_key))
