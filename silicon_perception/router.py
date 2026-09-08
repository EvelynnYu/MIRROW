"""哨兵 API 路由 — 已迁移至 silicon_perception.alerts
保留此文件作为向后兼容的 re-export。
"""

from .alerts.router import router

__all__ = ["router"]
