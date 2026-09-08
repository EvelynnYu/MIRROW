"""键鼠空闲数据源 — Windows GetLastInputInfo API

纯 ctypes 调用，零延迟，零幻觉。
返回距上次键盘/鼠标输入的秒数，用于判断用户是否在电脑前。
"""

import logging
from typing import Optional

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class InputIdleSource(DataSource):
    """键鼠空闲检测 — 通过 Windows GetLastInputInfo API"""

    name = "input_idle"

    async def read(self) -> DataPoint:
        idle_seconds = self._get_idle_seconds()
        return DataPoint(source=self.name, data={
            "input_idle_seconds": round(idle_seconds, 1) if idle_seconds is not None else None,
        })

    @staticmethod
    def _get_idle_seconds() -> Optional[float]:
        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("dwTime", wintypes.DWORD),
                ]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None

            tick_count = ctypes.windll.kernel32.GetTickCount()
            idle_ms = tick_count - lii.dwTime
            return idle_ms / 1000.0
        except Exception:
            return None

    @property
    def connection_state(self) -> str:
        """Windows API 总是可用的——键鼠空闲检测不依赖外部连接。"""
        idle = self._get_idle_seconds()
        return "connected" if idle is not None else "failed"
