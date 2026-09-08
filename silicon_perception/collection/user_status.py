"""用户状态数据源 — 事件驱动，从 shared_state 读取（非轮询 wander_manager）"""

import logging

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


class UserStatusSource(DataSource):
    """用户声明状态 — 由 main.py:set_user_status_combined 事件驱动更新。"""

    name = "user_status"

    async def read(self) -> DataPoint:
        try:
            from mirrow_core.shared_state import get_current_user_status
            status = get_current_user_status()
            if status:
                return DataPoint(source=self.name, data={"user_status": status})
            # 首次未设置时，回退到 wander_manager（只回退一次）
            from wander_manager.user_status import get_user_status
            s = get_user_status()
            val = s.value if hasattr(s, 'value') else str(s)
            # 同步到 shared_state 供后续读取
            from mirrow_core.shared_state import set_current_user_status
            set_current_user_status(val)
            return DataPoint(source=self.name, data={"user_status": val})
        except Exception as e:
            logger.debug(f"UserStatusSource 读取失败: {e}")
            return DataPoint(source=self.name)

    @property
    def connection_state(self) -> str:
        return "connected"
