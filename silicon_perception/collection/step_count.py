"""步数传感器数据源 — 通过手机 relay 定期轮询

每 5 分钟通过 mobile_relay 调用 mobile_steps_sensor，
获取累计步数值。停滞检测由 analysis/trigger_detector 的 _detect_steps 负责。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class StepCountSource(DataSource):
    """步数传感器 — 手机 relay 定期轮询"""

    name = "step_count"
    _base_tick_interval = 5  # 正常每 5 tick 一次

    def __init__(self):
        self._last_value: Optional[int] = None
        self._last_change_time: Optional[datetime] = None
        self._today_steps: int = 0
        self._consecutive_failures: int = 0
        self._last_error: Optional[str] = None

        # 从 daily_health_summary 恢复今日步数（重启后不归零）
        try:
            from silicon_perception.recording.health_store import get_store
            today = datetime.now().strftime("%Y-%m-%d")
            rows = get_store().get_daily_summaries(today, today)
            if rows and rows[0].get("steps"):
                self._today_steps = int(rows[0]["steps"])
                logger.info(f"StepCountSource: 从 DB 恢复今日步数 {self._today_steps}")
        except Exception:
            pass

    @property
    def tick_interval(self) -> int:
        """失败后加速重试：未恢复时每 tick 都采，成功后回到 5 tick。"""
        return 1 if self._consecutive_failures > 0 else self._base_tick_interval

    async def read(self, force: bool = False) -> DataPoint:
        data = await self._read_sensor(force=force)
        now = datetime.now()

        if data is None:
            self._consecutive_failures += 1
            return DataPoint(source=self.name, data={
                "cumulative_steps": None,
                "steps_today": None,  # 断连时不返回过期值，防止假告警
                "available": False,
            })

        cumulative = int(data["steps"])
        phone_today = data.get("today_steps")
        phone_valid = data.get("today_steps_valid", False)
        logical_date = data.get("logical_date")

        was_disconnected = self._consecutive_failures > 0
        self._consecutive_failures = 0
        self._last_error = None

        # 断连恢复：重置停滞时钟——断连期间是否走动无法知晓，不能算作停滞
        if was_disconnected:
            self._last_change_time = now
        # 追踪步数变化时间戳（用于停滞检测，键于累计值）
        if self._last_value is None or cumulative != self._last_value:
            self._last_change_time = now

        today_str = now.strftime("%Y-%m-%d")

        # 优先信任手机端算好的今日步数（手机有最好的重启+5点跨天处理）
        if phone_valid and isinstance(phone_today, (int, float)) and phone_today >= 0:
            self._today_steps = int(phone_today)
            # 对齐后端回退缓存，防日后 hand-off（手机→WebView兜底）打架
            try:
                from health_tracker.tracker import get_health_tracker
                get_health_tracker().sync_today_steps(cumulative, int(phone_today))
            except Exception:
                pass
        else:
            # 回退：手机没给权威值 → 后端减法式重算
            try:
                from health_tracker.tracker import get_health_tracker
                ht = get_health_tracker()
                if ht._step_callback:
                    self._today_steps = ht._track_steps(cumulative)
                else:
                    self._today_steps = max(self._today_steps, 0)
            except Exception:
                pass
        self._last_value = cumulative

        # 实时同步步数到 daily_health_summary（键于手机 logical_date，处理5点跨天边界）
        try:
            from silicon_perception.recording.health_store import get_store
            summary_date = logical_date or today_str
            get_store().upsert_daily_summary(summary_date, steps=self._today_steps)
        except Exception:
            pass

        return DataPoint(source=self.name, data={
            "cumulative_steps": cumulative,
            "steps_today": self._today_steps,
            "last_change_time": self._last_change_time.isoformat() if self._last_change_time else None,
            "available": True,
        }, captured_at=now.isoformat())

    async def _read_sensor(self, force: bool = False) -> Optional[dict]:
        """通过 mobile_relay 读取步数。返回整个 data dict（含 steps/today_steps/today_steps_valid/logical_date）。失败返回 None。"""
        try:
            from mirrow_core.shared_state import get_mobile_relay_callback
            relay = get_mobile_relay_callback()
            if not relay:
                self._last_error = "手机未连接"
                return None
            import uuid
            result = await asyncio.wait_for(
                relay(str(uuid.uuid4()), "mobile_steps_sensor", {"force": force}),
                timeout=15.0,
            )
            if isinstance(result, dict) and result.get("success"):
                data = result.get("data", {})
                if isinstance(data, str):
                    import json as _json
                    try:
                        data = _json.loads(data)
                    except Exception:
                        data = {}
                steps = data.get("steps")
                if isinstance(steps, (int, float)):
                    return data
            if isinstance(result, dict):
                self._last_error = result.get("content") or result.get("error") or "步数读取失败"
            return None
        except asyncio.TimeoutError:
            self._last_error = "步数超时(15s)"
            logger.debug("StepCountSource: 手机 relay 超时")
            return None
        except Exception as e:
            self._last_error = f"读取异常: {str(e)[:60]}"
            logger.debug(f"StepCountSource: 读取失败: {e}")
            return None

    @property
    def connection_state(self) -> str:
        if self._consecutive_failures >= 3:
            return "failed"
        if self._last_value is not None:
            return "connected"
        return "disconnected"

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def today_steps(self) -> int:
        return self._today_steps

    @property
    def last_change_seconds_ago(self) -> Optional[float]:
        if self._last_change_time is None:
            return None
        return (datetime.now() - self._last_change_time).total_seconds()
