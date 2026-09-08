# 漫想事件类型定义
#
# 共享的类型定义，避免循环导入

from datetime import datetime
from typing import Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import uuid


class EventType(Enum):
    """漫想事件类型"""
    KEYWORD_EXPANSION = "keyword_expansion"      # 关键词生成+主题扩写
    MEMORY_FETCH = "memory_fetch"                # 记忆抓取
    USER_TRACKING = "user_tracking"              # 用户追踪
    SLEEP = "sleep"                              # 休眠
    BROWSE_NEWS = "browse_news"                  # 看新闻
    BROWSE_XIAOHONGSHU = "browse_xiaohongshu"    # 刷小红书（专用真机只读）
    BROWSE_SOCIAL_FEED = "browse_social_feed"    # 逛 AI 与用户的私有朋友圈
    BROWSE_TAOBAO = "browse_taobao"              # 真实商品搜索、本地收藏与逛街小记
    VISIT_LOUNGE = "visit_lounge"                # 允许自主串门的好友，真实会客与回家小记
    SELF_REFLECTION = "self_reflection"          # 自省（大脑架构+愿望清单）
    BROWSE_BOOKMARKS = "browse_bookmarks"        # 看收藏夹
    HOST_GROUP_ACTIVITY = "host_group_activity"  # 宿主显式注册的群组活动接口
    LISTEN_MUSIC = "listen_music"                # AI 自己听歌（旋律分析+感想）

    @classmethod
    def _missing_(cls, value):
        """Allow a host to map one legacy persisted event value during upgrade.

        The mapping is deliberately supplied outside the published source.  A
        deployment with no mapping treats an unknown historical value as data
        that needs an explicit migration rather than silently reinterpreting it.
        """
        import os
        legacy = os.getenv("MIRROW_LEGACY_HOST_GROUP_EVENT_TYPE", "").strip()
        if legacy and str(value) == legacy:
            return cls.HOST_GROUP_ACTIVITY
        return None


@dataclass
class WanderEvent:
    """漫想事件"""
    event_type: EventType
    timestamp: datetime = field(default_factory=datetime.now)
    description: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    process_log: str = ""  # 事件过程日志
    event_id: str = ""  # 唯一事件ID，用于去重

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "description": self.description,
            "details": self.details,
            "process_log": self.process_log,
            "event_id": self.event_id
        }
