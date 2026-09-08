"""当前前台App数据源 — 通过手机 relay 快速轮询

每 2 分钟通过 mobile_relay 调用 mobile_current_app，
获取手机当前/最近前台App包名。
"""
import asyncio
import logging
from datetime import datetime
from .base import DataSource, DataPoint
from .app_name_map import resolve_app_name

logger = logging.getLogger(__name__)


class CurrentAppSource(DataSource):
    """手机当前前台App — 2min 轮询"""

    name = "current_app"
    tick_interval = 2  # 每 2 个基础 tick 采集一次（2min）

    def __init__(self):
        self._last_package: str = ""
        self._consecutive_failures: int = 0
        self._last_success_time = None

    async def read(self) -> DataPoint:
        try:
            from mirrow_core.shared_state import get_mobile_relay_callback
            relay = get_mobile_relay_callback()
            if not relay:
                self._consecutive_failures += 1
                return DataPoint(source=self.name, data={"available": False})

            import uuid
            result = await asyncio.wait_for(
                relay(str(uuid.uuid4()), "mobile_current_app", {}),
                timeout=8.0,
            )
            if not result or not result.get("success"):
                self._consecutive_failures += 1
                return DataPoint(source=self.name, data={
                    "available": False,
                    "package": self._last_package or None,
                })

            data = result.get("data", {})
            pkg = data.get("package", "")
            screen_on = data.get("screen_on", False)
            app_name = resolve_app_name(pkg) or data.get("app_name") or pkg
            if pkg:
                self._last_package = pkg
            self._consecutive_failures = 0
            self._last_success_time = datetime.now().isoformat()
            return DataPoint(source=self.name, data={
                "available": True,
                "package": pkg,
                "screen_on": screen_on,
                "app_name": app_name,
            }, captured_at=self._last_success_time)
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            return DataPoint(source=self.name, data={
                "available": False, "package": self._last_package or None})
        except Exception as e:
            self._consecutive_failures += 1
            logger.debug(f"CurrentAppSource 读取失败: {e}")
            return DataPoint(source=self.name, data={
                "available": False, "package": self._last_package or None})

    @property
    def connection_state(self) -> str:
        if self._consecutive_failures >= 3:
            return "failed"
        if self._last_package:
            return "connected"
        return "disconnected"
