"""上下文构建器 — Recipe + Builder 模型。

用法：
    from context_builder import ContextBuilder, get_recipe, list_recipes

    # 按 Recipe 名构建
    result = await ContextBuilder.build("FULL_CHAT", session_id=sid, ...)

    # 方法链定制
    builder = ContextBuilder(session_id)
    builder.identity(persona=p, mood=m)
    result = await builder.build()
"""

import time
from datetime import datetime
from typing import Dict, Any

from context_builder.builder import (
    ContextBuilder,
    ContextResult,
    commit_context_result,
    use_context_builder,
    context_diff_enabled,
    maybe_diff_contexts,
)
from context_builder.recipes import get_recipe, list_recipes, Recipe, RecipeSection
from context_builder.ingredients import get_ingredient, estimate_tokens

# ═══════════════════════════════════════════════════
# Recipe 调用追踪（供 /monitor 读取，前端面板展示）
# ═══════════════════════════════════════════════════

_recipe_call_log: Dict[str, dict] = {}  # key = scenario name


def record_recipe_call(scenario: str, recipe_name: str, sections: dict,
                       total_chars: int = 0, total_tokens: int = 0,
                       messages_count: int = 0, extra: dict = None,
                       via_builder: bool = False, msg_order: list = None):
    """记录一次 Recipe 调用（Builder 或旧代码路径均可调用）。

    via_builder=True 表示真正经 ContextBuilder 构建；False 为硬构建自报（仅监控标签）。
    sections 值可为 str 或 dict；dict 时透传 status/condition/count 段级状态字段。
    msg_order: formatted_messages 的简化快照 [{idx, role, section, preview, chars}]，
               供前端展示"AI 的阅读顺序"时间轴。
    """
    def _sec_entry(v):
        if isinstance(v, str):
            return {"chars": len(v), "tokens": estimate_tokens(v), "text": v}
        entry = {"chars": v.get("chars") or 0,
                 "tokens": v.get("tokens") or 0,
                 "text": v.get("text") or ""}
        for k in ("status", "condition", "count"):
            if k in v:
                entry[k] = v[k]
        return entry

    _recipe_call_log[scenario] = {
        "scenario": scenario,
        "recipe": recipe_name,
        "timestamp": datetime.now().isoformat(),
        "via_builder": via_builder,
        "sections": {k: _sec_entry(v) for k, v in sections.items()},
        "total_chars": total_chars or sum(len(v) if isinstance(v, str) else (v.get("chars") or 0) for v in sections.values()),
        "total_tokens": total_tokens or sum(estimate_tokens(v) if isinstance(v, str) else (v.get("tokens") or 0) for v in sections.values()),
        "messages_count": messages_count,
        "extra": extra or {},
        "msg_order": msg_order or [],
    }


def get_recipe_call_log() -> dict:
    """返回所有场景的最近一次 Recipe 调用记录。"""
    return dict(_recipe_call_log)


__all__ = [
    "ContextBuilder", "ContextResult", "commit_context_result",
    "get_recipe", "list_recipes", "Recipe", "RecipeSection",
    "get_ingredient",
    "use_context_builder", "context_diff_enabled", "maybe_diff_contexts",
    "record_recipe_call", "get_recipe_call_log",
]
