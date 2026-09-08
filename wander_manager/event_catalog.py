"""Explicit execution semantics for every currently executable Wander event."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .event_types import EventType
from .runtime_models import GoalMode


@dataclass(frozen=True)
class EventStrategy:
    event_type: EventType
    display_name: str
    allowed_goal_modes: tuple[GoalMode, ...]
    default_goal_mode: GoalMode
    default_goal_value: Optional[int]
    min_goal_value: Optional[int] = None
    max_goal_value: Optional[int] = None
    node_unit: str = ""
    external_signal: bool = False


EVENT_CATALOG: dict[EventType, EventStrategy] = {
    EventType.VISIT_LOUNGE: EventStrategy(EventType.VISIT_LOUNGE, "好友串门", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次拜访"),
    EventType.BROWSE_TAOBAO: EventStrategy(EventType.BROWSE_TAOBAO, "逛淘宝", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次闲逛"),
    EventType.LISTEN_MUSIC: EventStrategy(EventType.LISTEN_MUSIC, "听歌", (GoalMode.COUNT, GoalMode.DURATION), GoalMode.COUNT, 3, 1, 15, "首歌"),
    EventType.SLEEP: EventStrategy(EventType.SLEEP, "休眠", (GoalMode.DURATION,), GoalMode.DURATION, 60, 5, 480, "分钟"),
    EventType.BROWSE_NEWS: EventStrategy(EventType.BROWSE_NEWS, "看新闻", (GoalMode.COUNT,), GoalMode.COUNT, 3, 1, 10, "篇新闻"),
    EventType.BROWSE_XIAOHONGSHU: EventStrategy(EventType.BROWSE_XIAOHONGSHU, "刷小红书", (GoalMode.COUNT,), GoalMode.COUNT, 3, 1, 5, "篇帖子"),
    # The outer event/tool mount is a single visual affordance.  The bounded
    # visit decision remains in the feed evidence, not in this display label.
    EventType.BROWSE_SOCIAL_FEED: EventStrategy(EventType.BROWSE_SOCIAL_FEED, "💌 打开了朋友圈", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次浏览"),
    EventType.BROWSE_BOOKMARKS: EventStrategy(EventType.BROWSE_BOOKMARKS, "翻收藏", (GoalMode.COUNT,), GoalMode.COUNT, 3, 1, 20, "条收藏"),
    EventType.MEMORY_FETCH: EventStrategy(EventType.MEMORY_FETCH, "记忆抓取", (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit="条记忆"),
    EventType.KEYWORD_EXPANSION: EventStrategy(EventType.KEYWORD_EXPANSION, "关键词扩写", (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit="关键词链"),
    EventType.SELF_REFLECTION: EventStrategy(EventType.SELF_REFLECTION, "自省", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次自省"),
    EventType.USER_TRACKING: EventStrategy(EventType.USER_TRACKING, "用户追踪", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次感知"),
    # A host may register this optional single-node activity through host_hooks.
    # The open-source distribution supplies no peer, room, transport, or callback.
    EventType.HOST_GROUP_ACTIVITY: EventStrategy(EventType.HOST_GROUP_ACTIVITY, "群组活动", (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, "次活动"),
}

if set(EVENT_CATALOG) != set(EventType):  # import-time programmer error, never a DB side effect
    missing = set(EventType) - set(EVENT_CATALOG)
    extra = set(EVENT_CATALOG) - set(EventType)
    raise RuntimeError(f"Event catalog drift: missing={missing}, extra={extra}")


def strategy_for(event_type: EventType) -> EventStrategy:
    return EVENT_CATALOG[event_type]


def normalize_goal(event_type: EventType, mode: GoalMode | str | None, value: object = None) -> tuple[GoalMode, Optional[int]]:
    """Return an executable goal without inventing unsupported activity semantics.

    News accepts a planner's invalid count only when it supplied a duration: five
    minutes per article is the documented fallback, bounded by the news strategy.
    """
    strategy = strategy_for(event_type)
    try:
        requested_mode = GoalMode(mode) if mode is not None else strategy.default_goal_mode
    except ValueError:
        requested_mode = strategy.default_goal_mode

    # News is count-driven, but a duration returned by an LLM has a documented
    # five-minutes-per-article conversion rather than being silently ignored.
    if event_type == EventType.BROWSE_NEWS and requested_mode == GoalMode.DURATION:
        try:
            estimated_count = max(1, int(value) // 5)
        except (TypeError, ValueError):
            estimated_count = strategy.default_goal_value
        return GoalMode.COUNT, min(estimated_count, strategy.max_goal_value or estimated_count)

    selected_mode = requested_mode
    if selected_mode not in strategy.allowed_goal_modes:
        selected_mode = strategy.default_goal_mode
        value = None

    if selected_mode in (GoalMode.OPEN_ENDED, GoalMode.EXTERNAL_SIGNAL):
        return selected_mode, None
    if selected_mode == GoalMode.SINGLE:
        return selected_mode, 1

    try:
        numeric_value = int(value) if value is not None else strategy.default_goal_value
    except (TypeError, ValueError):
        numeric_value = None

    if numeric_value is None or numeric_value <= 0:
        numeric_value = strategy.default_goal_value
    assert numeric_value is not None
    if event_type == EventType.LISTEN_MUSIC and selected_mode == GoalMode.DURATION:
        return selected_mode, max(5, min(numeric_value, 240))
    return selected_mode, max(strategy.min_goal_value or numeric_value, min(numeric_value, strategy.max_goal_value or numeric_value))
