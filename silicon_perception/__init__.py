# 硅基数据控制模块 (Silicon Sentinel)
#
# AI 的哨兵系统 — 持续监控用户健康/状态数据，
# 检测异常时主动推送关心消息。
#
# 与 Wander 漫想的区别：
# - Sentinel: 规则驱动、始终运行、健康/安全优先
# - Wander:   概率驱动、仅空闲时、娱乐/陪伴优先

from .sentinel import SiliconSentinel, get_sentinel, init_sentinel, SENTINEL_AVAILABLE
from .analysis.anomaly_detector import AnomalyDetector, AnomalyRule, DataSnapshot
from .recording.health_store import HealthStore

__all__ = [
    "SiliconSentinel",
    "get_sentinel",
    "init_sentinel",
    "SENTINEL_AVAILABLE",
    "AnomalyDetector",
    "AnomalyRule",
    "DataSnapshot",
    "HealthStore",
]
