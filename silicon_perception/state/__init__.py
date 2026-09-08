"""状态引擎 — 感知层的下游消费者。

把割裂的 behavior_state（设备级推断）回写 user_status，
接通 behavior_profile 基线做个性化阈值，用信号仲裁器落实 100% 铁律。

对外边界一律经 model.to_legacy() 投影回旧 9 枚举 user_status，存量代码零改动。
"""

from .model import Presence, Activity, RestMode, StateVector, SignalEvidence, from_legacy
from .evidence import StateTransition
from .engine import StateEngine, get_state_engine
from . import bridge

__all__ = [
    "Presence", "Activity", "RestMode", "StateVector", "SignalEvidence", "from_legacy",
    "StateTransition", "StateEngine", "get_state_engine", "bridge",
]
