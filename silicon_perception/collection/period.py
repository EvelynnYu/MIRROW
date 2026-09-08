"""生理期数据源"""

import logging

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class PeriodSource(DataSource):
    """生理期数据源 — 读取 calendar_manager"""

    name = "period"

    async def read(self) -> DataPoint:
        try:
            from calendar_manager.database import get_period_info_today
            info = get_period_info_today()
            return DataPoint(source=self.name, data={
                "is_period": info.get("is_period", False) if info else False,
                "period_day": info.get("period_day") if info else None,
            })
        except Exception as e:
            logger.debug(f"PeriodSource 读取失败: {e}")
            return DataPoint(source=self.name)

    @property
    def connection_state(self) -> str:
        return "connected"
