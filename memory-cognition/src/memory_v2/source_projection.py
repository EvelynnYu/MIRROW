"""Stable Memory V2 text derived from the one conversation-row authority.

The projection is deliberately limited to facts persisted *in that row*.
Side databases may enrich the live context, but their mutable contents cannot
silently change a settled batch's source digest or quotation spans.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from context_builder.notification_projection import build_notification_context_fact
from context_builder.tool_event_projection import project_tool_event
from context_builder.text_utils import _redact_tool_fact, _tool_fact_status


# Older completed days already have immutable source manifests.  Only newly
# active days use receipt-aware projection; this is a read rule, not a second
# event store or a migration of historical rows.  Keep this v1 projection
# stable for its active-date cohort; a future change needs a new cutover branch
# so completed batch digests and quote spans remain verifiable.
SOURCE_PROJECTION_VERSION = 1
SOURCE_PROJECTION_START_ACTIVE_DATE = "2026-09-24"


def _calls(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _count(value: Any) -> int:
    try:
        return max(0, min(int(value), 10_000))
    except (TypeError, ValueError, OverflowError):
        return 0


def long_text_card_fact(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    if not isinstance(raw, Mapping) or not str(raw.get("body") or "").strip():
        return ""
    title = _redact_tool_fact(raw.get("title") or "无标题")
    if raw.get("kind") == "music":
        return f"[音乐卡片] 人类伙伴发送了一张音乐卡片《{title}》。卡片原文保存在原消息附件中。"
    return f"[长文卡片] 人类伙伴发送了一张长文卡片《{title}》。卡片全文保存在原消息附件中。"


def _card_fact(call: Mapping[str, Any]) -> str:
    extra = call.get("extra_data")
    if not isinstance(extra, Mapping):
        return ""
    status = _tool_fact_status(dict(call))
    if isinstance(extra.get("theater_event"), Mapping):
        return f"[小剧场事件] Agent留下了小剧场记录。结果：{status}。卡片关联的作品详情由小剧场记录保存。"
    if isinstance(extra.get("lounge_visit"), Mapping):
        card = extra["lounge_visit"]
        partner = _redact_tool_fact(card.get("partner_name") or "一位好友")
        return f"[会客事件] Agent与{partner}发生了一次会客。结果：{status}。"
    if isinstance(extra.get("music_card"), Mapping):
        card = extra["music_card"]
        title = _redact_tool_fact(card.get("name") or card.get("title") or "一首歌曲")
        artist = _redact_tool_fact(card.get("artist"))
        return f"[音乐事件] Agent处理了《{title}》" + (f"（{artist}）" if artist else "") + f"。结果：{status}。"
    if extra.get("attachment_kind") == "music":
        return f"[音乐事件] Agent发起了一项音乐操作。结果：{status}。"
    if str(call.get("tool") or "") == "generate_image":
        return f"[图像事件] Agent生成图片。结果：{status}。"
    if str(call.get("tool") or "") == "send_voice":
        return f"[语音事件] Agent发送语音。结果：{status}。"
    return ""


def memory_source_content(row: Mapping[str, Any]) -> str | None:
    """Return projected text, or None when a notification is not a fact source.

    Older assistant rows without receipt event IDs keep their original digest.
    The ambient notification already entered V2 before this change, so its
    existing source text must also remain byte-for-byte stable.
    """

    role = str(row.get("role") or "")
    content = str(row.get("content") or "")
    event_type = str(row.get("event_type") or "")
    active_date = str(row.get("active_date") or "")
    if role == "notification":
        if event_type == "ambient_listening_observation":
            return content
        if active_date < SOURCE_PROJECTION_START_ACTIVE_DATE:
            return None
        if event_type == "browse_taobao":
            for call in _calls(row.get("tool_calls")):
                extra = call.get("extra_data")
                card = extra.get("taobao_trip") if isinstance(extra, Mapping) else None
                if isinstance(card, Mapping):
                    count = _count(card.get("count"))
                    added = _count(card.get("cart_added_count"))
                    return (f"[淘宝事件] Agent逛淘宝并保存了{count}件商品到本地心愿袋；"
                            f"其中{added}件已加购。没有下单或付款。商品详情仍在原卡片中。")
            return None
        return build_notification_context_fact(row) or None
    if role == "system":
        if (event_type != "group_chat_summary"
                or active_date < SOURCE_PROJECTION_START_ACTIVE_DATE
                or "----------群聊摘要----------" in content):
            return None
        return "[群聊派生摘要，非原话] " + content if content.strip() else None
    if role == "user" and active_date >= SOURCE_PROJECTION_START_ACTIVE_DATE:
        card_fact = long_text_card_fact(row.get("long_text_card"))
        return "\n".join(part for part in (content, card_fact) if part) if card_fact else content
    if role != "assistant":
        return content
    if active_date < SOURCE_PROJECTION_START_ACTIVE_DATE:
        return content
    facts: list[str] = []
    seen: set[str] = set()
    for ordinal, call in enumerate(_calls(row.get("tool_calls"))):
        event_id = str(call.get("event_id") or "")
        occurred_at = str(call.get("completed_at") or row.get("timestamp") or "")
        receipt_key = event_id if event_id.startswith("tool_event:") else f"row:{ordinal}"
        if receipt_key in seen:
            continue
        seen.add(receipt_key)
        fact = _card_fact(call) or project_tool_event(call)
        if fact:
            facts.append(f"[{occurred_at}] {fact}")
    if not facts:
        return content
    return "\n".join([*facts, content] if content.strip() else facts)


__all__ = ["SOURCE_PROJECTION_VERSION", "SOURCE_PROJECTION_START_ACTIVE_DATE",
           "long_text_card_fact", "memory_source_content"]
