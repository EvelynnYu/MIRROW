"""One bounded textual view of a settled Wander timeline event."""

from __future__ import annotations

import json
from typing import Any


def format_wander_activity_event(summary: str, tool_calls: Any) -> str:
    calls = tool_calls
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (TypeError, ValueError):
            calls = []
    first = calls[0] if isinstance(calls, list) and calls else {}
    description = (
        str(first.get("description") or "").strip()[:60]
        if isinstance(first, dict) else ""
    ) or "一次漫想活动已结算"
    thought = str(summary or "").strip()
    return f"Agent 的漫想事件：{description}。" + (
        f"当时的结算感受：{thought}" if thought else ""
    )


__all__ = ["format_wander_activity_event"]
