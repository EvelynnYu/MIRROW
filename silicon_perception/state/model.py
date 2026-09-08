"""二维状态模型 + 向下兼容投影。

核心洞察：旧 9 枚举混了两个正交维度——
- presence（在场性）：有物理铁证 → 可 100% 自动
- activity（活动）：无铁证 → 只能声明/验证
- rest_mode（休息模式）：sleeping/napping 是"聊到这里/手动"声明的语义状态，
  覆盖 presence 物理推断（防打扰期间即使检测到手机活动也不切出）。

对外一律经 to_legacy() 投影回旧 UserStatus 值，存量代码零改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Presence(str, Enum):
    """在场性（物理铁证，100% 自动）"""
    AT_COMPUTER = "at_computer"   # 在电脑前（键鼠活跃）
    ON_PHONE = "on_phone"         # 在手机上（手机亮屏+前台App 且 PC 空闲）
    AWAY = "away"                 # 离开设备（键鼠长空闲+屏幕不活跃+手机灭屏）
    OUT = "out"                   # 外出（GPS=work/elsewhere）
    UNKNOWN = "unknown"           # 无法确定（信号冲突/陈旧/不足）


class Activity(str, Enum):
    """活动（声明 + 验证；仅少数可自动）"""
    NONE = "none"                 # 无特定活动
    CODING = "coding"             # 写代码（PC 前台是编码工具，进程铁证）
    GAMING = "gaming"             # 游戏（游戏清单命中，或声明）
    EATING = "eating"             # 吃饭（声明/NFC）
    BATHING = "bathing"           # 洗澡（声明/NFC）
    OTHER = "other"               # 其他行程（声明）


class RestMode(str, Enum):
    """休息模式（声明触发，覆盖 presence 物理推断）"""
    NONE = "none"
    SLEEPING = "sleeping"         # "聊到这里"触发，准备入睡+夜间防打扰
    NAPPING = "napping"           # 手动切入，小憩


@dataclass
class SignalEvidence:
    """单条信号证据（证据链元素）"""
    source: str                   # 信号来源，如 "gps" / "foreground_exe" / "input_idle"
    value: object                 # 信号值
    tier: int                     # 信号层级 1=物理铁证 2=设备活动 3=用户声明
    fresh_at: Optional[str] = None  # 数据真实有效时刻（ISO），None=无时效


@dataclass
class StateVector:
    """二维状态向量 — 引擎内部表示。对外经 to_legacy() 投影。"""
    presence: Presence = Presence.UNKNOWN
    activity: Activity = Activity.NONE
    rest_mode: RestMode = RestMode.NONE
    confidence: float = 0.0       # 100% 铁律：只有 1.0（确定）或 0.0（unknown）
    evidence: List[SignalEvidence] = field(default_factory=list)
    reason_code: str = ""         # 如 "gps_arrived_work" / "editor_foreground_sustained"

    def to_legacy(self) -> str:
        """投影回旧 UserStatus 值（activity/rest 优先，无则从 presence 派生）。

        存量代码（conversation_messages/status_change_log/前端渲染/context_scheduler 等）
        全部读旧 status 字段，本投影保证它们零改动。
        """
        # 1 休息模式最高优先（覆盖一切物理推断）
        if self.rest_mode == RestMode.SLEEPING:
            return "sleeping"
        if self.rest_mode == RestMode.NAPPING:
            return "napping"
        # 2 activity 次之
        if self.activity == Activity.CODING:
            return "coding"
        if self.activity == Activity.GAMING:
            return "gaming"
        if self.activity == Activity.EATING:
            return "eating"
        if self.activity == Activity.BATHING:
            return "bathing"
        if self.activity == Activity.OTHER:
            return "other"
        # 3 从 presence 派生
        if self.presence == Presence.OUT:
            return "out"
        if self.presence in (Presence.AT_COMPUTER, Presence.ON_PHONE, Presence.AWAY):
            return "idle"
        # unknown → idle（漫想另读二维原始值做概率打折，见 engine/漫想集成）
        return "idle"

    def is_definite(self) -> bool:
        """是否确定状态（100% 铁律：confidence==1.0）"""
        return self.confidence >= 1.0

    def key(self) -> tuple:
        """用于防抖比较的状态键（presence+activity+rest_mode，不含证据/置信）"""
        return (self.presence, self.activity, self.rest_mode)


# 旧 UserStatus 值 → 二维（读现有声明状态时用，供仲裁器把 T3 声明纳入）
_LEGACY_TO_REST = {
    "sleeping": RestMode.SLEEPING,
    "napping": RestMode.NAPPING,
}
_LEGACY_TO_ACTIVITY = {
    "coding": Activity.CODING,
    "gaming": Activity.GAMING,
    "eating": Activity.EATING,
    "bathing": Activity.BATHING,
    "other": Activity.OTHER,
}
_LEGACY_TO_PRESENCE = {
    "out": Presence.OUT,
    "idle": Presence.UNKNOWN,  # idle 声明不含物理信息，视为未知在场
}


def from_legacy(status: str) -> StateVector:
    """把旧 UserStatus 值解析成二维向量（用于把用户当前声明作为 T3 信号纳入仲裁）。

    注意：这是"声明"来源，confidence 保持 0.0，由仲裁器决定是否采信。
    """
    sv = StateVector()
    if status in _LEGACY_TO_REST:
        sv.rest_mode = _LEGACY_TO_REST[status]
    elif status in _LEGACY_TO_ACTIVITY:
        sv.activity = _LEGACY_TO_ACTIVITY[status]
    elif status in _LEGACY_TO_PRESENCE:
        sv.presence = _LEGACY_TO_PRESENCE[status]
    return sv
