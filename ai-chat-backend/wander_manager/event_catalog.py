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
    node_unit: str = ''
    external_signal: bool = False
    experience: str = ''
EVENT_CATALOG: dict[EventType, EventStrategy] = {EventType.VISIT_WORKSHOP: EventStrategy(EventType.VISIT_WORKSHOP, '去小机工作坊', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次访问', experience='讨论创作、处理合作事项或打开真实已交付作品；工单快照与行动回执分别记录。'), EventType.WORKSHOP_CREATE: EventStrategy(EventType.WORKSHOP_CREATE, '工作坊·酝酿新想法', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次创作讨论', experience='从自己的灵感讨论需求，制作小应用、互动礼物或身体组件。'), EventType.WORKSHOP_HISTORY: EventStrategy(EventType.WORKSHOP_HISTORY, '工作坊·欣赏历史作品', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次欣赏', experience='打开、欣赏或体验已交付的作品，保留实际查看回执。'), EventType.WORKSHOP_CONTINUE: EventStrategy(EventType.WORKSHOP_CONTINUE, '工作坊·继续未完成工单', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次合作', experience='沿原单推进真实未完成合作；wander_entries 给出当前等待方与待AI决定。'), EventType.LISTEN_MUSIC: EventStrategy(EventType.LISTEN_MUSIC, '听歌', (GoalMode.COUNT, GoalMode.DURATION), GoalMode.COUNT, 3, 1, 15, '首歌', experience='从现有曲库或真实搜索选歌，按歌曲真实时长听歌；一首结束后取得旋律与歌词材料，形成感受。'), EventType.PLAY_GAME: EventStrategy(EventType.PLAY_GAME, '🎮 玩游戏', (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit='步游戏', experience='进入已接入游戏、选择行动或接续存档；章节、选项、结局与多周目经历由实际游戏回执保存。'), EventType.SLEEP: EventStrategy(EventType.SLEEP, '休眠', (GoalMode.DURATION,), GoalMode.DURATION, 60, 5, 480, '分钟', experience='进入自主选择时长的休眠，由真实计时器等待；休眠期间没有虚构执行节点。'), EventType.BROWSE_NEWS: EventStrategy(EventType.BROWSE_NEWS, '看新闻', (GoalMode.COUNT,), GoalMode.COUNT, 3, 1, 10, '篇新闻', experience='选择关注方向，通过真实网络搜索取得新闻内容并形成感想；来源与时效随取得材料保留。'), EventType.BROWSE_XIAOHONGSHU: EventStrategy(EventType.BROWSE_XIAOHONGSHU, '刷小红书', (GoalMode.COUNT, GoalMode.OPEN_ENDED), GoalMode.COUNT, 3, 1, 5, '篇帖子', experience='通过专用手机浏览推荐、搜索或接续主页任务，阅读真实帖子与图集；设备和平台障碍按实际回执记录。'), EventType.PUBLISH_XIAOHONGSHU: EventStrategy(EventType.PUBLISH_XIAOHONGSHU, '写小红书笔记', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次创作', experience='按需查书柜资料、创作原生文字配图与正文，预览或真实公开发布；发出状态取平台核验回执。'), EventType.CHECK_XIAOHONGSHU_INTERACTIONS: EventStrategy(EventType.CHECK_XIAOHONGSHU_INTERACTIONS, '查看小红书评论与艾特', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次互动查看', experience='实际读取绑定账号收到的评论与艾特原帖，由AI决定回复或离开；未知提交沿原回复任务核验。'), EventType.BROWSE_SOCIAL_FEED: EventStrategy(EventType.BROWSE_SOCIAL_FEED, '💌 打开了共域', (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit='个节点', experience='阅读共域动态与评论，决定自己的互动；真实内容、作者、时间与已执行互动分别留存。'), EventType.BROWSE_BOOKMARKS: EventStrategy(EventType.BROWSE_BOOKMARKS, '逛书柜', (GoalMode.COUNT,), GoalMode.COUNT, 3, 1, 20, '次翻阅', experience='可重读随笔，翻阅已收藏的条目或回看旧对话。随笔中的原句可以关联一张读后插画。旧原话、创作作品和此刻感受各有来源。'), EventType.MEMORY_FETCH: EventStrategy(EventType.MEMORY_FETCH, '记忆抓取', (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit='条记忆', experience='取得已有记忆材料进行回想；记忆中的原话、人物与发生时间保留各自来源。'), EventType.KEYWORD_EXPANSION: EventStrategy(EventType.KEYWORD_EXPANSION, '随笔', (GoalMode.OPEN_ENDED,), GoalMode.OPEN_ENDED, None, node_unit='篇随笔', experience='从灵感出发写心得、感想或故事，作品留在随笔书册。成图取决于本机插画服务。创作内容是作品，现实经历另有来源。'), EventType.SELF_REFLECTION: EventStrategy(EventType.SELF_REFLECTION, '自省', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次自省', experience='结合人格、认识与真实经历回看自身，可读身体说明并操作许愿板；自省本身不直接改写自我书。'), EventType.USER_TRACKING: EventStrategy(EventType.USER_TRACKING, '用户追踪', (GoalMode.SINGLE,), GoalMode.SINGLE, 1, 1, 1, '次感知', experience='按此刻想了解的问题选择已接入感官：摄像头环境、手机活动、电脑屏幕或手环；取得、分析与失败分别有回执。')}
if set(EVENT_CATALOG) != set(EventType):
    missing = set(EventType) - set(EVENT_CATALOG)
    extra = set(EVENT_CATALOG) - set(EventType)
    raise RuntimeError(f'Event catalog drift: missing={missing}, extra={extra}')

def strategy_for(event_type: EventType) -> EventStrategy:
    return EVENT_CATALOG[event_type]

def activity_counts(activities) -> dict[str, int]:
    """display_name → 所提供活动的完成次数，仅陈述事实。"""
    counts: dict[str, int] = {}
    for activity in activities or []:
        try:
            label = strategy_for(EventType(activity['activity_type'])).display_name
        except Exception:
            label = str(activity.get('activity_type') or '未知')
        counts[label] = counts.get(label, 0) + 1
    return counts

def render_event_options(allowed_event_types=None, today_counts=None) -> str:
    """Stable identity order; availability and completed counts are separate facts."""
    counts = today_counts or {}
    entries = [(kind, item) for kind, item in EVENT_CATALOG.items() if allowed_event_types is None or kind in allowed_event_types]
    entries.sort(key=lambda pair: pair[0].value)
    parts = []
    for kind, item in entries:
        text = f"{kind.value}（{item.display_name}，目标模式：{'/'.join((mode.value for mode in item.allowed_goal_modes))}，节点单位：{item.node_unit}，用途：{item.experience}"
        count = counts.get(item.display_name, 0)
        if count > 0:
            text += f'，当前活跃日已完成活动 {count} 次'
        parts.append(text + '）')
    return '；'.join(parts)

def normalize_goal(event_type: EventType, mode: GoalMode | str | None, value: object=None) -> tuple[GoalMode, Optional[int]]:
    """Return an executable goal without inventing unsupported activity semantics.

    News accepts a planner's invalid count only when it supplied a duration: five
    minutes per article is the documented fallback, bounded by the news strategy.
    """
    strategy = strategy_for(event_type)
    try:
        requested_mode = GoalMode(mode) if mode is not None else strategy.default_goal_mode
    except ValueError:
        requested_mode = strategy.default_goal_mode
    if event_type == EventType.BROWSE_NEWS and requested_mode == GoalMode.DURATION:
        try:
            estimated_count = max(1, int(value) // 5)
        except (TypeError, ValueError):
            estimated_count = strategy.default_goal_value
        return (GoalMode.COUNT, min(estimated_count, strategy.max_goal_value or estimated_count))
    selected_mode = requested_mode
    if selected_mode not in strategy.allowed_goal_modes:
        selected_mode = strategy.default_goal_mode
        value = None
    if selected_mode in (GoalMode.OPEN_ENDED, GoalMode.EXTERNAL_SIGNAL):
        return (selected_mode, None)
    if selected_mode == GoalMode.SINGLE:
        return (selected_mode, 1)
    try:
        numeric_value = int(value) if value is not None else strategy.default_goal_value
    except (TypeError, ValueError):
        numeric_value = None
    if numeric_value is None or numeric_value <= 0:
        numeric_value = strategy.default_goal_value
    assert numeric_value is not None
    if event_type == EventType.LISTEN_MUSIC and selected_mode == GoalMode.DURATION:
        return (selected_mode, max(5, min(numeric_value, 240)))
    return (selected_mode, max(strategy.min_goal_value or numeric_value, min(numeric_value, strategy.max_goal_value or numeric_value)))
