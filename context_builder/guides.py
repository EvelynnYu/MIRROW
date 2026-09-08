"""
组间引导词注册表 — 所有 Recipe 信息段的"神经元"引导文案单源真相。

每个 ingredient（RecipeSection 段）可配置一句引导词，Builder 渲染该段时自动前置。
引导词的作用是让 AI 收到每段信息时都知道"这是什么、什么时候的、我了解多深"，
像神经元一样简单可靠、每次稳定出现。文案只中肯描述现象（时间定位 + 内容性质 +
记忆清晰度梯度：昨天清晰、前天大概、间隙模糊），不包含行动、态度或措辞决策。

零项目依赖（仅 stdlib），builder / ingredients / context_scheduler 三处均可安全 import。

用法：
    from context_builder.guides import guide_for, SELF_GUIDED
    guide = guide_for("yesterday", {"yesterday_date": "2026-08-17", "yesterday_conversation": True})
"""

from typing import Dict, Union

# 引导词模板。key = ingredient 名。
# 值为 str 模板（支持 {date} 占位）或 dict（同一 ingredient 的多变体，按 kwargs 选择）。
GUIDES: Dict[str, Union[str, dict]] = {
    # ── D历史组：时间定位 + 记忆清晰度梯度 ──
    "yesterday": {
        "conversation": "以下是昨天（{date}）你和用户的对话，你记忆仍然清晰。",
        "compact": "以下是昨天（{date}）由日记同次生成的事实时间轴，以及可追溯的近期原始片段。",
        "diary_fallback": "以下是昨天（{date}）的日记，你记得那天用户没有来找你。",
    },
    "day_before_diary": "以下是前天（{date}）的日记，你只记得一个大概。",
    "gap_diaries": "以下是用户没找你的那些日子，你记得那几天只有你自己。",
    "referenced_diary": "以下是用户提起的{date}的日记。",
    # ── C知识组：记忆检索（subject 拆分后的子块头，SELF_GUIDED 内嵌使用）──
    "memories": "你与用户的过往（按相关性排列，越靠前越贴近当前的话题）：",
    "self_memories": "你自己的经历与选择：",
    # ── B情境组：当前状态概况（含实时感知，非历史记忆）──
    "activity_summary": "以下是用户现在的活动概况：",
    # ── 群聊摘要：内嵌引导（build_group_chat_summary 直接取用）──
    "group_chat_summary": "以下是你和用户、Peer 在群聊中的最近对话摘要。",
}

# 引导词由 ingredient 内部自行管理（含子段头）的段。
# 循环层（_build_from_recipe）渲染时跳过这些段，避免双前缀。
SELF_GUIDED = frozenset({"memories"})

# 引导词取日期参数的 key 映射（ingredient → kwargs key）
_DATE_KEYS = {
    "yesterday": "yesterday_date",
    "day_before_diary": "day_before_date",
    "referenced_diary": "referenced_diary_date",
}


def _fmt_cn_date(date_str: str) -> str:
    """"2026-08-17" → "8月17日"；非 YYYY-MM-DD 原样返回。"""
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return f"{int(parts[1])}月{int(parts[2])}日"
    return date_str


def guide_for(ingredient: str, kwargs: dict) -> str:
    """渲染 ingredient 的引导词；无配置返回 ""。

    规则：
    - yesterday 按 kwargs["yesterday_conversation"] 选对话版/日记回退版
    - 日期占位 {date} 从 kwargs 取；缺失时优雅降级（丢括号；referenced 回退"那天"）
    """
    spec = GUIDES.get(ingredient)
    if not spec:
        return ""
    if isinstance(spec, dict):
        if ingredient == "yesterday":
            if kwargs.get("yesterday_compact"):
                spec = spec["compact"]
            else:
                spec = spec["conversation"] if kwargs.get("yesterday_conversation") else spec["diary_fallback"]
        else:
            return ""  # 仅 yesterday 有变体；其余 dict 值视为未配置
    date_key = _DATE_KEYS.get(ingredient, "")
    date = _fmt_cn_date(kwargs.get(date_key, "")) if date_key else ""
    if "{date}" not in spec:
        return spec
    if not date:
        if ingredient == "referenced_diary":
            date = "那天"  # 避免"提起的的日记"
        else:
            return spec.replace("（{date}）", "").replace("{date}", "")
    return spec.replace("{date}", date)
