from datetime import datetime
from typing import Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import uuid

class EventType(Enum):
    """漫想事件类型"""
    KEYWORD_EXPANSION = 'keyword_expansion'
    MEMORY_FETCH = 'memory_fetch'
    USER_TRACKING = 'user_tracking'
    SLEEP = 'sleep'
    BROWSE_NEWS = 'browse_news'
    BROWSE_XIAOHONGSHU = 'browse_xiaohongshu'
    PUBLISH_XIAOHONGSHU = 'publish_xiaohongshu'
    XHS_DIRECT_MESSAGES = 'xhs_direct_messages'
    CHECK_XIAOHONGSHU_INTERACTIONS = 'check_xiaohongshu_interactions'
    BROWSE_SOCIAL_FEED = 'browse_social_feed'
    VISIT_WORKSHOP = 'visit_workshop'
    WORKSHOP_CREATE = 'workshop_create'
    WORKSHOP_HISTORY = 'workshop_history'
    WORKSHOP_CONTINUE = 'workshop_continue'
    SELF_REFLECTION = 'self_reflection'
    BROWSE_BOOKMARKS = 'browse_bookmarks'
    LISTEN_MUSIC = 'listen_music'
    PLAY_GAME = 'play_game'

@dataclass
class WanderEvent:
    """漫想事件"""
    event_type: EventType
    timestamp: datetime = field(default_factory=datetime.now)
    description: str = ''
    details: Dict[str, Any] = field(default_factory=dict)
    process_log: str = ''
    event_id: str = ''

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex

    def to_dict(self) -> dict:
        """转换为字典"""
        return {'event_type': self.event_type.value, 'timestamp': self.timestamp.isoformat(), 'description': self.description, 'details': self.details, 'process_log': self.process_log, 'event_id': self.event_id}
