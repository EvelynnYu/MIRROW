"""健康数据存储 — 已迁移至 silicon_perception.recording
保留此文件作为向后兼容的 re-export。
"""

from .recording.health_store import HealthStore, get_store

__all__ = ["HealthStore", "get_store"]
