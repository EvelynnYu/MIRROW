"""屏幕使用时长数据源 — 通过手机 relay 定期轮询

每 30 分钟通过 mobile_relay 调用 mobile_usage，
获取当日屏幕总时长 + Top App 分布。
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)

# 手机 App 包名 → 类别映射（与 behavior_state.py 共用逻辑，此处独立维护）
_PACKAGE_CATEGORY = {
    "com.tencent.mobileqq": "social",
    "com.tencent.mm": "social",
    "com.sina.weibo": "social",
    "com.ss.android.ugc.aweme": "entertainment",
    "com.tencent.qqlive": "entertainment",
    "com.bilibili.app.in": "entertainment",
    "com.tencent.tmgp": "game",
    "com.miHoYo": "game",
    "com.mirrow.app": "mirrow",
    "com.android.chrome": "browser",
    "com.microsoft.office": "work",
}


class ScreenTimeSource(DataSource):
    """屏幕使用时长 — 手机 relay 每 30 分钟轮询"""

    name = "screen_time"
    tick_interval = 10  # 每 10 个基础 tick 采集一次（10×60s=10min）

    def __init__(self):
        self._consecutive_failures: int = 0
        self._last_data_time: Optional[str] = None  # 首次成功采集时间

    async def read(self) -> DataPoint:
        try:
            from mirrow_core.shared_state import get_mobile_relay_callback
            relay = get_mobile_relay_callback()
            if not relay:
                self._consecutive_failures += 1
                return DataPoint(source=self.name, data={"available": False})

            import uuid
            result = await asyncio.wait_for(relay(str(uuid.uuid4()), "mobile_usage", {}), timeout=15.0)
            if not result or not result.get("success"):
                self._consecutive_failures += 1
                return DataPoint(source=self.name, data={
                    "available": False,
                    "error": result.get("error", "unknown") if result else "no_response",
                })

            data = result.get("data", {})
            total_min = data.get("total_minutes", 0)
            top_apps = data.get("top_apps", [])

            # 分类聚合
            cat_minutes = {}
            for app in (top_apps or []):
                pkg = app.get("package", "")
                minutes = app.get("minutes", app.get("duration_minutes", 0))
                cat = _classify_app(pkg)
                cat_minutes[cat] = cat_minutes.get(cat, 0) + minutes

            self._consecutive_failures = 0
            self._last_data_time = datetime.now().isoformat()
            return DataPoint(source=self.name, data={
                "available": True,
                "screen_time_minutes": total_min,
                "top_apps": top_apps[:10],
                "category_minutes": cat_minutes,
                "top_app_category": max(cat_minutes, key=cat_minutes.get) if cat_minutes else "other",
            }, captured_at=self._last_data_time)
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            return DataPoint(source=self.name, data={"available": False, "error": "timeout"})
        except Exception as e:
            self._consecutive_failures += 1
            logger.debug(f"ScreenTimeSource 读取失败: {e}")
            return DataPoint(source=self.name, data={"available": False, "error": str(e)})

    @property
    def connection_state(self) -> str:
        if self._consecutive_failures >= 3:
            return "failed"
        if self._last_data_time is not None:
            return "connected"
        return "disconnected"


def _classify_app(package: str) -> str:
    """包名 → 大类。"""
    for prefix, cat in _PACKAGE_CATEGORY.items():
        if package.startswith(prefix):
            return cat
    return "other"
