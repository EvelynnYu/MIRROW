"""证据链 + 状态切换事件。

每次状态切换记录"为什么切"（哪些信号、什么值），一举两得：
- debug/审计：切换可追溯
- AI 情感表达素材：human_hint 给 AI "注意到你"的话（"GPS 到公司了"）

StateTransition 由 engine 产出，bridge 落库 + 回写 user_status，
表达层（阶段4 ProactiveExpressionArbiter）订阅。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .model import StateVector, SignalEvidence

# reason_code → 给 AI 的自然语言素材（不是成品文案，AI 自己组织语气）
_HINT = {
    "gps_work": "定位到了公司/工作地",
    "gps_elsewhere": "定位显示在外面",
    "pc_active": "回到电脑前了",
    "phone_active": "在用手机",
    "idle_and_devices_off": "离开设备了",
    "editor_foreground_sustained": "在专注写代码",
    "game_foreground": "在打游戏",
}


def hint_for(reason_code: str) -> str:
    for k, v in _HINT.items():
        if reason_code.startswith(k):
            return v
    return ""


@dataclass
class StateTransition:
    """一次状态切换（证据链元素 + 表达素材）。"""
    from_legacy: str
    to_legacy: str
    to_state: StateVector
    at: str                       # ISO
    reason_code: str
    evidence: List[SignalEvidence] = field(default_factory=list)
    source: str = "auto"          # auto / manual / sleep_onset
    human_hint: str = ""

    @classmethod
    def build(cls, from_legacy: str, to_state: StateVector, at: str, source: str = "auto"):
        return cls(
            from_legacy=from_legacy,
            to_legacy=to_state.to_legacy(),
            to_state=to_state,
            at=at,
            reason_code=to_state.reason_code,
            evidence=list(to_state.evidence),
            source=source,
            human_hint=hint_for(to_state.reason_code),
        )
