"""Auditable plan-decision boundary for the Wander runtime migration.

This module only turns one LLM decision into a validated plan.  It never
creates activities or starts handlers; ``runtime_controller`` owns that state
transition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .event_catalog import EVENT_CATALOG, completed_counts_text, render_event_options as _event_list_text
from .event_types import EventType
from .goal_policy import normalize_goal_policy
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
    # Explicit meanings; ``away_duration`` remains a compatibility input for
    # older callers but is not relabelled when these fields are populated.
    status_duration: str = ""
    private_message_idle: str = ""
    physical_idle: str = ""
    today_wander: str = ""
    # Factual timeline of what really happened today (activities + inbound
    # visits).  It includes exhausted/absent facts such as "no outbound visit
    # yet", because absence cannot be inferred from a missing option.
    today_facts: str = ""
    inclination_note: str = ""
    # display_name → 当前活跃日已完成次数；不是排序权重。
    today_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivitySpec:
    event_type: EventType
    goal_mode: GoalMode
    goal_value: Optional[int]
    reason: str = ""
    completion: str = "fixed"
    xhs_task: Optional[dict] = None


@dataclass(frozen=True)
class PlanDecision:
    activities: tuple[ActivitySpec, ...]
    horizon_min: int
    raw_plan: dict[str, Any]
    horizon_mode: str = "deadline"
    autonomous_goals: bool = False
    action: str = "act"
    defer_seconds: Optional[float] = None
    defer_reason: str = ""


def _event_from_value(value: Any) -> Optional[EventType]:
    if isinstance(value, EventType):
        return value
    candidate = str(value or "").strip()
    for event_type, strategy in EVENT_CATALOG.items():
        if candidate in (event_type.value, event_type.name, strategy.display_name):
            return event_type
    return None


_PLAN_TEMPERATURE_ENV = "WANDER_PLAN_TEMPERATURE"
_DEFAULT_PLAN_TEMPERATURE = 0.7


def plan_temperature() -> float:
    """Planning temperature.

    Temperature 0 made "free choice" a deterministic argmax over a context
    dominated by the day's own history, so the same two activities kept
    winning.  A moderate temperature keeps the choice auditable while letting
    AI actually pick differently across near-identical moments.
    """
    raw = str(os.getenv(_PLAN_TEMPERATURE_ENV, "") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_PLAN_TEMPERATURE
    except ValueError:
        value = _DEFAULT_PLAN_TEMPERATURE
    return max(0.0, min(value, 1.5))


def build_runtime_block(
    context: RuntimeContext,
    allowed_event_types: Optional[frozenset[EventType]] = None,
    last_seen=None,
) -> str:
    now = context.current_time or datetime.now().strftime("%Y/%m/%d %H:%M")
    return "\n".join((
        f"[当前时间]\n{now}",
        f"[人类伙伴状态]\n{context.user_status or '未知'}",
        *([f"[状态持续时间]\n{context.status_duration}"] if context.status_duration else []),
        *([f"[距最后私聊]\n{context.private_message_idle}"] if context.private_message_idle else []),
        *([f"[设备本地物理空闲]\n{context.physical_idle}"] if context.physical_idle else []),
        *([f"[离开时长]\n{context.away_duration}"] if not any((context.status_duration, context.private_message_idle, context.physical_idle)) and context.away_duration else []),
        f"[今日漫想]\n{completed_counts_text(context.today_counts) or context.today_wander or '暂无'}",
        *([f"[今天真实发生的事]\n{context.today_facts}"] if context.today_facts else []),
        f"[可做事件列表]\n{_event_list_text(allowed_event_types, last_seen=last_seen)}",
        f"[上次没接上的念头]\n{context.inclination_note or '无'}",
    ))


_OUTPUT_INSTRUCTION = """结合自己的兴趣、心绪、已有约定与真实新进展，选择此刻值得经历的活动。可做列表描述各活动的真实能力，经历时间线与当前状态各保留其来源和时效。
根据上述真实状态，仅输出 JSON 对象：
{"inclination_text":"一句话说出此刻自己想做什么","horizon_min":预计分钟数,"horizon_mode":"estimate|deadline","activities":[{"event_type":"事件类型","goal":{"mode":"count|duration|open_ended|single|external_signal","value":整数或null,"completion":"estimate|fixed"},"reason":"简短原因"}]}
estimate 表示到这个时间只做一次是否继续的复核，不会直接截断活动；deadline 只用于你明确希望到点结束的计划。
count/open_ended 的 completion 默认为 estimate：value 是先做多少的预计目标，达到后由你在节点复核中决定继续多少或结束；只有你明确希望到点/到数即结束才填 fixed。duration、single、external_signal 的完成条件是既有真实合同，必须填 fixed。
时间线里的“已经得到／仍想接续”，是那几轮当时觉得的；后来的心情可能又变了。心情可以留下，事情也可以已经做够。
inclination_text 表达此刻想做什么；reason 简述这次想经历什么、怎样算做够。可以接着探索，也可以因喜欢而重温，或换件事；结合现在的兴趣、对话与生活节奏自行选择。
选择浏览小红书时，在 reason 里带上阅读入口和具体目标：首页推荐、话题、已知账号名、自身主页，或已有任务编号；想读某人的帖子时保留能定位这个人的已有线索。
休息也是可选活动，时长自行决定。
初始选择此刻想先做的 1 项，过程中可以转做别的事。event_type 取可做事件列表；这里是在规划，真实执行随后发生。"""


class PlanDecisionAdapter:
    def __init__(self, store: WanderRuntimeStore,
                 llm_caller: Callable[..., Awaitable[FlashJsonResult]] = call_flash_json_detailed,
                 context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
                 allowed_event_types: Optional[set[EventType]] = None,
                 max_activities: int = 2,
                 availability_checks: Optional[dict[EventType, Callable[[], bool]]] = None):
        self.store = store
        self.llm_caller = llm_caller
        self.context_builder = context_builder
        self.allowed_event_types = (
            frozenset(allowed_event_types) if allowed_event_types is not None else frozenset(EVENT_CATALOG)
        )
        if not self.allowed_event_types:
            raise ValueError("at least one event type must be enabled")
        self.max_activities = max(1, min(int(max_activities), 2))
        self.availability_checks = dict(availability_checks or {})

    def _last_seen_facts(self) -> dict[str, str]:
        """Read-only last-completion facts; absence never blocks planning."""
        if self.store is None:
            return {}
        try:
            return self.store.completed_last_seen_by_type()
        except Exception:
            return {}

    def effective_allowed_event_types(self) -> frozenset[EventType]:
        """Shared admission for initial planning and review-time successors.

        Real capability decisions belong to the backend gate in
        ``admission.py``; the planner only consumes its answer.
        """
        from .admission import filter_allowed

        return filter_allowed(self.allowed_event_types, self.availability_checks)

    async def decide(self, run: WanderRun, context: RuntimeContext) -> Optional[PlanDecision]:
        allowed_event_types = self.effective_allowed_event_types()
        last_seen = self._last_seen_facts()
        runtime_block = build_runtime_block(context, allowed_event_types, last_seen=last_seen)
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
        # Keep the historical column for old runs, but v3 has no probability
        # selection signal. AI's inclination comes from this plan decision.
        run.probability_snapshot = {}
        run.inclination_text = ""
        run.context_snapshot = {"runtime_block": runtime_block}
        self.store.save_run(run)
        try:
            built = await self._build_recipe(runtime_block, context)
        except Exception as exc:
            self._record_context_error(run, runtime_block, f"recipe_build:{type(exc).__name__}")
            return None
        from context_builder.assembly import assemble_context_text, assemble_context_result
        from context_builder.channel_projection import backend_turn
        system_content = assemble_context_text(built)
        sections = getattr(built, "sections", {}) or {}
        runtime_section = sections.get("wander_runtime", {})
        if not system_content or not runtime_section.get("text") or not system_content.rstrip().endswith(runtime_block.rstrip()):
            self._record_context_error(run, runtime_block, "runtime_block_not_final_or_missing")
            return None
        messages = assemble_context_result(built) + [backend_turn(_OUTPUT_INSTRUCTION)]
        try:
            detailed = await self.llm_caller(messages=messages, temperature=plan_temperature(), max_tokens=16384,
                route_label="wander_plan",
                result_validator=lambda item: None if self._normalize(item.parsed,
                    allowed_event_types=allowed_event_types, max_activities=self.max_activities) is not None
                    else "no_valid_activity_in_plan")
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
        if normalized is not None:
            run.inclination_text = str(raw_parsed.get("inclination_text") or "").strip()[:300]
            self.store.save_run(run)
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
        from .planning_context import material_and_query
        material, context_query = await material_and_query(context.session_id,
            str(context.inclination_note or '').strip())
        if self.context_builder is not None:
            return await self.context_builder("WANDER_V2", wander_runtime_text=runtime_block,
                persona=context.persona, session_id=context.session_id, context_query=context_query,
                active_day_scene_material=material)
        from context_builder.builder import ContextBuilder
        return await ContextBuilder.build("WANDER_V2", wander_runtime_text=runtime_block,
            persona=context.persona, session_id=context.session_id, context_query=context_query,
            active_day_scene_material=material)

    def _record_context_error(self, run: WanderRun, runtime_block: str, error: str) -> None:
        self.store.record_decision(run_id=run.run_id, phase=DecisionPhase.PLAN, model="", recipe="WANDER_V2",
            input_context={"runtime": runtime_block}, parsed_output={"raw": {}, "normalized": {}},
            status="context_error", error=error)

    @staticmethod
    def _decision_dict(decision: Optional[PlanDecision]) -> dict[str, Any]:
        if decision is None:
            return {}
        return {"action": decision.action, "defer_seconds": decision.defer_seconds,
                "defer_reason": decision.defer_reason,
                "horizon_min": decision.horizon_min, "horizon_mode": decision.horizon_mode, "autonomous_goals": decision.autonomous_goals, "activities": [
            {"event_type": item.event_type.value,
             "goal": {"mode": item.goal_mode.value, "value": item.goal_value, "completion": item.completion}, "reason": item.reason}
            for item in decision.activities
        ]}

    @staticmethod
    def _normalize(
        payload: dict[str, Any],
        allowed_event_types: Optional[frozenset[EventType]] = None,
        max_activities: int = 2,
    ) -> Optional[PlanDecision]:
        if not isinstance(payload, dict):
            return None
        action = payload.get('action', 'act')
        if action == 'defer':
            # 旧输出的兼容：等一会儿就是一次短休息，不再保留独立的 defer 状态。
            from .timing_policy import deferred_wait
            waiting = deferred_wait(payload)
            if waiting is None:
                return None
            delay, reason = waiting
            minutes = min(480, max(5, int((delay + 59) // 60)))
            return PlanDecision(
                (ActivitySpec(EventType.SLEEP, GoalMode.DURATION, minutes, reason, 'fixed'),),
                30, payload, 'estimate', False,
            )
        if action != 'act':
            return None
        try:
            candidate = payload.get("horizon_min", 30)
            horizon = int(candidate)
            if isinstance(candidate, bool) or horizon <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            horizon = 30
        # ``timedelta`` must remain representable by the trusted local clock;
        # rejecting absurd model numbers is not an artificial autonomy cap.
        try:
            datetime.now() + timedelta(minutes=horizon)
        except (OverflowError, ValueError):
            horizon = 30
        horizon_mode = str(payload.get("horizon_mode") or "estimate").strip().lower()
        if horizon_mode not in {"estimate", "deadline"}:
            horizon_mode = "estimate"
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
            goal_mode, value, completion = normalize_goal_policy(event_type, goal, autonomous=True)
            xhs_task = None
            if event_type == EventType.BROWSE_XIAOHONGSHU and raw.get('xhs_task') is not None:
                from .xhs_request import validate_task
                try:
                    xhs_task = validate_task(raw['xhs_task'])
                except ValueError:
                    pass
            activities.append(ActivitySpec(event_type, goal_mode, value, str(raw.get("reason") or ""), completion,
                                           xhs_task=xhs_task))
            seen_events.add(event_type)
        return PlanDecision(tuple(activities), horizon, payload, horizon_mode, autonomous_goals=True) if activities else None


def _usage_value(usage: dict[str, Any], *keys: str) -> Optional[int]:
    from llm_usage import extract_token_usage
    values = extract_token_usage(usage)
    fields = {'prompt_tokens': 'prompt', 'completion_tokens': 'completion',
              'cached_tokens': 'cache_hit', 'cache_tokens': 'cache_hit'}
    for key in keys:
        if key in fields:
            return values[fields[key]]
        try:
            value = usage.get(key)
            return int(value) if value is not None else None
        except (AttributeError, TypeError, ValueError):
            continue
    return None
