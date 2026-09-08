# 思维漫想管理器
#
# 架构层级：
# ┌─────────────────────────────────────────────────────────┐
# │                    模式开关层                            │
# │     管理漫想模式开启/关闭状态，检测用户回复时间阈值        │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    漫想创建层                            │
# │     按概率随机创建行为事件（关键词扩写/记忆抓取/看新闻等） │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    漫想日志层                            │
# │     记录每次事件类型、时间、过程                          │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    主动打扰判断层                        │
# │     LLM评分 + 公式计算 + 推送决策                        │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    消息生成器                            │
# │     生成主动消息内容                                      │
# └─────────────────────────────────────────────────────────┘

# 共享类型定义（避免循环导入）
from .event_types import EventType, WanderEvent

# 各层模块
from .mode_switch import ModeSwitch, WanderMode, get_mode_switch, init_mode_switch
from .wander_creator import WanderCreator, get_wander_creator, init_wander_creator
from .wander_log import WanderLog, WanderLogEntry, get_wander_log, init_wander_log
from .disturb_judgment import DisturbJudgment, JudgmentResult, get_disturb_judgment, init_disturb_judgment
from .event_handlers import (
    BaseEventHandler,
    SleepHandler,
    KeywordExpansionHandler,
    MemoryFetchHandler,
    BrowseNewsHandler,
    SelfReflectionHandler,
    BrowseBookmarksHandler,
    UserTrackingHandler,
    EventHandlerFactory
)
from .xiaohongshu_adb import AdbXiaohongshuSource, BatterySnapshot
from .message_generator import ProactiveMessageGenerator, get_message_generator, init_message_generator
from .user_tracking_service import (
    UserTrackingService,
    UserActivityType,
    TrackingResult,
    get_user_tracking_service,
    init_user_tracking_service
)
from .config import (
    WanderConfig,
    ConfigManager,
    get_config,
    get_config_manager,
    init_config
)
from .manager import WanderManager, get_wander_manager, init_wander_manager
from .user_status import (
    UserStatus,
    get_user_status,
    set_user_status,
    get_user_status_context,
    get_user_status_context_rich,
    get_user_status_custom_text,
    set_user_status_custom_text,
    get_status_presets,
    check_wakeup,
    StatusMeta,
    get_status_meta,
    set_status_meta,
)

__all__ = [
    # 共享类型
    "EventType",
    "WanderEvent",
    # 协调器
    "WanderManager",
    "get_wander_manager",
    "init_wander_manager",
    # 模式开关层
    "ModeSwitch",
    "WanderMode",
    "get_mode_switch",
    "init_mode_switch",
    # 漫想创建层
    "WanderCreator",
    "get_wander_creator",
    "init_wander_creator",
    # 漫想日志层
    "WanderLog",
    "WanderLogEntry",
    "get_wander_log",
    "init_wander_log",
    # 主动打扰判断层
    "DisturbJudgment",
    "JudgmentResult",
    "get_disturb_judgment",
    "init_disturb_judgment",
    # 事件处理器
    "BaseEventHandler",
    "SleepHandler",
    "KeywordExpansionHandler",
    "MemoryFetchHandler",
    "BrowseNewsHandler",
    "SelfReflectionHandler",
    "BrowseBookmarksHandler",
    "UserTrackingHandler",
    "EventHandlerFactory",
    # 小红书专用真机来源（只读 ADB）
    "AdbXiaohongshuSource",
    "BatterySnapshot",
    # 消息生成器
    "ProactiveMessageGenerator",
    "get_message_generator",
    "init_message_generator",
    # 用户追踪服务
    "UserTrackingService",
    "UserActivityType",
    "TrackingResult",
    "get_user_tracking_service",
    "init_user_tracking_service",
    # 配置管理
    "WanderConfig",
    "ConfigManager",
    "get_config",
    "get_config_manager",
    "init_config",
    # 用户状态
    "UserStatus",
    "get_user_status",
    "set_user_status",
    "get_user_status_context",
    "get_user_status_context_rich",
    "get_user_status_custom_text",
    "set_user_status_custom_text",
    "get_status_presets",
    "check_wakeup",
    "StatusMeta",
    "get_status_meta",
    "set_status_meta",
]
