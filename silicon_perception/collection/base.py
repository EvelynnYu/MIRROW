"""数据源抽象基类 — DataSource ABC + DataPoint 数据模型"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def _parse_hr(data: bytes) -> int:
    """解析标准 BLE Heart Rate Measurement 数据包。
    Flags byte bit 0: 0=8-bit HR, 1=16-bit HR (little-endian)."""
    flags = data[0]
    if flags & 0x01:
        return int.from_bytes(data[1:3], "little")
    return data[1]


@dataclass
class DataPoint:
    """统一数据点 — 所有数据源产出的标准格式"""
    source: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    data: Dict[str, Any] = field(default_factory=dict)
    # 数据真实有效时间（采集成功时刻，非构造时刻）。None = 本次无新数据（显式不可用标记）。
    # 与 timestamp 区别：timestamp 是"读取尝试"时刻，captured_at 是"数据有效"时刻。
    captured_at: Optional[str] = None


class DataSource(ABC):
    """数据源抽象基类 — 所有数据源继承此类"""
    name: str = "base"

    @abstractmethod
    async def read(self) -> DataPoint:
        """读取一次数据，失败时返回空 DataPoint（不抛异常）"""
        ...

    async def start(self):
        """启动数据源（如建立持久连接），默认无操作"""
        pass

    async def stop(self):
        """停止数据源（如断开连接），默认无操作"""
        pass
