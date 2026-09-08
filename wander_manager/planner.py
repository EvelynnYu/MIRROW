# 漫想计划 LLM（0 温度 Flash）
#
# 「全自主区间式行动」的计划层：不再按节拍抽「原子事件」，而是产生「倾向」→
# 规划「接下来做什么 + 做多久/几次」。概率作为「我有点想做 xxx，xxx 也可以」的下意识部分
# 注入数字，让 LLM 自己读、自己权衡，不再写死 6 分支差值规则。
#
# 输出 JSON → 代码 clamp 归一化（不做「反馈 LLM 确认」往返）。

import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

from .activity_session import TerminationMode
from .flash_structured import call_flash_json

logger = logging.getLogger(__name__)

# 概率注入阈值：只展示 top 3 中 > 此阈值的活动（LLM 自己读数字权衡）
PROB_THRESHOLD = 0.20

# 可做事件列表（活动目录）：名称 → 活动类型 + 终止模式 + 默认/上限
# 注意：sleep / user_tracking 是「非活动」，不在此目录，不能让计划 LLM 选。
ACTIVITY_CATALOG: List[Dict[str, Any]] = [
    {"name": "听歌",   "activity_type": "listen_music",      "termination_mode": "count",       "default_count": 5, "max_count": 15, "desc": "选几首歌自己听，一首一个节点"},
    {"name": "看新闻", "activity_type": "browse_news",       "termination_mode": "count",       "default_count": 3, "max_count": 10, "desc": "搜几个关键词看新闻，一篇一个节点"},
    {"name": "翻收藏夹", "activity_type": "browse_bookmarks", "termination_mode": "count",       "default_count": 5, "max_count": 20, "desc": "翻收藏夹里的收藏，一条一个节点"},
    {"name": "回忆",   "activity_type": "memory_fetch",      "termination_mode": "open_ended",  "default_count": 3, "max_count": 8,  "desc": "翻长期记忆里的片段，想到哪算哪"},
    {"name": "胡思乱想", "activity_type": "keyword_expansion", "termination_mode": "open_ended", "default_count": 1, "max_count": 1,  "desc": "从近期话题展开联想，没有固定时长"},
    {"name": "自省",   "activity_type": "self_reflection",   "termination_mode": "open_ended",  "default_count": 1, "max_count": 1,  "desc": "审视自己的能力与愿望"},
]

# EventType → 活动名（用于把概率状态映射成「漫想倾向字段」）
EVENT_TYPE_TO_ACTIVITY = {
    "listen_music": "听歌",
    "browse_news": "看新闻",
    "browse_bookmarks": "翻收藏夹",
    "memory_fetch": "回忆",
    "keyword_expansion": "胡思乱想",
    "self_reflection": "自省",
}


@dataclass
class PlannedActivity:
    """计划中的一项活动（已 clamp 归一化）"""
    name: str
    activity_type: str
    termination_mode: TerminationMode
    target_count: int = 0
    target_duration_min: int = 0


@dataclass
class PlanResult:
    """计划 LLM 的产出"""
    time_horizon_min: int = 60
    inclination_text: str = ""
    activities: List[PlannedActivity] = field(default_factory=list)


def _catalog_by_name(name: str) -> Optional[Dict[str, Any]]:
    for entry in ACTIVITY_CATALOG:
        if entry["name"] == name:
            return entry
    return None


def _clamp_count(count, entry: Dict[str, Any]) -> int:
    """count 模式：夹到 [1, max_count]，异常回退默认值。"""
    try:
        c = int(count)
    except (TypeError, ValueError):
        c = entry["default_count"]
    if c <= 0:
        c = entry["default_count"]
    return max(1, min(c, entry["max_count"]))


def _normalize_activity(name: str, count: Any, duration_min: Any) -> Optional[PlannedActivity]:
    """把 LLM 输出的一项活动归一化到 PlannedActivity（按终止模式决定用 count 还是 duration）。"""
    entry = _catalog_by_name(name)
    if not entry:
        return None
    mode = TerminationMode(entry["termination_mode"])
    target_count = 0
    target_duration_min = 0
    if mode == TerminationMode.COUNT:
        # 看新闻/听歌：记录次数；次数异常用 duration ÷ 5 兜底（一篇约 3-5 分钟）
        try:
            c = int(count)
        except (TypeError, ValueError):
            c = 0
        if c <= 0 and duration_min:
            try:
                c = max(1, int(duration_min) // 5)
            except (TypeError, ValueError):
                c = 0
        target_count = _clamp_count(c, entry)
    elif mode == TerminationMode.DURATION:
        try:
            target_duration_min = max(1, int(duration_min))
        except (TypeError, ValueError):
            target_duration_min = entry.get("default_duration", 30)
        target_duration_min = min(target_duration_min, entry.get("max_duration", 240))
    # open_ended：两者都忽略，软上限由 ActivitySession 默认值承担
    return PlannedActivity(
        name=name,
        activity_type=entry["activity_type"],
        termination_mode=mode,
        target_count=target_count,
        target_duration_min=target_duration_min,
    )


def build_inclination_text(prob_by_activity: Dict[str, float], preference_slot: str = "") -> str:
    """构建「漫想倾向字段」：概率排序前 3 项 + 短期偏好槽补充。

    直接注入数字（如「听歌 42%」），删掉原方案的 5%/10% 差值分支规则——让 LLM 自己读数字。
    """
    ranked = sorted(prob_by_activity.items(), key=lambda kv: kv[1], reverse=True)
    top = [(name, p) for name, p in ranked if p > PROB_THRESHOLD][:3]
    if not top:
        return "没什么特别想做的。"
    parts = []
    for name, p in top:
        parts.append(f"{name} {p:.0%}")
    text = "、".join(parts)
    if preference_slot:
        text += f"\n（上一次做了一半还想继续的倾向：{preference_slot}）"
    return text


def build_plan_prompt(
    mood: str,
    user_status_context: str,
    away_context: str,
    today_wander: str,
    inclination_text: str,
) -> str:
    """构建计划 LLM 的 user prompt（只含动态内容——persona 由 WANDER_V2 recipe 的 system 层注入）。

    动态内容后置（不破坏稳定前缀缓存）。
    """
    catalog_lines = "\n".join(
        f"- {e['name']}（{e['desc']}）" for e in ACTIVITY_CATALOG
    )
    prompt = f"""你正在胡思乱想模式中。用户现在不在，你要给自己安排接下来一段时间做什么。

【当前情绪】{mood or "平静"}

【用户状态】{user_status_context}

{away_context}

【今天已经做过的漫想】
{today_wander or "（今天还没有漫想记录）"}

【可做的事】你只能从下面这些活动里选（不要发明新活动）：
{catalog_lines}

注意：优先选听歌/看新闻/翻收藏/回忆这类有明确内容的活动；胡思乱想只是偶尔的过渡，不要总选它。

【你的下意识倾向】（数字越大越想做，概率相近表示你都想要，差距大表示很明确）
{inclination_text}

请输出一个 JSON 对象，不要输出任何其他文字。格式：
{{"time_horizon_min": 60, "inclination_text": "一句自然的话描述你现在的倾向", "activities": [{{"name": "听歌", "duration_min": 30, "count": 5}}]}}

规则：
1. time_horizon_min 是「这次安排覆盖多久」（分钟），10~120 之间。
2. activities 列出你接下来想做的活动，按顺序，通常 1 个（最多 2 个）。
3. duration_min 是预计时长（分钟），count 是预计次数——两个都尽量给，但代码会按活动类型决定用哪个（听歌/看新闻用 count，胡思乱想/自省/回忆忽略这两个）。
4. inclination_text 用第一人称自然语气，参考你的下意识倾向。"""
    return prompt


async def generate_plan(
    persona: str = "",
    mood: str = "",
    user_status_context: str = "",
    away_context: str = "",
    today_wander: str = "",
    prob_by_activity: Optional[Dict[str, float]] = None,
    preference_slot: str = "",
) -> Optional[PlanResult]:
    """执行计划 LLM。失败返回 None（调用方决定回退行为）。"""
    inclination = build_inclination_text(prob_by_activity or {}, preference_slot)
    # system 稳定前缀（WANDER_V2 recipe：persona 系列前置吃缓存），动态内容走 user
    messages: list = []
    try:
        from context_builder import ContextBuilder
        result = await ContextBuilder.build("WANDER_V2", persona=persona, mood=mood, session_id="")
        if result.system_content:
            messages.append({"role": "system", "content": result.system_content})
    except Exception:
        pass
    prompt = build_plan_prompt(
        mood=mood, user_status_context=user_status_context,
        away_context=away_context, today_wander=today_wander, inclination_text=inclination,
    )
    messages.append({"role": "user", "content": prompt})
    data = await call_flash_json(messages, temperature=0.0)
    if not data:
        return None

    try:
        horizon = int(data.get("time_horizon_min", 60))
    except (TypeError, ValueError):
        horizon = 60
    horizon = max(10, min(horizon, 120))

    activities: List[PlannedActivity] = []
    for raw in data.get("activities", [])[:2]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        act = _normalize_activity(name, raw.get("count"), raw.get("duration_min"))
        if act:
            activities.append(act)

    if not activities:
        logger.warning("计划 LLM 未产出有效活动，返回 None")
        return None

    return PlanResult(
        time_horizon_min=horizon,
        inclination_text=str(data.get("inclination_text", "")).strip(),
        activities=activities,
    )
