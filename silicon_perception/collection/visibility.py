"""MIRROW 页面可见性数据源"""

import logging

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class MirrowVisibilitySource(DataSource):
    """MIRROW 页面可见性 — 读取 shared_state 全局变量"""

    name = "mirrow_visibility"

    async def read(self) -> DataPoint:
        try:
            from mirrow_core.shared_state import get_mirrow_page_visible, get_mirrow_page_last_seen
            import time
            visible = get_mirrow_page_visible()
            last_seen = get_mirrow_page_last_seen()
            ago = time.monotonic() - last_seen if last_seen > 0 else 999999
            return DataPoint(source=self.name, data={
                "mirrow_visible": visible and ago < 10,
                "last_seen_seconds": round(ago, 1),
            })
        except Exception as e:
            logger.debug(f"MirrowVisibilitySource 读取失败: {e}")
            return DataPoint(source=self.name)

    @property
    def connection_state(self) -> str:
        return "connected"
