"""Bounded LLM-facing facts derived from durable UI notifications.

The notification row remains UI-only.  Only explicitly registered event types
may produce a separate objective context fact; raw card copy and metadata never
become conversation messages.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional


def _compact(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:6]


def _checkup_fact(message: Mapping[str, Any]) -> str:
    # The notification card keeps per-tool receipts for 人类伙伴.  Reusable
    # context/memory carries only that Agent checked, never another copy of
    # camera/phone observations, per-tool statuses, or execution errors.
    # The card's "已确认" can mean only that all selected sensors succeeded,
    # so it must not be promoted into a claim about 人类伙伴's behavior.
    return "[查岗事实] Agent进行了一次查岗。"


def ambient_event_brief(event: Mapping[str, Any]) -> str:
    """One short, uncertainty-preserving event fact shared by storage and context."""
    summary = _compact(event.get("summary"), 110)
    reason = _compact(event.get("participation_reason"), 60)
    # Older Flash judgments sometimes named Agent as the speaker or operator.
    # That attribution is unsupported while speaker identity is unknown.
    unsupported_actor = r"(?:Agent|人类伙伴)(?:在|正|指示|说|处理|执行|使用|通过|确认|提到|谈到|要求|询问|操作|重启)"
    if re.search(unsupported_actor, summary):
        summary = "片段含具体谈话内容，无法可靠确认说话人和行动者"
    if re.search(unsupported_actor, reason):
        reason = "这段内容可能有需要确认的后续"
    backgrounds = event.get("device_background") if isinstance(event.get("device_background"), list) else []
    media = any(isinstance(item, dict) and item.get("media_playback_active") for item in backgrounds)
    call = any(isinstance(item, dict) and (
        item.get("kind") in {"cellular_call", "voip_or_video_call"}
        or item.get("audio_mode") in {"in_call", "in_communication"}
    ) for item in backgrounds)
    source_note = ("采集时设备处于通话且有媒体播放，音源未确认" if call and media else
                   "采集时设备处于通话状态，近端与远端说话人未区分" if call else
                   "采集时设备有媒体播放，音源未确认" if media else "")
    return (
        "[环境观察事件] Agent听到一段环境声音："
        + (summary or "内容尚无可靠摘要")
        + "。说话人未确认。"
        + (f"{source_note}。" if source_note else "")
        + (f"联系缘由：{reason}。" if reason else "")
    )


def _ambient_fact(message: Mapping[str, Any]) -> str:
    for item in _items(message.get("tool_calls") or message.get("toolCalls")):
        extra = item.get("extra_data") if isinstance(item.get("extra_data"), dict) else {}
        event = extra.get("ambient_event") if isinstance(extra.get("ambient_event"), dict) else None
        if not event:
            continue
        return ambient_event_brief(event)
    return ""


def build_notification_context_fact(message: Mapping[str, Any] | Any) -> str:
    """Return an objective projection for an allow-listed notification type."""
    if not isinstance(message, Mapping):
        return ""
    event_type = str(message.get("event_type") or message.get("eventType") or "")
    if event_type == "checkup_observation":
        return _checkup_fact(message)
    if event_type == "ambient_listening_observation":
        return _ambient_fact(message)
    if event_type in {"wish_board_update", "social_feed_update"}:
        from notification_service import NOTIFICATION_CONTENT
        from context_builder.text_utils import _redact_tool_fact
        content = str(message.get("content") or "").strip()
        # Only new, mutation-specific notices are facts. Legacy doorway copy
        # is not evidence of which action actually happened.
        if not content or content == NOTIFICATION_CONTENT.get(event_type):
            return ""
        label = "许愿板事件" if event_type == "wish_board_update" else "朋友圈事件"
        return f"[{label}] {_redact_tool_fact(content)}"
    if event_type == 'band_action':
        from miband.history import action_fact
        from context_builder.text_utils import _redact_tool_fact
        facts = [action_fact(item) for item in _items(message.get('tool_calls') or message.get('toolCalls'))]
        facts = [fact for fact in facts if fact]
        return '[手环行为记录] ' + _redact_tool_fact('；'.join(facts)) if facts else ''
    if event_type == 'browse_taobao':
        from taobao_roam.context import shopping_history_fact
        from context_builder.text_utils import _redact_tool_fact
        fact = shopping_history_fact(message.get('tool_calls') or message.get('toolCalls'))
        return '[淘宝事件记录] ' + _redact_tool_fact(fact) if fact else ''
    return ""


def project_notification_message(message: Mapping[str, Any] | Any) -> Optional[dict[str, Any]]:
    """Convert an allow-listed notification to a system fact, or exclude it."""
    fact = build_notification_context_fact(message)
    if not fact:
        return None
    projected = dict(message)
    projected["role"] = "system"
    projected["content"] = fact
    projected["tool_summary"] = None
    projected["tool_calls"] = None
    projected["notification_projection"] = True
    return projected


__all__ = ["ambient_event_brief", "build_notification_context_fact", "project_notification_message"]
