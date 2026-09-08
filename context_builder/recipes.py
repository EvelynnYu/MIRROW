"""
上下文 Recipe 定义 — 所有场景的上下文配方的 SINGLE SOURCE OF TRUTH。

每个 Recipe 声明自己的 Section 列表 + 参数覆盖。
消费者按名取 Recipe，改配方只需改此文件。
"""

from dataclasses import dataclass, field
from typing import List, Callable, Optional


@dataclass
class RecipeSection:
    """Recipe 段定义"""
    name: str                                    # 段名（日志/调试用）
    ingredient: str                              # ingredients.py 中的函数名
    params: dict = field(default_factory=dict)   # 覆盖默认参数
    condition: Optional[str] = None              # 条件注入的 context flag 名（None=始终注入）
    group: str = ""                              # 分组标签：A身份/B情境/C知识/D历史/E系统/对话注入
    guide: Optional[str] = None                  # 组间引导词覆盖：None=用 guides.GUIDES 注册表默认；""=禁用；非空=覆盖（支持 {date} 占位）


@dataclass
class Recipe:
    name: str
    description: str
    sections: List[RecipeSection]
    consumer: str = ""                           # 消费者标签：AI私聊/AI漫想/AI哨兵/AI提醒/... 辅助监控台展示


# ═══════════════════════════════════════════════════════════
# 共享基础段
# ═══════════════════════════════════════════════════════════

# ── 推送共享基础层（哨兵/提醒/漫想共用）──
# 注：mood 已改为独立动态段（不再由 persona 内嵌，避免打断缓存前缀）。
# 注：无"离开信息"段——推送场景用户人不在，"用户刚回来"语义不符（离开时长在漫想 user 层 prompt 中）。
# recent_messages n=0 = 今天全量（AI 推送时必须能看到用户最后的消息，防止被自己的漫想挤出窗口）。
_PUSH_BASE = [
    # 稳定前缀（缓存友好，逐轮不变）
    RecipeSection("身份", "persona", group="A身份"),
    RecipeSection("人设锚点", "persona_anchor", group="A身份"),
    RecipeSection("行为调整", "active_evolutions", group="A身份"),
    RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
    RecipeSection("标记说明", "message_markers", group="E系统"),
    # D历史组：推送场景也需昨日背景（AI 每次推送要延续昨天的事，带引导词），一天内稳定缓存友好
    RecipeSection("前天日记", "day_before_diary", group="D历史"),
    RecipeSection("昨日对话", "yesterday", group="D历史"),
    RecipeSection("间隙日记", "gap_diaries", group="D历史"),
    # 动态块：自我书按本次触发事实检索，因此位于稳定的跨日历史之后
    RecipeSection("自我书", "self_book", group="A身份"),
    RecipeSection("情绪", "mood", group="B情境"),
    RecipeSection("开放线索", "open_loops", group="B情境"),
    RecipeSection("时间", "time", group="B情境"),
    RecipeSection("今日时间线", "today_timeline", group="B情境"),
    RecipeSection("天气", "weather", group="B情境"),
    RecipeSection("音乐耳蜗", "music_cochlea", condition="music_cochlea", group="B情境"),
    RecipeSection("语音耳蜗", "voice_cochlea", group="B情境"),
    RecipeSection("群聊当前", "group_chat_current", group="对话注入"),
    # 主动消息没有新的用户发言需要回复；同日对话作为历史材料，而不是 API 活跃轮次。
    RecipeSection("今日对话记录", "push_conversation_history", group="对话注入"),
    # 当前状态是理解主动消息场景的近端事实：统一沉到历史之后、本次事件之前。
    # 其中已含状态持续时长、私聊静默、群聊活跃度和感知新鲜度，避免分散重复。
    RecipeSection("当前状态总览", "push_current_state", group="对话注入"),
]


# ═══════════════════════════════════════════════════════════
# Recipe 定义
# ═══════════════════════════════════════════════════════════

FULL_CHAT = Recipe(
    "主聊天（完整）",
    "正常聊天的完整上下文，按 A-E 层组织",
    consumer="AI私聊",
    sections=[
        # ── 稳定前缀（缓存友好，逐轮不变）──
        RecipeSection("身份", "persona", group="A身份"),
        RecipeSection("人设锚点", "persona_anchor", group="A身份"),
        RecipeSection("行为调整", "active_evolutions", group="A身份"),
        RecipeSection("自我书", "self_book", group="A身份"),
        RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
        RecipeSection("标记说明", "message_markers", group="E系统"),

        # ── 动态块（人格叙事优先：我是谁→过往→昨天→此刻）──
        RecipeSection("情绪", "mood", group="B情境"),
        RecipeSection("开放线索", "open_loops", group="B情境"),
        RecipeSection("用户资料", "user_profile", group="B情境"),
        RecipeSection("记忆检索", "memories", params={"top_k": 10}, group="C知识"),
        RecipeSection("待办浮现", "todo_snippets", group="C知识"),
        RecipeSection("世界书", "world_book", group="C知识"),
        RecipeSection("SCP 百科", "scp", group="C知识"),
        RecipeSection("前天日记", "day_before_diary", condition="inject_yesterday", group="D历史"),
        RecipeSection("昨日对话", "yesterday", condition="inject_yesterday", group="D历史"),
        RecipeSection("间隙日记", "gap_diaries", condition="inject_yesterday", group="D历史"),
        RecipeSection("引用日记", "referenced_diary", condition="date_referenced", group="D历史"),
        RecipeSection("重要事件", "important_events", condition="inject_yesterday", group="D历史"),
        RecipeSection("时间", "time", group="B情境"),
        RecipeSection("活动摘要", "activity_summary", group="B情境"),
        RecipeSection("离开信息", "away", condition="away", group="B情境"),
        RecipeSection("哨兵快照", "sentinel_snapshot", group="B情境"),
        RecipeSection("天气", "weather", group="B情境"),
        RecipeSection("深夜模式", "night_mode", condition="night_mode", group="B情境"),
        RecipeSection("刷手机模式", "phone_browse", condition="phone_browse", group="B情境"),
        RecipeSection("音乐耳蜗", "music_cochlea", condition="music_cochlea", group="B情境"),
        RecipeSection("语音耳蜗", "voice_cochlea", group="B情境"),
        # 未进入游玩窗口时，仅在当前话题明确涉及裂隙档案时由调用方提供。
        RecipeSection("裂隙档案连续性", "rift_game_context", condition="rift_game_context", group="B情境"),

        # ── 对话注入层 ──
        RecipeSection("对话历史", "conversation_messages", group="对话注入"),
        RecipeSection("群聊当前", "group_chat_current", group="对话注入"),
        RecipeSection("群组动态", "host_group_current", group="对话注入"),
        RecipeSection("时间感知", "time_awareness", condition="time_hint", group="对话注入"),
        RecipeSection("群聊摘要", "group_chat_summary", group="对话注入"),
        RecipeSection("状态变更履历", "status_changes", group="对话注入"),
        RecipeSection("哨兵注解", "sentinel_timeline", group="对话注入"),
    ],
)


RIFT_PLAY = Recipe(
    "裂隙档案共同游玩",
    "AI 保留完整人格骨架、聚焦当前虚构案件的主聊天上下文",
    consumer="AI私聊·裂隙档案",
    sections=[
        # 人格骨架不做简化，游戏不是另一个角色人格。
        RecipeSection("身份", "persona", group="A身份"),
        RecipeSection("人设锚点", "persona_anchor", group="A身份"),
        RecipeSection("行为调整", "active_evolutions", group="A身份"),
        RecipeSection("自我书", "self_book", group="A身份"),
        RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
        RecipeSection("标记说明", "message_markers", group="E系统"),

        # 只保留关系与此刻需要的材料，避免无关知识挤占案件推理空间。
        RecipeSection("情绪", "mood", group="B情境"),
        RecipeSection("用户资料", "user_profile", group="B情境"),
        RecipeSection("相关记忆", "memories", params={"top_k": 3}, group="C知识"),
        RecipeSection("时间", "time", group="B情境"),
        RecipeSection("裂隙档案运行态", "rift_game_context", group="B情境"),

        RecipeSection("对话历史", "conversation_messages", group="对话注入"),
        RecipeSection("时间感知", "time_awareness", condition="time_hint", group="对话注入"),
    ],
)


LIGHTWEIGHT = Recipe(
    "漫想轻量",
    "漫想模式精简上下文（旧管道回退用，未接线 Builder）",
    consumer="AI漫想(旧)",
    sections=[
        RecipeSection("身份", "persona", params={"simplified": True}),
        RecipeSection("时间", "time"),
        RecipeSection("时间锚点", "timeline_anchor"),
        RecipeSection("用户状态", "user_status", condition="user_status_not_idle"),
        RecipeSection("生理期", "period", condition="period_active"),
        RecipeSection("离开信息", "away", condition="away"),
        RecipeSection("漫想说明", "wander_mode"),
        RecipeSection("世界书摘要", "world_book_summaries"),
        RecipeSection("最近对话", "recent_messages", params={"n": 10}),
        RecipeSection("最近漫想", "recent_wander", params={"n": 5}),
    ],
)


GROUP_CHAT_K = Recipe(
    "群聊 AI",
    "群聊中 AI 的上下文（接入 Builder 管道，FULL_CHAT 结构 + 群聊特有段）",
    consumer="AI群聊",
    sections=[
        # ── 稳定前缀（缓存友好，逐轮不变）──
        RecipeSection("身份", "persona", group="A身份"),
        RecipeSection("人设锚点", "persona_anchor", group="A身份"),
        RecipeSection("行为调整", "active_evolutions", group="A身份"),
        RecipeSection("自我书", "self_book", group="A身份"),
        RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
        RecipeSection("标记说明", "message_markers", group="E系统"),
        RecipeSection("工具禁用", "group_tool_ban", group="E系统"),

        # ── 动态块（人格叙事优先）──
        RecipeSection("情绪", "mood", group="B情境"),
        RecipeSection("开放线索", "open_loops", group="B情境"),
        RecipeSection("用户资料", "user_profile", group="B情境"),
        RecipeSection("记忆检索", "memories", params={"top_k": 5}, group="C知识"),
        RecipeSection("待办浮现", "todo_snippets", group="C知识"),
        RecipeSection("世界书", "world_book", group="C知识"),
        RecipeSection("SCP 百科", "scp", group="C知识"),
        RecipeSection("前天日记", "day_before_diary", condition="inject_yesterday", group="D历史"),
        RecipeSection("昨日对话", "yesterday", condition="inject_yesterday", group="D历史"),
        RecipeSection("间隙日记", "gap_diaries", condition="inject_yesterday", group="D历史"),
        RecipeSection("引用日记", "referenced_diary", condition="date_referenced", group="D历史"),
        RecipeSection("重要事件", "important_events", condition="inject_yesterday", group="D历史"),
        RecipeSection("时间", "time", group="B情境"),
        RecipeSection("活动摘要", "activity_summary", group="B情境"),
        RecipeSection("离开信息", "away", condition="away", group="B情境"),
        RecipeSection("哨兵快照", "sentinel_snapshot", group="B情境"),
        RecipeSection("天气", "weather", group="B情境"),
        RecipeSection("深夜模式", "night_mode", condition="night_mode", group="B情境"),
        RecipeSection("刷手机模式", "phone_browse", condition="phone_browse", group="B情境"),
        RecipeSection("音乐耳蜗", "music_cochlea", condition="music_cochlea", group="B情境"),
        RecipeSection("语音耳蜗", "voice_cochlea", group="B情境"),

        # ── 对话注入层 ──
        RecipeSection("对话历史", "conversation_messages", group="对话注入"),
        RecipeSection("时间感知", "time_awareness", condition="time_hint", group="对话注入"),
        RecipeSection("群聊当前", "group_chat_current", group="对话注入"),
        RecipeSection("状态变更履历", "status_changes", group="对话注入"),
        RecipeSection("哨兵注解", "sentinel_timeline", group="对话注入"),

        # ── 群聊特有层 ──
        RecipeSection("Peer 回复", "peer_previous", params={"cap": 300}, group="对话注入"),
        RecipeSection("轮次感知", "round_awareness", group="对话注入"),
    ],
)


SENTINEL_PUSH = Recipe(
    "哨兵-推送",
    "哨兵触发时 AI 主动发消息的 Flash 上下文",
    consumer="AI哨兵",
    sections=[
        *_PUSH_BASE,
        # list ingredient：按时间排序沉到对话历史之后，确保本次变化是生成前最后读取的事实。
        RecipeSection("本次感知事件", "sentinel_event", group="对话注入"),
        RecipeSection("本次主动联系意图", "sentinel_intent", group="对话注入"),
    ],
)


MUSIC_COCHLEA_PUSH = Recipe(
    "音乐耳蜗-推送",
    "音乐耳蜗触发时 AI 主动分享听歌感受的 Pro 上下文（精简，不含哨兵健康/基线段）",
    consumer="AI音乐耳蜗",
    sections=[
        # 稳定前缀
        RecipeSection("身份", "persona", group="A身份"),
        RecipeSection("人设锚点", "persona_anchor", group="A身份"),
        RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
        # 动态块——只保留音乐相关，不含哨兵快照/GPS/群聊
        RecipeSection("情绪", "mood", group="B情境"),
        RecipeSection("时间", "time", group="B情境"),
        RecipeSection("活动摘要", "activity_summary", group="B情境"),
        RecipeSection("天气", "weather", group="B情境"),
        RecipeSection("音乐耳蜗", "music_cochlea", group="B情境"),
        # 对话注入层
        RecipeSection("最近对话", "recent_messages", params={"n": 6}, group="对话注入"),
    ],
)


PHONE_BROWSE = Recipe(
    "刷手机陪聊",
    "刷手机模式极简上下文（仅 4 段）",
    consumer="AI 刷手机",
    sections=[
        RecipeSection("身份", "persona", group="A身份"),
        RecipeSection("情绪", "mood", group="A身份"),
        RecipeSection("时间", "time", group="A身份"),
        RecipeSection("最近对话", "recent_messages", params={"n": 6}, group="对话注入"),
    ],
)


PARALLEL_TIMELINE = Recipe(
    "平行时空",
    "平行时空角色扮演上下文",
    consumer="平行时空",
    sections=[
        RecipeSection("身份", "persona"),
        RecipeSection("角色设定", "roleplay_setup"),
        RecipeSection("故事大纲", "story_outline", params={"cap": 300}),
        RecipeSection("近期摘要", "recent_summary", params={"cap": 500}),
        RecipeSection("对话历史", "timeline_history"),
    ],
)


SELF_TASK = Recipe(
    "自调度任务",
    "定时自唤醒任务的执行上下文（与哨兵/漫想/提醒共享 _PUSH_BASE，AI 醒来时不是失忆的）",
    consumer="自调度",
    sections=[
        *_PUSH_BASE,
        RecipeSection("任务信息", "task_info", group="E系统"),
        RecipeSection("可用工具", "available_tools", group="E系统"),
    ],
)


PEER_GROUP = Recipe(
    "群聊 Peer",
    "群聊中 Peer 的回复上下文",
    consumer="Peer群聊",
    sections=[
        RecipeSection("Peer 身份", "peer_identity"),
        RecipeSection("时间", "time"),
        RecipeSection("记忆快照", "peer_moments"),
        RecipeSection("项目记忆", "peer_memory_files"),
        RecipeSection("群聊历史", "group_chat_history"),
        RecipeSection("AI 回复", "k_previous_reply", params={"cap": 500}),
    ],
)


DIARY = Recipe(
    "日记生成",
    "每日日记生成的 Flash prompt 上下文",
    consumer="日记",
    sections=[
        RecipeSection("日记规则", "diary_rules"),
        RecipeSection("昨日参考", "yesterday_diary"),
        RecipeSection("活跃 TODO", "active_todos"),
        RecipeSection("对话全文", "full_conversation"),
    ],
)


# ── 漫想推送 Recipe ──
# 新运行时把已完成活动的真实细节传给 wander_activity 尾段；旧漫想调用方仍可在最后消息中携带事件。
# wander_mode 由调用方传入 push 版说明文案。

WANDER_PUSH = Recipe("漫想-推送", "漫想触发时 AI 主动发消息的 Flash 上下文", consumer="AI漫想", sections=[
    *_PUSH_BASE,
    RecipeSection("漫想说明", "wander_mode", group="E系统"),
    RecipeSection("最近漫想", "recent_wander", params={"n": 0}, group="对话注入"),
    RecipeSection("本次漫想活动", "wander_activity", group="对话注入"),
])


# ── 漫想 v2 计划/节点边界 LLM 的稳定前缀 ──
# 「全自主区间式行动」的计划 LLM + 节点边界 LLM 高频调用（每 15min 计划、每节点边界）。
# 关键：persona 系列稳定段前置（缓存命中），mood/time 变化段后置（不破坏稳定前缀）。
# 动态运行态由调用方传入最终段；具体输出指令保持为单独的最后 user message。
WANDER_V2 = Recipe("漫想v2-计划/节点", "漫想 v2 计划/节点边界 LLM 稳定前缀", consumer="AI漫想v2", sections=[
    # 纯稳定前缀（缓存友好）：persona 系列逐轮不变，命中 DeepSeek prompt cache。
    # mood/time 是变化段（分钟级/小时级），放前缀会破坏缓存 → 由调用方放 user prompt 动态层。
    RecipeSection("身份", "persona", group="A身份"),
    RecipeSection("人设锚点", "persona_anchor", group="A身份"),
    RecipeSection("行为调整", "active_evolutions", group="A身份"),
    RecipeSection("时间锚点", "timeline_anchor", group="A身份"),
    RecipeSection("开放线索", "open_loops", group="B情境"),
    RecipeSection("漫想运行态", "wander_runtime", group="E系统"),
])

# Node review/settlement receive only this event segment, never prior-run thoughts.
WANDER_ACTIVITY = Recipe("漫想活动", "当前事件段的节点复核、自省与结算", consumer="漫想", sections=[
    section for section in WANDER_V2.sections if section.ingredient != "open_loops"
])
WANDER_V2.sections.insert(-1, RecipeSection("最近内在漫想", "recent_inner_wander", group="D历史"))


REMINDER_PUSH = Recipe(
    "提醒-推送",
    "日程/查岗提醒时的 Flash 上下文",
    consumer="AI提醒",
    sections=[
        *_PUSH_BASE,
        RecipeSection("本次提醒事件", "reminder_event", group="对话注入"),
    ],
)


# ═══════════════════════════════════════════════════════════
# Recipe 注册表
# ═══════════════════════════════════════════════════════════

_ALL_RECIPES: dict = {
    "FULL_CHAT": FULL_CHAT,
    "RIFT_PLAY": RIFT_PLAY,
    "LIGHTWEIGHT": LIGHTWEIGHT,
    "GROUP_CHAT_K": GROUP_CHAT_K,
    "SENTINEL_PUSH": SENTINEL_PUSH,
    "MUSIC_COCHLEA_PUSH": MUSIC_COCHLEA_PUSH,
    "PHONE_BROWSE": PHONE_BROWSE,
    "PARALLEL_TIMELINE": PARALLEL_TIMELINE,
    "SELF_TASK": SELF_TASK,
    "PEER_GROUP": PEER_GROUP,
    "DIARY": DIARY,
    "WANDER_PUSH": WANDER_PUSH,
    "WANDER_V2": WANDER_V2,
    "WANDER_ACTIVITY": WANDER_ACTIVITY,
    "REMINDER_PUSH": REMINDER_PUSH,
}


def get_recipe(name: str) -> Optional[Recipe]:
    """按名获取 Recipe。"""
    return _ALL_RECIPES.get(name)


def list_recipes() -> List[str]:
    """列出所有可用 Recipe 名。"""
    return list(_ALL_RECIPES.keys())
