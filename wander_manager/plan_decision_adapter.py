"""Auditable plan-decision boundary for the Wander runtime migration.

This module only turns one LLM decision into a validated plan.  It never
creates activities or starts handlers; ``runtime_controller`` owns that state
transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .event_catalog import EVENT_CATALOG, normalize_goal
from .event_types import EventType
from .flash_structured import FlashJsonResult, call_flash_json_detailed
from .runtime_models import DecisionPhase, GoalMode, WanderRun
from .runtime_store import WanderRuntimeStore


@dataclass
class RuntimeContext:
    persona: str
    session_id: str = ""
    mood: str = ""
    current_time: str = ""
    user_status: str = ""
    away_duration: str = ""
    # Explicit meusers; ``away_duration`` remains a compatibility input for
    # older callers but is not relabelled when these fields are populated.
    status_duration: str = ""
    private_message_idle: str = ""
    physical_idle: str = ""
    today_conversation: str = ""
    today_wander: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    inclination_note: str = ""
    # display_name → 今日已完成次数，供可做事件列表排序与计数标注（0 未选过）
    today_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivitySpec:
    event_type: EventType
    goal_mode: GoalMode
    goal_value: Optional[int]
    reason: str = ""


@dataclass(frozen=True)
class PlanDecision:
    activities: tuple[ActivitySpec, ...]
    horizon_min: int
    raw_plan: dict[str, Any]


def _event_from_value(value: Any) -> Optional[EventType]:
    if isinstance(value, EventType):
        return value
    candidate = str(value or "").strip()
    for event_type, strategy in EVENT_CATALOG.items():
        if candidate in (event_type.value, event_type.name, strategy.display_name):
            return event_type
    return None


def _ranked_probabilities(probabilities: dict[str, float]) -> list[tuple[EventType, float]]:
    merged: dict[EventType, float] = {}
    for raw_key, raw_value in probabilities.items():
        event_type = _event_from_value(raw_key)
        if event_type is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        merged[event_type] = max(merged.get(event_type, float("-inf")), value)
    return sorted(merged.items(), key=lambda item: (-item[1], item[0].value))


def build_inclination_text(probabilities: dict[str, float], supplement: str = "") -> str:
    """Render the agreed tendency language with fixed, testable boundaries."""
    chosen = [(EVENT_CATALOG[event].display_name, value) for event, value in _ranked_probabilities(probabilities)[:3] if value > 0.20]
    names = [name for name, _ in chosen]
    values = [value for _, value in chosen]
    if not names:
        text = "没什么特别想做的。"
    elif len(names) == 1:
        text = f"想做{names[0]}，没什么其他特别想做的。"
    elif len(names) == 2:
        if values[0] - values[1] < 0.05:
            text = f"有点想做{names[0]}、{names[1]}。"
        else:
            text = f"很想做{names[0]}，{names[1]}也不是不行。"
    else:
        d12, d23, d13 = values[0] - values[1], values[1] - values[2], values[0] - values[2]
        if d13 < 0.05:
            text = f"{names[0]}、{names[1]}、{names[2]}都有点想做。"
        elif d12 < 0.05 and 0.05 <= d23 <= 0.10:
            text = f"有点想做{names[0]}、{names[1]}，实在不行{names[2]}也行。"
        elif d12 < 0.05 and d23 > 0.10:
            text = f"有点想做{names[0]}、{names[1]}。"
        elif d23 < 0.05 and d12 > 0.05:
            text = f"很想做{names[0]}，{names[1]}和{names[2]}也不是不行。"
        elif d12 > 0.10 and d23 > 0.10:
            text = f"想做{names[0]}，没什么其他特别想做的。"
        else:
            text = f"想做{names[0]}，{names[1]}和{names[2]}也可以。"
    return f"{text}\n补充：{supplement.strip()}" if supplement.strip() else text


def _event_list_text(
    allowed_event_types: Optional[frozenset[EventType]] = None,
    today_counts: Optional[dict[str, int]] = None,
) -> str:
    """按今日选用次数升序渲染可做事件列表：没选过(0)排前，选过的带计数排后。

    today_counts 以 display_name 为键（与 EVENT_CATALOG 的 display_name 对齐）。
    """
    today_counts = today_counts or {}
    items = [
        (today_counts.get(item.display_name, 0), item)
        for item in EVENT_CATALOG.values()
        if allowed_event_types is None or item.event_type in allowed_event_types
    ]
    items.sort(key=lambda pair: (pair[0], pair[1].event_type.value))
    parts = []
    for count, item in items:
        text = (
            f"{item.event_type.value}（{item.display_name}，"
            f"目标模式：{'/'.join(mode.value for mode in item.allowed_goal_modes)}"
        )
        if count > 0:
            text += f"，今天已进行 {count} 次"
        text += "）"
        parts.append(text)
    return "；".join(parts)


def build_runtime_block(
    context: RuntimeContext,
    allowed_event_types: Optional[frozenset[EventType]] = None,
) -> str:
    now = context.current_time or datetime.now().strftime("%Y/%m/%d %H:%M")
    return "\n".join((
        f"[当前情绪]\n{context.mood or '暂无显著变化'}",
        f"[当前时间]\n{now}",
        f"[用户状态]\n{context.user_status or '未知'}",
        *([f"[状态持续时间]\n{context.status_duration}"] if context.status_duration else []),
        *([f"[距最后私聊]\n{context.private_message_idle}"] if context.private_message_idle else []),
        *([f"[设备本地物理空闲]\n{context.physical_idle}"] if context.physical_idle else []),
        *([f"[离开时长]\n{context.away_duration}"] if not any((context.status_duration, context.private_message_idle, context.physical_idle)) and context.away_duration else []),
        f"[今日对话]\n{context.today_conversation or '暂无'}",
        f"[今日漫想]\n{context.today_wander or '暂无'}",
        f"[可做事件列表]\n{_event_list_text(allowed_event_types, context.today_counts)}",
        f"[漫想倾向]\n上方可做事件列表中的所有事件都可以——凭此刻内心自由选择，选当下最想做/最能触动你的。",
        f"[倾向补充]\n{context.inclination_note or '无'}",
    ))


_OUTPUT_INSTRUCTION = """根据上述真实状态，仅输出 JSON 对象：
{"horizon_min":10-120整数,"activities":[{"event_type":"事件类型","goal":{"mode":"count|duration|open_ended|single|external_signal","value":整数或null},"reason":"简短原因"}]}
初始只选择此刻真正想先做的 1 项；不要预先把候选活动排满。若做的过程中自然想转去另一件事，节点复核阶段仍可现场续作。event_type 必须来自可做事件列表。你的选择来自你的内心状态，用户状态仅作背景参考，不要只围绕用户。不要声称已执行任何事情。"""


class PlanDecisionAdapter:
    def __init__(self, store: WanderRuntimeStore,
                 llm_caller: Callable[..., Awaitable[FlashJsonResult]] = call_flash_json_detailed,
                 context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
                 allowed_event_types: Optional[set[EventType]] = None,
                 max_activities: int = 2):
        self.store = store
        self.llm_caller = llm_caller
        self.context_builder = context_builder
        self.allowed_event_types = (
            frozenset(allowed_event_types) if allowed_event_types is not None else frozenset(EVENT_CATALOG)
        )
        if not self.allowed_event_types:
            raise ValueError("at least one event type must be enabled")
        self.max_activities = max(1, min(int(max_activities), 2))

    def effective_allowed_event_types(self) -> frozenset[EventType]:
        """Shared admission for initial planning and review-time successors."""
        allowed = set(self.allowed_event_types)
        from .host_hooks import event_available
        for event in (EventType.VISIT_LOUNGE, EventType.BROWSE_TAOBAO, EventType.BROWSE_SOCIAL_FEED,
                      EventType.HOST_GROUP_ACTIVITY, EventType.BROWSE_BOOKMARKS):
            if event in allowed and (not event_available(event) or self.store.has_recent_failed_activity(event.value, minutes=15)):
                allowed.discard(event)
        if EventType.LISTEN_MUSIC in allowed:
            try:
                if self.store.has_recent_failed_activity("listen_music", minutes=15):
                    allowed.discard(EventType.LISTEN_MUSIC)
            except Exception:
                # Planning must remain available if an old/minimal test store
                # does not expose the optional cooldown query.
                pass
        return frozenset(allowed)

    async def decide(self, run: WanderRun, context: RuntimeContext) -> Optional[PlanDecision]:
        allowed_event_types = self.effective_allowed_event_types()
        runtime_block = build_runtime_block(context, allowed_event_types)
        if not context.persona.strip():
            self.store.save_run(run)
            self._record_context_error(run, runtime_block, "persona_is_required")
            return None
        if context.session_id and run.session_id and context.session_id != run.session_id:
            self.store.save_run(run)
            self._record_context_error(run, runtime_block, "session_id_mismatch")
            return None
        if not run.session_id:
            run.session_id = context.session_id
        run.probability_snapshot = dict(context.probabilities)
        enabled_probabilities = {
            key: value
            for key, value in context.probabilities.items()
            if _event_from_value(key) in allowed_event_types
        }
        run.inclination_text = build_inclination_text(enabled_probabilities)
        run.context_snapshot = {"runtime_block": runtime_block}
        self.store.save_run(run)
        try:
            built = await self._build_recipe(runtime_block, context)
        except Exception as exc:
            self._record_context_error(run, runtime_block, f"recipe_build:{type(exc).__name__}")
            return None
        system_content = getattr(built, "system_content", "")
        sections = getattr(built, "sections", {}) or {}
        runtime_section = sections.get("wander_runtime", {})
        if not system_content or not runtime_section.get("text") or not system_content.rstrip().endswith(runtime_block.rstrip()):
            self._record_context_error(run, runtime_block, "runtime_block_not_final_or_missing")
            return None
        messages = [{"role": "system", "content": system_content}, {"role": "user", "content": _OUTPUT_INSTRUCTION}]
        try:
            detailed = await self.llm_caller(messages=messages, temperature=0.0, max_tokens=16384)
        except Exception as exc:
            self.store.record_decision(run_id=run.run_id, phase=DecisionPhase.PLAN, model="", recipe="WANDER_V2",
                input_context={"system": system_content, "runtime": runtime_block, "user": _OUTPUT_INSTRUCTION},
                parsed_output={"raw": {}, "normalized": {}}, status="llm_error", error=type(exc).__name__)
            return None
        raw_parsed = detailed.parsed if detailed.status == "ok" else None
        normalized = self._normalize(
            raw_parsed,
            allowed_event_types=allowed_event_types,
            max_activities=self.max_activities,
        ) if raw_parsed is not None else None
        status, error = detailed.status, detailed.error
        if raw_parsed is not None and normalized is None:
            status, error = "validation_error", "no_valid_activity_in_plan"
        self.store.record_decision(
            run_id=run.run_id, phase=DecisionPhase.PLAN, model=detailed.model, recipe="WANDER_V2",
            temperature=detailed.temperature, input_context={"system": system_content, "runtime": runtime_block, "user": _OUTPUT_INSTRUCTION},
            raw_output=detailed.raw_content, reasoning=detailed.reasoning,
            parsed_output={"raw": raw_parsed or {}, "normalized": self._decision_dict(normalized)},
            status=status, error=error, duration_ms=detailed.duration_ms,
            prompt_tokens=_usage_value(detailed.usage, "prompt_tokens"),
            completion_tokens=_usage_value(detailed.usage, "completion_tokens"),
            cache_tokens=_usage_value(detailed.usage, "cached_tokens", "cache_tokens"),
        )
        return normalized

    async def _build_recipe(self, runtime_block: str, context: RuntimeContext) -> Any:
        if self.context_builder is not None:
            return await self.context_builder("WANDER_V2", wander_runtime_text=runtime_block,
                persona=context.persona, session_id=context.session_id)
        from context_builder.builder import ContextBuilder
        return await ContextBuilder.build("WANDER_V2", wander_runtime_text=runtime_block,
            persona=context.persona, session_id=context.session_id)

    def _record_context_error(self, run: WanderRun, runtime_block: str, error: str) -> None:
        self.store.record_decision(run_id=run.run_id, phase=DecisionPhase.PLAN, model="", recipe="WANDER_V2",
            input_context={"runtime": runtime_block}, parsed_output={"raw": {}, "normalized": {}},
            status="context_error", error=error)

    @staticmethod
    def _decision_dict(decision: Optional[PlanDecision]) -> dict[str, Any]:
        if decision is None:
            return {}
        return {"horizon_min": decision.horizon_min, "activities": [
            {"event_type": item.event_type.value,
             "goal": {"mode": item.goal_mode.value, "value": item.goal_value}, "reason": item.reason}
            for item in decision.activities
        ]}

    @staticmethod
    def _normalize(
        payload: dict[str, Any],
        allowed_event_types: Optional[frozenset[EventType]] = None,
        max_activities: int = 2,
    ) -> Optional[PlanDecision]:
        try:
            horizon = int(payload.get("horizon_min", 30))
        except (TypeError, ValueError):
            horizon = 30
        horizon = max(10, min(horizon, 120))
        raw_activities = payload.get("activities")
        if not isinstance(raw_activities, list):
            return None
        activities: list[ActivitySpec] = []
        seen_events: set[EventType] = set()
        allowed = allowed_event_types if allowed_event_types is not None else frozenset(EVENT_CATALOG)
        for raw in raw_activities[:max(1, min(int(max_activities), 2))]:
            if not isinstance(raw, dict):
                continue
            event_type = _event_from_value(raw.get("event_type"))
            if event_type is None or event_type not in allowed or event_type in seen_events:
                continue
            goal = raw.get("goal") if isinstance(raw.get("goal"), dict) else {}
            mode, value = normalize_goal(event_type, goal.get("mode"), goal.get("value"))
            activities.append(ActivitySpec(event_type, mode, value, str(raw.get("reason") or "")))
            seen_events.add(event_type)
        return PlanDecision(tuple(activities), horizon, payload) if activities else None


def _usage_value(usage: dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        try:
            value = usage.get(key)
            return int(value) if value is not None else None
        except (AttributeError, TypeError, ValueError):
            continue
    return None
