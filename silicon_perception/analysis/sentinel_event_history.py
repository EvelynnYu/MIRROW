"""Render prior sentinel events without recycling a Flash judgment as a sensor fact."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


_TRIGGER_MARKER = "\n[触发标识]"
_HISTORY_TRIGGER_LABELS = {
    "wifi_home_arrival": "家庭 Wi-Fi 连接变化",
    "gps_category_change": "位置类别变化",
    "gps_moved": "位置移动",
    "gps_stable_stop": "两次定位未见显著位移",
    "steps_burst": "步数突增",
    "steps_stagnant": "步数一段时间未变化",
    "app_session_long": "应用使用时长",
    "status_change_silent": "状态变化后的静默",
    "periodic_check": "定期检查",
    "hr_surge": "心率变化",
    "hr_drop": "心率变化",
    "hr_sustained_high": "心率变化",
}


def format_push_audit(observation: str, motivation: str, triggers: Sequence[str]) -> str:
    """Keep the original audit text, with source-owned trigger IDs for future history views."""
    observation = (observation or "").strip()
    motivation = (motivation or "").strip()
    audit = f"{observation} | 主动联系原因:{motivation}" if motivation else observation
    trigger_ids = [item[:80] for item in triggers[:8] if isinstance(item, str) and item]
    return f"{audit}{_TRIGGER_MARKER}{json.dumps(trigger_ids, ensure_ascii=False)}"


def _history_trigger_label(trigger_id: str) -> str:
    if trigger_id.startswith("behavior_state_change:"):
        return "设备使用状态变化"
    if trigger_id.startswith("cross_source_attention:"):
        return "跨来源状态对照"
    return _HISTORY_TRIGGER_LABELS.get(trigger_id, "其他感知变化")


def _push_trigger_history(message: str) -> str:
    _, marker, encoded = message.rpartition(_TRIGGER_MARKER)
    if not marker:
        return "触发类型未记录"
    try:
        trigger_ids = json.loads(encoded)
    except (TypeError, ValueError):
        return "触发类型未记录"
    if not isinstance(trigger_ids, list):
        return "触发类型未记录"
    labels = []
    for trigger_id in trigger_ids[:8]:
        if not isinstance(trigger_id, str):
            continue
        label = _history_trigger_label(trigger_id)
        if label not in labels:
            labels.append(label)
    return "、".join(labels) if labels else "触发类型未记录"


def render_today_event_history(events: Sequence[Mapping[str, Any]]) -> str:
    """Show Flash pushes as prior actions; retain independent event messages."""
    if not events:
        return "无异常"
    lines = []
    for event in events[:5]:
        timestamp = str(event.get("timestamp") or "")
        time_label = timestamp[11:16] if len(timestamp) >= 16 else ""
        rule_id = str(event.get("rule_id") or "")
        message = str(event.get("message") or "")
        if rule_id == "flash_push":
            trigger_label = _push_trigger_history(message)
            lines.append(
                f"- {time_label} {trigger_label}唤醒过一次主动联系尝试"
                "（历史动作；当时的观察与理由不代表当前事实）"
            )
        else:
            lines.append(f"- {time_label} {message or rule_id}")
    return "\n".join(lines)
