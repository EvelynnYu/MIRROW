"""
ContextBuilder — 按 Recipe 组装上下文。

用法：
    # 按 Recipe 名（90% 场景）
    result = await ContextBuilder.build("FULL_CHAT", session_id=sid, user_message=msg, ...)

    # 方法链（10% 定制场景）
    builder = ContextBuilder(session_id)
    builder.identity(persona=persona, mood=mood)
    builder.situational(status=True, away=True)
    result = await builder.build()

灰度控制：
    MIRROW_USE_CONTEXT_BUILDER=1 → 使用 Builder
    MIRROW_CONTEXT_DIFF=1 → 并行运行新旧，差异日志
"""

import os
import logging
from typing import Dict, Any, Optional, List

from context_builder.ingredients import (
    get_ingredient, estimate_tokens,
)
from context_builder.recipes import get_recipe, Recipe, RecipeSection
from context_builder.guides import guide_for, SELF_GUIDED
from context_builder.text_utils import build_tool_history_fact, strip_internal_history_markers
from message_roles import is_ui_only_role

logger = logging.getLogger(__name__)


class ContextResult:
    """构建结果——兼容现有 ContextResult 接口"""
    def __init__(self):
        self.system_content: str = ""
        self.dynamic_content: str = ""  # 动态块（P4：移到对话历史之后，命中缓存）
        self.formatted_messages: list = []
        self.memories: list = []
        self.stats: dict = {}
        self.sections: dict = {}       # 监控台展示用
        self.section_groups: list = [] # 监控台分层展示用
        # Stable identity for the final-prompt audit and deferred one-shot
        # resource commit.  Empty is valid for legacy callers that never pass
        # a provider boundary.
        self.call_id: str = ""
        self.one_shot_claims: list = []


# P4 稳定段白名单：这些段放 system 前缀（缓存命中 + 首因锚）。
# 其余 str 段（memories/yesterday/time/weather 等）在灰度开启时移到动态块（对话历史之后）。
_STABLE_SECTIONS = {
    "persona", "persona_anchor", "active_evolutions",
    "timeline_anchor", "message_markers", "tool_descriptions", "user_profile",
    # D历史组：同一天内稳定（今天每轮注入相同内容），放稳定前缀命中缓存
    "day_before_diary", "yesterday", "gap_diaries", "important_events",
    # 注意：self_book 不在此列——它是「按当前消息检索」的动态段（sb.search(user_message)），
    # 每轮结果不同，若放稳定前缀会破坏缓存前缀（命中率从 84% 掉到 1.5% 的根因）。
}


def dynamic_last_enabled() -> bool:
    """灰度开关：MIRROW_CONTEXT_DYNAMIC_LAST=1 时把动态块移到对话历史之后。"""
    return os.environ.get("MIRROW_CONTEXT_DYNAMIC_LAST") == "1"


# P4 通用动态后移只对主聊天 recipe 生效。主动推送的核心事件通过 list ingredient
# 显式成为对话历史后的 system 尾消息，不依赖此灰度开关，也不会被只取 system_content 静默丢失。
_DYNAMIC_LAST_RECIPES = {"FULL_CHAT", "GROUP_CHAT_K", "RIFT_PLAY"}


class ContextBuilder:
    """上下文构建器"""

    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self._recipe_name: str = ""      # 当前使用的 Recipe 名（中文显示名，如"主聊天（完整）"）
        self._recipe_key: str = ""       # Recipe 注册表 key（如"FULL_CHAT"，P4 动态后移判断用）
        self._scenario_override: str = ""  # 监控台场景名覆盖（如"查岗"区分于"提醒-推送"）
        self._system_content: str = ""
        self._dynamic_content: str = ""  # 动态块（P4：移到对话历史之后，命中缓存）
        self._extra_messages: list = []  # system_content 之外的独立消息
        self._sections: dict = {}        # {ingredient_name: content}
        self._section_status: dict = {}  # {ingredient_name: {"status": ok/gated/empty/error/interleaved, ...}}
        self._kwargs: dict = {}          # 传入的上下文参数
        self._flags: dict = {}           # 条件注入标志
        self._conversation_empty_today = False  # 空态：今天无用户消息（供推送 recipe 边界回填提示）
        self._push_diary_kwargs = None  # 推送场景自取昨日背景缓存（一天内不变）
        self._one_shot_claims: list = []  # peeked prosody/sentinel resources

    # ── 静态入口 ──

    @classmethod
    async def build(cls, recipe_name: str, **kwargs) -> ContextResult:
        """按 Recipe 名构建上下文。"""
        recipe = get_recipe(recipe_name)
        if not recipe:
            raise ValueError(f"Unknown recipe: {recipe_name}")
        builder = cls(kwargs.get("session_id", ""))
        builder._recipe_key = recipe_name  # 存注册表 key，供 P4 动态后移判断
        return await builder._build_from_recipe(recipe, **kwargs)

    # ── 方法链（定制场景用）──

    def identity(self, persona: str = "", mood: str = "", anchor_cap: int = 0) -> "ContextBuilder":
        """注入身份层。"""
        if persona:
            if mood:
                persona = f"{persona}\n[当前情绪：{mood}]"
            self._add("persona", persona)
        self._add("persona_anchor", "")  # 由 build 时处理
        self._add("timeline_anchor", build_timeline_anchor())
        self._add("time", build_time_section())
        return self

    def situational(self, include: List[str] = None, exclude: List[str] = None) -> "ContextBuilder":
        """注入情境层（状态/生理期/离开/哨兵/心率等）。"""
        all_items = {
            "user_status": build_user_status_section,
            "period": build_period_section,
            "away": lambda: None,  # 外部注入
            "sentinel_snapshot": build_sentinel_snapshot,
            "heart_rate": build_heart_rate_section,
        }
        for name, fn in all_items.items():
            if include and name not in include:
                continue
            if exclude and name in exclude:
                continue
            result = fn() if callable(fn) else None
            if result:
                self._add(name, result)
        return self

    def knowledge(self, memory_top_k: int = 10, memory_cap: int = 0,
                  world_book: bool = True, scp: bool = True) -> "ContextBuilder":
        """注入知识层（记忆/世界书/SCP）。"""
        self._kwargs["memory_top_k"] = memory_top_k
        self._kwargs["memory_cap"] = memory_cap
        self._flags["world_book"] = world_book
        self._flags["scp"] = scp
        return self

    def history(self, **kwargs) -> "ContextBuilder":
        self._kwargs.update(kwargs)
        self._flags["inject_history"] = True
        return self

    def conversation(self) -> "ContextBuilder":
        self._flags["include_conversation"] = True
        return self

    def custom_section(self, name: str, content: str) -> "ContextBuilder":
        self._add(name, content)
        return self

    # ── 内部构建 ──

    async def _build_from_recipe(self, recipe: Recipe, **kwargs) -> ContextResult:
        """遍历 Recipe.sections，逐个调用 ingredients。"""
        self._recipe_name = recipe.name
        self._scenario_override = kwargs.pop("_scenario", "") or ""
        # 保存 time_awareness_hint——它同时用于条件标志和 ingredient 数据
        _time_hint_val = kwargs.pop("time_awareness_hint", None)
        self._flags = {
            "user_status_not_idle": kwargs.pop("user_status_not_idle", True),
            "period_active": kwargs.pop("period_active", True),
            "away": kwargs.pop("away", False),
            "night_mode": kwargs.pop("night_mode", False),
            "phone_browse": kwargs.pop("phone_browse", False),
            "music_cochlea": kwargs.pop("music_cochlea", False),
            "wander_active": kwargs.pop("is_wander_active", False),
            "is_new_topic": kwargs.pop("is_new_topic", False),
            "inject_yesterday": kwargs.pop("inject_yesterday", False),
            "date_referenced": kwargs.pop("date_referenced", False),
            "time_hint": _time_hint_val is not None,
            "has_temp_summary": kwargs.pop("has_temp_summary", False),
            "rift_game_context": bool(kwargs.get("rift_game_context_text")),
        }
        self._kwargs = kwargs
        if _time_hint_val:
            self._kwargs["time_awareness_hint"] = _time_hint_val  # ingredient 需要此数据

        for section in recipe.sections:
            # 条件检查
            if section.condition and not self._flags.get(section.condition):
                self._sections[section.ingredient] = ""  # 条件跳过也记录
                self._section_status[section.ingredient] = {"status": "gated", "condition": section.condition}
                continue

            try:
                content = await self._call_ingredient(section)
                if content and isinstance(content, list):
                    self._add(section.ingredient, content)  # list → extra_messages
                    self._sections[section.ingredient] = ""  # 内容在 extra_messages，不进 system 快照
                    self._section_status[section.ingredient] = {"status": "interleaved", "count": len(content)}
                elif content and str(content).strip():
                    # 组间引导词：str 段渲染时自动前置（list 段在更早分支 return，天然跳过）
                    # 挂载点必须在 dynamic/_add 之前——P4 动态后移绕过 _add，这里统一渲染保证两路都带引导
                    text = str(content).strip()
                    guide = ""
                    if section.guide is not None:
                        guide = section.guide          # ""=禁用，非空=覆盖
                    elif section.ingredient not in SELF_GUIDED:
                        guide = guide_for(section.ingredient, self._kwargs)
                    if guide:
                        text = f"{guide}\n{text}"
                    if (dynamic_last_enabled()
                            and self._recipe_key in _DYNAMIC_LAST_RECIPES
                            and section.ingredient not in _STABLE_SECTIONS):
                        # P4 动态段 → dynamic_content（移到对话历史之后，命中缓存）
                        self._dynamic_content += text + "\n\n"
                        self._sections[section.ingredient] = text
                        self._section_status[section.ingredient] = {"status": "dynamic"}
                    else:
                        self._add(section.ingredient, text)  # string → system_content
                        self._section_status[section.ingredient] = {"status": "ok"}
                else:
                    self._sections[section.ingredient] = str(content) if content else ""
                    self._section_status[section.ingredient] = {"status": "empty"}
            except Exception:
                logger.warning(f"Ingredient '{section.ingredient}' failed, skipping")
                self._sections[section.ingredient] = ""
                self._section_status[section.ingredient] = {"status": "error"}

        return self._finalize()

    async def _call_ingredient(self, section: RecipeSection) -> Optional[str]:
        """调用单个 ingredient 函数。"""
        name = section.ingredient
        params = dict(section.params)

        # 合并外部 kwargs
        if name == "persona":
            # 注意：mood 不再内嵌进 persona（会打断缓存前缀，trap 64 已改为独立动态段）
            persona = self._kwargs.get("persona", "")
            cap = int(params.get("cap", 0) or 0)
            if cap > 0 and len(persona) > cap:
                # 保留前 cap 字符，在最近句号/换行处截断
                cut = persona[:cap].rfind("。")
                if cut < cap // 2:
                    cut = persona[:cap].rfind("\n")
                if cut > 0:
                    persona = persona[:cut+1]
                else:
                    persona = persona[:cap]
            return persona
        if name == "persona_anchor":
            persona = self._kwargs.get("persona", "")
            if persona:
                from mirrow_core.truncation_config import get_truncation_limit
                cap = get_truncation_limit("persona_anchor_cap")
                return persona.split("。")[0][:cap] if cap > 0 else persona.split("。")[0]
            return ""
        if name == "mood":
            mood = self._kwargs.get("mood", "")
            return f"[当前情绪：{mood}]" if mood else ""
        if name == "night_mode":
            return self._kwargs.get("night_mode_text", "")
        if name == "phone_browse":
            return self._kwargs.get("phone_browse_text", "")
        if name == "rift_game_context":
            return self._kwargs.get("rift_game_context_text", "")
        if name == "music_cochlea":
            fn = get_ingredient("music_cochlea")
            if fn:
                return fn()
            return ""
        if name == "voice_cochlea":
            # Voice prosody is request-scoped one-shot material.  The
            # ingredient only peeks; the provider boundary commits the claim
            # after an accepted response, so diagnostics/build failures can
            # retry without losing it.
            fn = get_ingredient("voice_cochlea")
            if not fn:
                return ""
            result = fn()
            try:
                from voice_manager.voice_cochlea import get_current_prosody_id
                prosody_id = get_current_prosody_id()
                if prosody_id and result:
                    self._one_shot_claims.append({"kind": "prosody", "stable_id": prosody_id})
            except Exception:
                pass
            return result
        if name == "wander_mode":
            return self._kwargs.get("wander_mode_text", "")
        if name == "wander_runtime":
            # WANDER_V2 专用 passthrough：调用方已按运行态合同构造，保持 recipe 最后一段。
            return self._kwargs.get("wander_runtime_text", "")
        if name == "push_current_state":
            # 哨兵在作出 PUSH 决定时会冻结当时的状态文本，确保判断与生成看到同一份事实。
            frozen = self._kwargs.get("push_current_state_text", "")
            if frozen:
                return [{
                    "role": "system",
                    "content": str(frozen).strip(),
                    "_ts": "9999-12-31T23:59:50",
                    "_section": "push_current_state",
                }]
        if name == "sentinel_intent":
            fn = get_ingredient("sentinel_intent")
            text = fn(
                self._kwargs.get("sentinel_summary", ""),
                self._kwargs.get("sentinel_push_motivation", ""),
            ) if fn else ""
            if not text:
                return []
            return [{
                "role": "system",
                "content": text,
                "_ts": "9999-12-31T23:59:59",
                "_section": "sentinel_intent",
            }]
        if name in {"sentinel_event", "reminder_event", "wander_activity"}:
            fn_name = {
                "sentinel_event": "sentinel_summary",
                "reminder_event": "reminder_event",
                "wander_activity": "wander_activity",
            }[name]
            fn = get_ingredient(fn_name)
            if not fn:
                return []
            if name == "sentinel_event":
                text = fn(self._kwargs.get("sentinel_summary", ""))
            elif name == "reminder_event":
                text = fn(self._kwargs.get("task_info", ""))
            else:
                text = fn(self._kwargs.get("wander_activity_text", ""))
            if not text:
                return []
            return [{
                "role": "system",
                "content": text,
                "_ts": "9999-12-31T23:59:58",
                "_section": name,
            }]
        if name == "message_markers":
            from context_builder.ingredients import MESSAGE_MARKERS_TEXT
            return MESSAGE_MARKERS_TEXT
        if name == "tool_descriptions":
            # passthrough：调用方预构建的描述（scheduler 在 Builder 之后拼接）
            prebuilt = self._kwargs.get("tools_desc", "")
            if prebuilt:
                return prebuilt
            # 或者直接调 ingredient function 渲染
            tools = self._kwargs.get("tools", {})
            fn = get_ingredient("tool_descriptions")
            if fn and tools:
                return fn(tools=tools)
            if fn:
                return fn()  # 无 tools 时由 ingredient 决定传什么
        if name == "user_profile":
            profile = self._kwargs.get("user_profile", "")
            if profile and profile.strip():
                return f"用户资料: {profile.strip()}"
            return ""
        if name == "available_tools":
            return self._kwargs.get("tools_desc", "")
        if name == "group_chat_summary":
            fn = get_ingredient("group_chat_summary")
            if fn:
                import asyncio
                try:
                    return await fn() if asyncio.iscoroutinefunction(fn) else fn()
                except Exception:
                    pass
            return ""
        if name == "group_chat_current":
            # async ingredient → returns list of dicts → extra_messages
            fn = get_ingredient("group_chat_current")
            if fn:
                import asyncio
                try:
                    return await fn(**self._build_fn_kwargs(name, {}, fn))
                except Exception:
                    pass
            return ""
        if name == "host_group_current":
            fn = get_ingredient("host_group_current")
            room_id = str(self._kwargs.get("host_group_room_id", "") or "")
            return await fn(room_id=room_id) if fn and room_id else ""
        if name == "group_tool_ban":
            fn = get_ingredient("group_tool_ban")
            return fn() if fn else ""
        if name == "peer_previous":
            content = self._kwargs.get("peer_prev_content", "")
            cap = int(params.get("cap", 300))
            fn = get_ingredient("peer_previous")
            return fn(content, cap) if fn else ""
        if name == "round_awareness":
            fn = get_ingredient("round_awareness")
            rn = self._kwargs.get("round_num", 1)
            mr = self._kwargs.get("max_rounds", 1)
            sk = self._kwargs.get("start_with_k", True)
            return fn(rn, mr, sk) if fn else ""
        if name == "day_before_diary":
            # 主聊天 schedule_context 预构建传入；推送场景键不存在 → 自取昨日背景
            if "day_before_diary" in self._kwargs:
                return self._kwargs.get("day_before_diary", "")
            dk = await self._resolve_push_diary()
            # 回写 day_before_date 供引导词 {date} 占位（否则显示"以下是前天的日记"缺日期）
            self._kwargs["day_before_date"] = dk.get("day_before", "")
            return dk.get("day_before_diary", "")
        if name == "gap_diaries":
            # 主聊天 schedule_context 预构建传入；推送场景键不存在 → 自取昨日背景
            if "gap_diaries" in self._kwargs:
                return self._kwargs.get("gap_diaries", "")
            dk = await self._resolve_push_diary()
            return dk.get("gap_diaries_text", "")
        if name == "referenced_diary":
            return self._kwargs.get("referenced_diary", "")
        if name == "important_events":
            return self._kwargs.get("important_events", "")
        if name == "recent_messages":
            # 委托给 conversation_messages（已合并，返回 list 🔀 穿插格式）
            return await self._call_ingredient(RecipeSection("对话历史", "conversation_messages"))
        # sentinel_summary 不再特殊分支——走 ingredient（build_sentinel_summary，AI 主体润滑）
        if name == "wander_event":
            return self._kwargs.get("wander_event", "")
        if name == "user_status_context":
            return self._kwargs.get("user_status_context", "")
        if name == "away_duration":
            return self._kwargs.get("away_duration", "")
        if name == "task_info":
            return self._kwargs.get("task_info", "")
        if name == "recent_wander":
            if self._kwargs.get("exclude_wander_history"):
                return ""
            val = self._kwargs.get("recent_wander", "")
            current_id = str(self._kwargs.get("current_activity_id") or "").strip()
            current_text = str(self._kwargs.get("wander_activity_text") or "").strip()
            # Legacy callers may still pass a preformatted list.  Do not
            # repeat the current activity when its summary is recognizable.
            if val and current_text and str(val).strip() == current_text:
                val = ""
            if val:
                return val
            # 调用方未传 → 自取今日漫想消息尾部 N 条（帮 AI 避免重复推送同类内容）
            try:
                from event_chronicle import get_global_chronicle
                from datetime import datetime as _dt
                chronicle = get_global_chronicle()
                today = _dt.now().strftime("%Y-%m-%d")
                msgs = chronicle.get_messages_by_date(today) or []
                n = int(params.get("n", 5))
                wanders = []
                for m in msgs:
                    if not m.get("is_wander"):
                        continue
                    mid = str(m.get("activity_id") or m.get("message_id") or m.get("id") or "").strip()
                    if current_id and mid == current_id:
                        continue
                    mtext = str(m.get("content") or "").strip()
                    if current_text and mtext and mtext == current_text:
                        continue
                    wanders.append(m)
                wanders = wanders[-n:]
                if wanders:
                    lines = ["## 你今天已发过的漫想"]
                    for m in wanders:
                        lines.append(f"- {(m.get('content') or '')[:150]}")
                    return "\n".join(lines)
            except Exception:
                pass
            return ""
        if name == "world_book_summaries":
            return self._kwargs.get("world_book_summaries", "")
        # ── 预计算原料（passthrough：schedule_context 已预计算，Builder 只透传格式化）──
        if name == "memories":
            mems = self._kwargs.get("memories", "")
            if isinstance(mems, list) and mems:
                top_k = int(params.get("top_k", 10))
                # 按 subject 拆分：subject=AI 是 AI 自己的经历与选择（自我记忆），
                # 与用户相关的记忆分开成块——防止 AI 自我记忆被错标成"你与用户的过往"
                anh_parts = []   # 与用户相关（subject 非 AI）
                self_parts = []  # AI 自己（subject=AI）
                neighborhood_parts = []  # 命中后的弱注入，折叠为一条背景提示
                anh_i = self_i = 0
                for mem in mems[:top_k]:
                    if hasattr(mem, 'source') and mem.source == "conversation":
                        continue
                    md = getattr(mem, 'metadata', {}) or {}
                    content = getattr(mem, 'content', '') if hasattr(mem, 'content') else str(mem)
                    if md.get("is_neighborhood"):
                        label = md.get("neighborhood_scene_label", "")
                        summary = str(content).replace("\n", " ").strip()[:100]
                        if summary:
                            neighborhood_parts.append(f"{label}: {summary}" if label else summary)
                        continue
                    subject = md.get("subject", "用户")
                    meta_parts = []
                    for key in ("time_label", "emotion_label", "domain"):
                        v = (md.get("time_axis_label") or md.get(key, "")) if key == "time_label" else md.get(key, "")
                        if v:
                            meta_parts.append(", ".join(v) if isinstance(v, list) else str(v))
                    meta_str = " · ".join(meta_parts)
                    content += str(md.get("source_hint") or "")
                    # ``K`` is the historical storage subject for AI-owned
                    # memories.  It is a compatibility key, not display text.
                    if subject == "K":
                        self_i += 1
                        self_parts.append(f"相关记忆 {self_i}: ({meta_str}) {content}" if meta_str else f"相关记忆 {self_i}: {content}")
                    else:
                        anh_i += 1
                        anh_parts.append(f"相关记忆 {anh_i}: ({meta_str}) {content}" if meta_str else f"相关记忆 {anh_i}: {content}")
                if neighborhood_parts:
                    anh_parts.append("（记忆邻域·背景折叠：" + "；".join(neighborhood_parts[:2]) + "）")
                # 两个子块各自带引导头（SELF_GUIDED 保证循环层不叠加）
                blocks = []
                if anh_parts:
                    anh_header = guide_for("memories", self._kwargs)
                    if any(getattr(mem, "metadata", {}).get("time_axis_label") for mem in mems[:top_k]):
                        anh_header += "\n## 时间轴回忆"
                    blocks.append(anh_header + "\n" + "\n".join(anh_parts))
                if self_parts:
                    blocks.append(guide_for("self_memories", self._kwargs) + "\n" + "\n".join(self_parts))
                return "\n\n".join(blocks) if blocks else ""
            return str(mems) if mems else ""
        if name == "yesterday":
            # 主聊天 schedule_context 预构建传入；推送场景键不存在 → 自取昨日背景
            if "yesterday_conversation" in self._kwargs or "yesterday_diary_fallback" in self._kwargs:
                return self._kwargs.get("yesterday_conversation", "") or self._kwargs.get("yesterday_diary_fallback", "")
            dk = await self._resolve_push_diary()
            # 回写 kwargs：挂载点 guide_for 用 self._kwargs 选 yesterday 引导词变体（对话/日记回退）与日期，
            # 不回写会因 kwargs 无 yesterday_conversation 而误选"日记"版引导词（内容实为对话）
            self._kwargs["yesterday_conversation"] = dk.get("yesterday_conversation", "")
            self._kwargs["yesterday_diary_fallback"] = dk.get("yesterday_diary_fallback", "")
            self._kwargs["yesterday_date"] = dk.get("yesterday", "")
            self._kwargs["yesterday_compact"] = dk.get("yesterday_compact", "")
            return (
                dk.get("yesterday_compact", "")
                or dk.get("yesterday_conversation", "")
                or dk.get("yesterday_diary_fallback", "")
            )
        if name == "recent_activity":
            return self._kwargs.get("recent_activity_text", "")
        if name == "away":
            return self._kwargs.get("away_info", "")
        if name == "world_book":
            entries = self._kwargs.get("world_book_entries", "")
            if isinstance(entries, list) and entries:
                top_k = int(params.get("top_k", 3))
                lines = ["## 世界书参考"]
                for entry in entries[:top_k]:
                    name = entry.get('name', '') if isinstance(entry, dict) else getattr(entry, 'name', '')
                    body = (entry.get('body', '') or "") if isinstance(entry, dict) else (getattr(entry, 'body', '') or "")
                    lines.append(f"### {name}")
                    lines.append(str(body))
                return "\n".join(lines)
            return str(entries) if entries else ""
        if name == "scp":
            entries = self._kwargs.get("scp_entries", "")
            if isinstance(entries, list) and entries:
                top_k = int(params.get("top_k", 2))
                lines = ["## SCP 灵魂补全计划参考"]
                for entry in entries[:top_k]:
                    if isinstance(entry, dict):
                        name = entry.get('name', '')
                        body = entry.get('body', '') or ""
                        category = entry.get('category', '')
                    else:
                        name = getattr(entry, 'name', '')
                        body = getattr(entry, 'body', '') or ""
                        category = getattr(entry, 'category', '')
                    header = f"### {name}" + (f" ({category})" if category else "")
                    lines.append(header)
                    lines.append(str(body))
                return "\n".join(lines)
            return str(entries) if entries else ""
        if name == "self_book":
            entries = self._kwargs.get("self_book_entries", "")
            if isinstance(entries, list) and entries:
                top_k = int(params.get("top_k", 3))
                lines = ["## 自我书"]
                for entry in entries[:top_k]:
                    if isinstance(entry, dict):
                        name = entry.get('name', '')
                        body = entry.get('body', '') or ""
                    else:
                        name = getattr(entry, 'name', '')
                        body = getattr(entry, 'body', '') or ""
                    lines.append(f"### {name}")
                    lines.append(str(body))
                return "\n".join(lines)
            if entries:
                return str(entries)
            # 主动消息没有新的 user_message，以本次触发事实作为语义检索锚点。
            query = (self._kwargs.get("context_query", "")
                     or self._kwargs.get("user_message", ""))
            fn = get_ingredient("self_book")
            if fn and query:
                return fn(query, top_k=int(params.get("top_k", 3)))
            return ""
        if name == "conversation_messages":
            mems = self._kwargs.get("conversation_messages", [])
            # 推送 recipe 不传 conversation_memories（无 context_scheduler），从 chronicle 自取
            if not mems:
                try:
                    from wander_manager.host_hooks import get_chronicle as get_global_chronicle
                    from datetime import datetime as _dt
                    chronicle = get_global_chronicle()
                    # 用话题状态机拿「当前话题开始日」（= active_date 活跃日），而非自己 hack 今天/昨天——
                    # 与主聊天 get_conversation_memories 同源：今天新话题→今天，跨午夜→话题开始日（昨天的日期）
                    _active_day = ""
                    if self.session_id:
                        try:
                            from summary import topic_state_machine
                            _ts = await topic_state_machine.get_topic_status(self.session_id)
                            _start = _ts.get("start_time") if _ts else ""
                            if _start:
                                _active_day = str(_start)[:10]  # now_iso +08:00，前 10 位即北京日期
                        except Exception:
                            pass
                    if not _active_day:
                        # 无活跃话题：活跃日=今天（不回退昨天——昨天由 D历史组 yesterday 段提供）
                        _active_day = _dt.now().strftime("%Y-%m-%d")
                    raw = chronicle.get_messages_by_date(_active_day) or []
                    # 只滤 UI-only 消息 + 按 session_id 过滤（防跨会话混入，AI 推送写的是当前活跃 session）
                    _sid = self.session_id or ""
                    _day_msgs = [
                        r for r in raw
                        if not is_ui_only_role(r.get("role"))
                        and (not _sid or not r.get("session_id") or r.get("session_id") == _sid)
                    ]
                    if _day_msgs:
                        mems = []
                        for r in _day_msgs:
                            class _Msg:
                                pass
                            m = _Msg()
                            m.content = r.get('content', '')
                            # 解析 voice_attachment（用户语音元数据 JSON 串 → dict，供 （语音·tone） 标记）
                            _va_raw = r.get('voice_attachment')
                            _va = None
                            if _va_raw:
                                try:
                                    import json as _jv
                                    _va = _jv.loads(_va_raw) if isinstance(_va_raw, str) else _va_raw
                                except Exception:
                                    _va = None
                            m.metadata = {
                                'role': r.get('role', 'user'),
                                'timestamp': r.get('timestamp', ''),
                                'message_id': r.get('message_id', ''),
                                'is_wander': r.get('is_wander'),
                                'is_sentinel': r.get('is_sentinel'),
                                'is_reminder': r.get('is_reminder'),
                                # 只携带持久化的短工具摘要；原始调用结构和产图数据不进上下文。
                                'tool_summary': r.get('tool_summary'),
                                # 展示边界只从中提取名称、成败和有界结果，不注入原始结构。
                                'tool_calls': r.get('tool_calls'),
                                'voice_attachment': _va,
                            }
                            mems.append(m)
                    # 空态检测：今天是否收到过用户（user 角色）的消息（独立于活跃日——
                    # 跨午夜话题活跃日=昨天有消息时，今天仍可能没收到用户消息，也要回填提示）
                    try:
                        _today = _dt.now().strftime("%Y-%m-%d")
                        _today_msgs = chronicle.get_messages_by_date(_today) or []
                        _has_user_today = any(
                            r.get("role") == "user"
                            and (not _sid or not r.get("session_id") or r.get("session_id") == _sid)
                            for r in _today_msgs
                        )
                        if not _has_user_today:
                            self._conversation_empty_today = True
                    except Exception:
                        pass
                except Exception:
                    pass
            if not mems:
                # 活跃日无消息 → 空态
                self._conversation_empty_today = True
                return ""
            # 格式化为 formatted_messages 列表（role+content），与旧 build_context() 行为一致
            from context_builder.ingredients import format_relative_time
            result = []
            for m in mems:
                meta = getattr(m, 'metadata', {}) if hasattr(m, 'metadata') else {}
                if self._kwargs.get("exclude_wander_history") and meta.get("is_wander"):
                    continue
                role = meta.get('role', 'user')
                if is_ui_only_role(role):
                    continue  # divider/notification 仅供 UI，不进 LLM 上下文
                ts = meta.get('timestamp', '')
                content = str(getattr(m, 'content', '')) if hasattr(m, 'content') else str(m)
                # 脱掉旧格式的角色前缀（"用户: " / "assistant: " 等）
                if ": " in content:
                    content = content.split(": ", 1)[1]
                # 近期错误版本可能把内部说话人标签写进了助手正文；不让污染继续回灌。
                if role == "assistant":
                    import re as _re
                    content = _re.sub(r'^\s*[Kk]\s*[：:]\s*', '', content, count=1)
                    content = strip_internal_history_markers(content)
                time_label = format_relative_time(ts)
                is_wander = meta.get('is_wander')
                is_sentinel = meta.get('is_sentinel')
                is_reminder = meta.get('is_reminder')
                # 主聊天由 API role 区分说话人，正文不混入姓名标签；普通 assistant
                # 也不加时间，避免模型在回复中模仿时间/说话人前缀。
                if is_sentinel:
                    label = f"[🛡️ {time_label}] " if time_label else "[🛡️] "
                elif is_reminder:
                    label = f"[⏰ {time_label}] " if time_label else "[⏰] "
                elif is_wander:
                    label = f"[💭 {time_label}] " if time_label else "[💭] "
                elif role == "user":
                    label = f"[{time_label}] " if time_label else ""
                    # 用户语音消息：附加 （语音·tone） 元信息（对齐昨日对话格式，trap 229 语义同 [🛡️]/[💭] label）
                    _va = meta.get("voice_attachment")
                    if _va and isinstance(_va, dict):
                        _tone = _va.get("tone")
                        label += f"（语音·{_tone}）" if _tone else "（语音）"
                elif role == "assistant":
                    label = ""
                elif time_label:
                    label = f"[{time_label}] "
                else:
                    label = ""
                result.append({"role": role, "content": f"{label}{content}", "_ts": ts, "_section": "conversation_messages"})
                fact = build_tool_history_fact(role, meta.get("tool_summary"), meta.get("tool_calls"))
                if fact:
                    result.append({"role": "system", "content": fact, "_ts": ts, "_section": "tool_history_fact"})
            return result
        if name == "push_conversation_history":
            # 主动消息需要完整连续性，但此刻并没有一条新的用户消息等待回复。
            # 将同日对话标成历史记录，避免 Chat API 把最后一个 user turn当成本次生成对象。
            history = await self._call_ingredient(
                RecipeSection("对话历史", "conversation_messages")
            )
            if not isinstance(history, list) or not history:
                return []
            lines = ["以下是今天已经发生的对话记录："]
            from context_builder.proactive_output import format_push_history_line
            for message in history:
                content = str(message.get("content", "")).strip()
                if content:
                    lines.append(format_push_history_line(message.get("role", ""), content))
            if len(lines) == 1:
                return []
            return [{
                "role": "system",
                "content": "\n\n".join(lines),
                "_ts": "9999-12-31T23:59:40",
                "_section": "push_conversation_history",
            }]
        if name == "status_changes":
            # 返回独立 system 消息列表——_ts="9999" 排在对话历史之后（每轮可能新增，
            # 穿插在对话历史中间会打断缓存前缀，统一沉底；content 内已带 [时间] 标签保持内部顺序）
            raw = self._kwargs.get("raw_status_history", [])
            if raw:
                msgs = []
                for entry in raw:
                    t = entry.get("time", "")
                    stime = t[11:16] if "T" in t else t[:5]
                    msgs.append({
                        "role": "system",
                        "content": f"[{stime}] 用户的状态发生了变化：{entry.get('_text', '')}",
                        # 与 conversation message 共用真实时间戳，最终按时间轴穿插。
                        "_ts": t or "9999",
                        "_section": "status_changes",
                    })
                return msgs  # list → _add() → _extra_messages
            # 回退：旧格式 string
            return self._kwargs.get("status_changes", "")
        if name == "sentinel_timeline":
            # 返回独立 system 消息列表（追加在对话消息之后）
            annotations = self._kwargs.get("sentinel_timeline_annotations", None)
            if annotations is None:
                # Prefer the annotator snapshot so event IDs can be claimed
                # and committed after a successful provider response.
                try:
                    from silicon_perception.sentinel import get_sentinel
                    sentinel = get_sentinel()
                    annotations = sentinel.peek_timeline_annotations() if sentinel else []
                except Exception:
                    annotations = []
                if not annotations:
                    # Legacy fallback: text-only ingredient has no claimable
                    # IDs, but still remains safe and backward compatible.
                    fn = get_ingredient("sentinel_timeline")
                    if fn:
                        try:
                            import asyncio
                            text = await fn() if asyncio.iscoroutinefunction(fn) else fn()
                            if text and text.strip():
                                return [{"role": "system", "content": text, "_section": "sentinel_timeline"}]
                        except Exception:
                            pass
                    return ""
            if isinstance(annotations, list) and annotations:
                event_ids = [a.get("_event_id") for a in annotations if isinstance(a, dict) and a.get("_event_id") not in (None, "")]
                if event_ids:
                    self._one_shot_claims.append({
                        "kind": "sentinel_events",
                        "stable_id": str(event_ids[0]),
                        "ids": event_ids,
                    })
                return [{"role": "system", "content": a.get("content", str(a)), "_section": "sentinel_timeline"} for a in annotations]
            return ""
        if name == "time_awareness":
            # 返回独立 system 消息——_ts="9999" 排在对话历史之后（每轮变，穿插在前会打断缓存前缀）
            hint = self._kwargs.get("time_awareness_hint", "")
            if hint and hint.strip():
                return [{"role": "system", "content": hint.strip(), "_section": "time_awareness", "_ts": "9999"}]
            return ""

        # 通用 ingredient 调用
        fn = get_ingredient(name)
        if fn:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**self._build_fn_kwargs(name, params, fn))
                else:
                    result = fn(**self._build_fn_kwargs(name, params, fn))
                return result
            except Exception as e:
                logger.warning(f"Ingredient '{name}' failed: {e}")
        return None

    def _build_fn_kwargs(self, name: str, params: dict, fn=None) -> dict:
        """构建 ingredient 函数的有效参数——只传函数实际接受的参数。"""
        merged = dict(self._kwargs)
        merged.update(params)
        if fn is None:
            return merged
        try:
            import inspect
            sig = inspect.signature(fn)
            accepted = set()
            for p in sig.parameters.values():
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                    accepted.add(p.name)
                elif p.kind == p.VAR_KEYWORD:  # **kwargs → accept everything
                    return merged
            return {k: v for k, v in merged.items() if k in accepted}
        except Exception:
            return merged

    async def _resolve_push_diary(self) -> dict:
        """推送场景自取昨日背景（与 schedule_context.py:2377-2486 活跃话题覆盖检测逻辑对齐）。

        返回 {yesterday, yesterday_conversation, yesterday_diary_fallback,
              day_before, day_before_diary, gap_diaries_text}。
        活跃话题跨午夜（活跃日=昨天）时对话流已含活跃日消息，"昨天"回溯到话题开始日前一天，
        避免与对话流重复；无活跃话题时昨天=日历昨天。结果缓存（一天内不变）。
        """
        if self._push_diary_kwargs is not None:
            return self._push_diary_kwargs
        result = {
            "yesterday": "", "yesterday_conversation": "", "yesterday_diary_fallback": "",
            "yesterday_compact": "", "yesterday_compact_source_ids": [],
            "day_before": "", "day_before_diary": "", "gap_diaries_text": "",
        }
        try:
            from event_chronicle import get_global_chronicle, _read_yesterday_messages_formatted
            from datetime import datetime, timedelta
            chronicle = get_global_chronicle()
            now = datetime.now()
            last_active = chronicle.get_last_active_date()
            # 活跃话题覆盖检测（与 schedule_context 一致）
            if last_active and self.session_id:
                try:
                    from summary import topic_state_machine
                    from mirrow_core.time_utils import to_beijing_time
                    ts = await topic_state_machine.get_topic_status(self.session_id)
                    if ts.get("is_active") and ts.get("start_time"):
                        start_beijing = to_beijing_time(ts["start_time"])
                        topic_start_date = start_beijing[:10] if start_beijing else ""
                        if topic_start_date and last_active >= topic_start_date:
                            _dt = datetime.strptime(topic_start_date, "%Y-%m-%d")
                            last_active = (_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception:
                    pass
            yesterday = last_active if last_active else (now - timedelta(days=1)).strftime("%Y-%m-%d")
            result["yesterday"] = yesterday
            if yesterday:
                # Proactive pushes get a deterministic compact layer only
                # when the structured diary is complete.  FULL_CHAT and all
                # legacy/non-push callers keep the existing full raw history.
                compact = None
                if self._recipe_key in {"WANDER_PUSH", "SENTINEL_PUSH", "REMINDER_PUSH"}:
                    try:
                        from context_builder.proactive_history import build_compact_yesterday_context
                        compact = build_compact_yesterday_context(
                            chronicle,
                            yesterday,
                            context_query=self._kwargs.get("context_query", ""),
                            messages=chronicle.get_messages_by_date(yesterday) or [],
                        )
                    except Exception:
                        compact = None
                if compact and compact.get("text"):
                    result["yesterday_compact"] = compact["text"]
                    result["yesterday_compact_source_ids"] = compact.get("source_ids", [])
                else:
                    yc = _read_yesterday_messages_formatted(target_date=yesterday)
                    if yc:
                        result["yesterday_conversation"] = yc
                    else:
                        entry = chronicle.get_diary_entry_by_date(yesterday)
                        if entry:
                            result["yesterday_diary_fallback"] = entry.content
            # 前天日记
            day_before = chronicle.get_last_active_date(before_date=last_active) if last_active else None
            if not day_before:
                try:
                    day_before = (datetime.strptime(yesterday, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception:
                    day_before = None
            result["day_before"] = day_before
            if day_before:
                entry = chronicle.get_diary_entry_by_date(day_before)
                if entry:
                    result["day_before_diary"] = entry.content
            # gap 日记：昨天到日历昨天之间的空白日（用户没找 AI 的日子）
            try:
                calendar_yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                if yesterday and yesterday < calendar_yesterday:
                    gap_start = datetime.strptime(yesterday, "%Y-%m-%d") + timedelta(days=1)
                    gap_end = datetime.strptime(calendar_yesterday, "%Y-%m-%d")
                    cursor = gap_start
                    gap_parts = []
                    while cursor <= gap_end:
                        date_str = cursor.strftime("%Y-%m-%d")
                        entry = chronicle.get_diary_entry_by_date(date_str)
                        if entry and entry.content.strip():
                            gap_parts.append(f"{date_str}: {entry.content}")
                        cursor += timedelta(days=1)
                    if gap_parts:
                        result["gap_diaries_text"] = "\n".join(gap_parts)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"_resolve_push_diary 失败: {e}")
        self._push_diary_kwargs = result
        return result

    def _add(self, name: str, content):
        """添加一段内容到系统提示或额外消息列表。
        - str → 追加到 system_content
        - list[dict]（每个 dict 有 'role' 键）→ 追加到 extra_messages
        """
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "role" in item:
                    item.setdefault("_section", name)  # 标段名供 msg_order 反查
                    self._extra_messages.append(item)
            return
        if content and str(content).strip():
            self._sections[name] = str(content).strip()
            self._system_content += str(content).strip() + "\n\n"

    def _finalize(self) -> ContextResult:
        result = ContextResult()
        result.system_content = self._system_content.strip()
        result.dynamic_content = self._dynamic_content.strip()

        # 空态回填：推送场景今天无用户消息时，对话流前回填提示（主聊天不触发——用户正在发消息）
        if self._conversation_empty_today and self._recipe_key not in ("FULL_CHAT", "GROUP_CHAT_K"):
            self._extra_messages.append({
                "role": "system",
                "content": "（今天暂未收到用户的消息，你正按自己的感知主动处理。）",
                "_ts": "0",  # 排对话流最前
                "_section": "conversation_empty_state",
            })

        # 自感知也是背景状态，必须在统一排序前加入；不能在本次推送事件之后追加，
        # 否则会把模型最后的注意力从真实触发事实拉回泛化的缺失提示。
        _MISSING_MAP = {
            "memories": "与当前内容相关的长期记忆本轮没有浮现。",
            "yesterday": "昨天的对话或日记材料本轮没有取得。",
        }
        _missing_msgs = [
            text for sec_name, text in _MISSING_MAP.items()
            if self._section_status.get(sec_name, {}).get("status") == "empty"
        ]
        if _missing_msgs:
            _self_text = " ".join(_missing_msgs)
            self._extra_messages.append({
                "role": "system", "content": _self_text,
                "_section": "self_awareness", "_ts": "",
            })
            self._section_status["self_awareness"] = {"status": "ok"}
            self._sections["self_awareness"] = _self_text

        # 按时间戳排序 extra_messages（status_changes 与 conversation 混合后按时序排列）
        if self._extra_messages:
            self._extra_messages.sort(key=lambda m: m.get("_ts", "9999"))

        # 构建 formatted_messages（兼容现有接口）
        if result.system_content:
            result.formatted_messages.append({"role": "system", "content": result.system_content})
        result.formatted_messages.extend(self._extra_messages)

        # 监控台数据（含段级状态：ok/gated/empty/error/interleaved）
        result.sections = {}
        for sec_name, content in self._sections.items():
            entry = {"text": content, "tokens": estimate_tokens(content), "chars": len(content)}
            entry.update(self._section_status.get(sec_name, {}))
            result.sections[sec_name] = entry
        # list 段（穿插注入）不在 _sections 里有内容，但状态要可见
        for sec_name, status in self._section_status.items():
            if sec_name not in result.sections:
                entry = {"text": "", "tokens": 0, "chars": 0}
                entry.update(status)
                result.sections[sec_name] = entry
        result.stats = {
            "total_system_chars": len(result.system_content),
            "total_system_tokens": estimate_tokens(result.system_content),
            "total_dynamic_chars": len(result.dynamic_content),
            "total_dynamic_tokens": estimate_tokens(result.dynamic_content),
            "messages_count": len(result.formatted_messages),
            "section_count": len(self._sections),
            "section_names": list(self._sections.keys()),
        }

        # Prompt ledger: record the final builder ordering as measurements and
        # digests only.  The provider boundary may append the user turn or an
        # output contract; its usage callback associates the returned usage by
        # exact/prefix message shape and commits one-shot claims then.
        try:
            from prompt_ledger import register_prompt
            ledger_messages = []
            for index, message in enumerate(result.formatted_messages):
                item = dict(message)
                if index == 0 and item.get("role") == "system":
                    item["layer"] = "stable"
                elif item.get("_section") in {"conversation_messages", "push_conversation_history"}:
                    item["layer"] = "interleaved"
                elif item.get("_section"):
                    item["layer"] = "dynamic"
                ledger_messages.append(item)
            if result.dynamic_content:
                ledger_messages.append({
                    "role": "system", "content": result.dynamic_content, "layer": "dynamic",
                })
            result.call_id = register_prompt(
                ledger_messages,
                recipe=self._recipe_key or self._recipe_name,
                scenario=self._scenario_override or self._recipe_name,
                activity_id=self._kwargs.get("activity_id") or self._kwargs.get("current_activity_id", ""),
                session_id=self.session_id,
                one_shot_claims=self._one_shot_claims,
            )
            result.one_shot_claims = list(self._one_shot_claims)
            result.stats["prompt_call_id"] = result.call_id
        except Exception as exc:
            logger.debug(f"prompt ledger registration skipped: {exc}")

        # 记录 Recipe 调用（供 /monitor 前端面板展示）
        if self._recipe_name:
            try:
                from context_builder import record_recipe_call
                # 构建 AI 阅读顺序快照（formatted_messages 每条消息的摘要）
                # 注意：必须在 pop _section/_ts 之前构建，否则 interleaved 消息丢失段名
                msg_order = []
                for i, m in enumerate(result.formatted_messages):
                    role = m.get("role", "?")
                    content = m.get("content", "")
                    section = m.get("_section", "") or ("" if role != "system" else "system_content")
                    msg_order.append({
                        "idx": i + 1,
                        "role": role,
                        "section": section,
                        "preview": content,
                        "chars": len(content),
                    })
                record_recipe_call(
                    scenario=self._scenario_override or self._recipe_name,
                    recipe_name=self._recipe_name,
                    sections=result.sections,
                    total_chars=result.stats["total_system_chars"],
                    total_tokens=result.stats["total_system_tokens"],
                    extra={"session_id": self.session_id},
                    via_builder=True,
                    msg_order=msg_order,
                )
            except Exception:
                pass  # 记录失败不能阻断上下文返回

        # 去掉内部字段（_ts, _section 不给 LLM）
        # 放在 msg_order 构建之后，确保监控台能看到段名
        for m in self._extra_messages:
            m.pop("_ts", None)
            m.pop("_section", None)

        return result


def commit_context_result(result: ContextResult) -> bool:
    """Commit one-shot context material after the provider accepts a call.

    Kept as a tiny public hook for HTTP/WS callers that already know the
    builder result.  Provider usage instrumentation normally commits sooner;
    this remains idempotent and harmless when called a second time.
    """
    call_id = getattr(result, "call_id", "") if result is not None else ""
    if not call_id:
        return False
    try:
        from prompt_ledger import commit_prompt_call
        return commit_prompt_call(call_id)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
# 灰度开关
# ═══════════════════════════════════════════════════════════

def use_context_builder() -> bool:
    """灰度开关：环境变量 MIRROW_USE_CONTEXT_BUILDER=1 时启用 Builder。"""
    return os.environ.get("MIRROW_USE_CONTEXT_BUILDER") == "1"


def context_diff_enabled() -> bool:
    """输出对比模式：环境变量 MIRROW_CONTEXT_DIFF=1 时并行运行新旧对比。"""
    return os.environ.get("MIRROW_CONTEXT_DIFF") == "1"


async def maybe_diff_contexts(recipe_name: str, old_result, new_result, **kwargs):
    """并行运行新旧上下文构建，差异写入日志。

    比较两层：① system_content（系统提示）② formatted_messages（对话层——
    穿插消息/角色前缀/时间感知位置等漂移只在这层可见）。
    """
    if not context_diff_enabled():
        return

    try:
        # 旧 ContextResult（context_scheduler）用 formatted_context，新 Builder 用 system_content
        old_content = (
            getattr(old_result, 'system_content', None) or
            getattr(old_result, 'formatted_context', None) or
            str(old_result)
        )
        new_content = new_result.system_content

        if old_content.strip() != new_content.strip():
            logger.warning(
                f"[CONTEXT_DIFF] Recipe={recipe_name} | "
                f"old_chars={len(old_content)} new_chars={len(new_content)} | "
                f"old_preview={old_content[:200]}... | "
                f"new_preview={new_content[:200]}..."
            )
        else:
            logger.info(f"[CONTEXT_DIFF] Recipe={recipe_name} MATCH ({len(old_content)} chars)")

        # ── 对话层比较（system 之外的 formatted_messages）──
        old_msgs = getattr(old_result, 'formatted_messages', None)
        new_msgs = getattr(new_result, 'formatted_messages', None)
        if old_msgs is not None and new_msgs is not None:
            # 跳过首条 system（上面已比较），只比后续消息
            old_tail = old_msgs[1:] if (old_msgs and old_msgs[0].get("role") == "system") else list(old_msgs)
            new_tail = new_msgs[1:] if (new_msgs and new_msgs[0].get("role") == "system") else list(new_msgs)
            old_sig = [(m.get("role", ""), (m.get("content") or "")[:50]) for m in old_tail]
            new_sig = [(m.get("role", ""), (m.get("content") or "")[:50]) for m in new_tail]
            if old_sig != new_sig:
                # 找出首个差异点
                first_diff = ""
                for i in range(max(len(old_sig), len(new_sig))):
                    o = old_sig[i] if i < len(old_sig) else ("<缺失>", "")
                    n = new_sig[i] if i < len(new_sig) else ("<缺失>", "")
                    if o != n:
                        first_diff = f"idx={i} old={o} new={n}"
                        break
                logger.warning(
                    f"[CONTEXT_DIFF] Recipe={recipe_name} MSGS_DIFF | "
                    f"old_count={len(old_tail)} new_count={len(new_tail)} | {first_diff}"
                )
            else:
                logger.info(f"[CONTEXT_DIFF] Recipe={recipe_name} MSGS_MATCH ({len(new_tail)} msgs)")
    except Exception as e:
        logger.warning(f"[CONTEXT_DIFF] comparison failed: {e}")
