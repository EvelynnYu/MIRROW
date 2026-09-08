"""屏幕活跃度数据源 — 截屏方差分析（轻量版，不调 LLM）"""

import asyncio
import logging
from typing import Dict, Any

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class ScreenActivitySource(DataSource):
    """屏幕活跃度数据源 — 截屏方差分析"""

    name = "screen_activity"

    def __init__(self):
        self._mss_available = False
        try:
            import mss
            import numpy as np
            self._mss = mss
            self._np = np
            self._mss_available = True
        except ImportError:
            logger.warning("ScreenActivitySource: mss/numpy 不可用")

    async def read(self) -> DataPoint:
        if not self._mss_available:
            return DataPoint(source=self.name)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._capture)
            return DataPoint(source=self.name, data=result)
        except Exception as e:
            logger.debug(f"ScreenActivitySource 读取失败: {e}")
            return DataPoint(source=self.name)

    def _capture(self) -> Dict[str, Any]:
        with self._mss.mss() as sct:
            monitor = sct.monitors[0]
            img = sct.grab(monitor)
            arr = self._np.array(img, dtype=self._np.uint8)
            gray = self._np.mean(arr[:, :, :3], axis=2).astype(self._np.float32)
            variance = float(self._np.var(gray))

        foreground_app = None
        foreground_exe = None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwnd = user32.GetForegroundWindow()

            # 窗口标题
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                foreground_app = buf.value

            # 进程名（替代关键词匹配，零额外依赖）
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                PROCESS_QUERY_LIMITED_INFO = 0x1000
                h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFO, False, pid.value)
                if h_process:
                    exe_buf = ctypes.create_unicode_buffer(260)
                    exe_size = wintypes.DWORD(260)
                    # QueryFullProcessImageNameW → 获取完整路径，取文件名
                    if kernel32.QueryFullProcessImageNameW(h_process, 0, exe_buf, ctypes.byref(exe_size)):
                        import os
                        foreground_exe = os.path.basename(exe_buf.value)
                    kernel32.CloseHandle(h_process)
        except Exception:
            pass

        return {
            "screen_active": variance > 80.0,
            "variance": round(variance, 1),
            "foreground_app": foreground_app,
            "foreground_exe": foreground_exe,
        }

    @property
    def connection_state(self) -> str:
        return "connected" if self._mss_available else "unavailable"
