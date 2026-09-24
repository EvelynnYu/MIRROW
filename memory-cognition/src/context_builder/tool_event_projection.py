"""Objective LLM-facing projection of one persisted tool event.

The original receipt stays in ``conversation_messages.tool_calls``.  This
module deliberately takes a single receipt, so an event-stream reader can
place it at its own occurrence time instead of after the final assistant NL.
"""

from __future__ import annotations

import re
import json
from typing import Any, Mapping

from .text_utils import _NON_CONTEXT_TOOLS, _TOOL_LABELS, _redact_tool_fact, _tool_fact_status


_RETRIEVAL_TOOLS = {"bookshelf", "web_search", "fetch_source_details"}
_SENSORY_TOOLS = {"eyes", "check_phone"}
_INDEPENDENT_EVENT_TOOLS = {"ambient_event_link"}
_BINARY_RUN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/])")
_QUOTED_SECRET = re.compile(
    r'(?i)(["\'](?:api[_-]?key|access[_-]?token|token|secret|password|authorization|cookie)["\']\s*:\s*["\'])[^"\']+'
)


def _safe_text(value: Any) -> str:
    """Retain textual results while excluding common inline binary payloads."""
    text = _redact_tool_fact(value)
    text = _QUOTED_SECRET.sub(r"\1[已隐藏]", text)
    return _BINARY_RUN.sub("[二进制数据已省略]", text)


def _query_subject(params: Any) -> str:
    if not isinstance(params, Mapping):
        return ""
    # Queries may be nested by the tool compiler.  Preserve only the search
    # subject, never the full parameter object or credentials.
    for key in ("query", "search_query", "keyword", "keywords", "topic", "source_id"):
        value = params.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return _safe_text(value)
    return ""


def project_tool_event(receipt: Mapping[str, Any] | Any) -> str:
    """Return one source-labelled fact, or empty for separately-owned events."""
    if not isinstance(receipt, Mapping):
        return ""
    name = str(receipt.get("tool") or receipt.get("name") or "").strip()
    if (not name or name in _NON_CONTEXT_TOOLS
            or name in _INDEPENDENT_EVENT_TOOLS):
        return ""
    extra = receipt.get("extra_data") if isinstance(receipt.get("extra_data"), Mapping) else {}
    if name == "band_touch" and extra.get("band_notice_id"):
        return ""  # The separately persisted notification owns this fact.
    if (extra.get("taobao_trip") or extra.get("lounge_visit") or extra.get("theater_card")
            or extra.get("theater_event")
            or extra.get("music_card") or extra.get("attachment_kind") == "music"
            or extra.get("xhs_comment")):
        return ""  # Existing domain cards own these specialized event facts.
    status = _tool_fact_status(dict(receipt))
    if name == "generate_image":
        return f"[图像事件] Agent生成了一张图片。结果：{status}。"
    if name == "send_voice":
        return f"[语音事件] Agent发送了一段语音。结果：{status}。"
    if name == "intimacy_book":
        params = receipt.get("parameters")
        action = str(params.get("action") or "") if isinstance(params, Mapping) else ""
        verb = "随机组合抽卡" if action == "combine" else "随机查询写作素材"
        return f"[工具事件] Agent翻阅《性爱大全》并{verb}。结果：{status}。"
    label = _TOOL_LABELS.get(name, "执行一项操作")
    if name in _RETRIEVAL_TOOLS:
        subject = _query_subject(receipt.get("parameters"))
        description = _safe_text(receipt.get("description"))
        what = subject or description or "资料"
        # Retrieval material is available in its original receipt but is not
        # replayed on every following turn.  Absence/failure remains explicit.
        retrieval_label = {"bookshelf": "翻阅书柜", "web_search": "检索公开资料", "fetch_source_details": "读取资料原文"}[name]
        return f"[工具事件] Agent{retrieval_label}，查找：{what}。结果：{status}。"
    description = _safe_text(receipt.get("description"))
    result = _safe_text(receipt.get("result"))
    error = _safe_text(receipt.get("error"))
    if name in _SENSORY_TOOLS:
        # Camera/screen results often contain a large analysis already told in
        # the adjacent NL; retain the outcome without duplicating the scene.
        detail = description or error or result
        return f"[感知事件] Agent{label}。结果：{status}。" + (f"{detail}" if detail else "")
    parts = [f"[工具事件] Agent{label}。结果：{status}。"]
    if description:
        parts.append(f"行动：{description}。")
    if result:
        parts.append(f"回执：{result}。")
    if error and error != result:
        parts.append(f"错误：{error}。")
    return "".join(parts)


def merge_tool_receipt_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive chronological events from the original message table alone.

    New receipts carry their own ID and completion time inside the existing
    ``tool_calls`` JSON.  Historical receipts without both fields remain on
    the legacy message-attached projection.  No derived data is persisted.
    """
    from time_utils import parse_ts

    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for message in messages:
        current = dict(message)
        raw = current.get("tool_calls")
        if raw:
            try:
                calls = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                calls = None
            if isinstance(calls, list):
                remaining = []
                for call in calls:
                    event_id = str(call.get("event_id") or "") if isinstance(call, dict) else ""
                    occurred_at = str(call.get("completed_at") or "") if isinstance(call, dict) else ""
                    fact = project_tool_event(call)
                    if not (event_id.startswith("tool_event:") and occurred_at and fact):
                        remaining.append(call)
                        continue
                    if event_id not in seen_ids:
                        seen_ids.add(event_id)
                        merged.append({
                            "role": "system",
                            "content": fact,
                            "timestamp": occurred_at,
                            "message_id": event_id,
                            "session_id": current.get("session_id", ""),
                            "active_date": current.get("active_date", ""),
                            "event_type": "tool_event",
                            "tool_calls": None,
                            "tool_summary": None,
                        })
                if len(remaining) != len(calls):
                    current["tool_calls"] = json.dumps(remaining, ensure_ascii=False)
                    if not remaining:
                        current["tool_summary"] = None
        merged.append(current)
    return sorted(merged, key=lambda item: parse_ts(str(item.get("timestamp") or "")))


__all__ = ["project_tool_event", "merge_tool_receipt_events"]
