"""异常检测规则引擎 — 已迁移至 silicon_perception.analysis
保留此文件作为向后兼容的 re-export。
"""

from .analysis.anomaly_detector import AnomalyDetector, AnomalyRule, DataSnapshot, Priority

__all__ = ["AnomalyDetector", "AnomalyRule", "DataSnapshot", "Priority"]
