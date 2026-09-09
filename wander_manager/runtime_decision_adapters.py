"""Auditable node-review and settlement decisions for the Wander runtime.

These adapters interpret persisted facts. They never advance the controller,
send a message, or update the emotion system.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .event_catalog import strategy_for
from .event_types import EventType
from .flash_structured import FlashJsonResult, call_flash_json_detailed
from .runtime_models import ActivityState, DecisionPhase, NodeState, SettlementReason
from .runtime_store import WanderRuntimeStore, redact_sensitive
from .timing_policy import normalize_delay_seconds


@dataclass
class DecisionContext:
    persona: str
    session_id: str
    mood: str = ""
    current_time: str = ""
    user_status: str = ""
    away_duration: str = ""
    # Explicitly distinguish status duration, private-message silence and
    # device-local physical idle.  ``away_duration`` remains for old callers.
    status_duration: str = ""
    private_message_idle: str = ""
    physical_idle: str = ""
    today_conversation: str = ""
    today_wander: str = ""


@dataclass
class EmotionEffect:
    changed: bool = False
    from_emotion: str = ""
    to_emotion: str = ""
    delta: str = ""
    confidence: float = 0.0


@dataclass
class NodeReviewDecision:
    reflection: str = "没有感想"
    emotion_effect: EmotionEffect = field(default_factory=EmotionEffect)
    continue_activity: bool = True
    abort_reason: str = ""
    switch_to: Optional[tuple[EventType, str]] = None
    next_node_delay_seconds: Optional[float] = None
    timing_reason: str = ""
    timing_source: str = "fallback"
    next_node_at: str = ""


@dataclass
class SettlementDecision:
    summary: str = ""
    share: bool = False
    continue_next: bool = False
    next_inclination_note: str = ""
    emotion_effect: EmotionEffect = field(default_factory=EmotionEffect)
    next_run_delay_seconds: Optional[float] = None
    timing_reason: str = ""
    timing_source: str = "fallback"
    next_plan_at: str = ""


class DecisionContextError(ValueError):
    pass


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是"}:
        return True
    if text in {"false", "0", "no", "n", "否", ""}:
        return False
    return default


def _normalize_effect(raw: Any) -> EmotionEffect:
    if not isinstance(raw, dict) or not _as_bool(raw.get("changed")):
        return EmotionEffect()
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return EmotionEffect(
        changed=True,
        from_emotion=str(raw.get("from", raw.get("from_emotion", "")) or "").strip(),
        to_emotion=str(raw.get("to", raw.get("to_emotion", "")) or "").strip(),
        delta=str(raw.get("delta", "") or "").strip(),
        confidence=confidence,
    )


def _safe_excerpt(value: Any, limit: int) -> str:
    redacted = redact_sensitive(value)
    if isinstance(redacted, str):
        text = redacted
    else:
        text = json.dumps(redacted, ensure_ascii=False, default=str)
    return text[:limit]


def _completed_progress(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for node in nodes:
        if node.get("state") != NodeState.COMPLETED.value:
            continue
        reflection = node.get("reflection") or "没有感想"
        lines.append(
            f"- 第{node['round_index']}节点：{node.get('source_summary') or '无摘要'}；感想：{reflection}"
        )
    return "\n".join(lines[-8:]) or "尚无已完成节点"


def _accumulated_emotion(nodes: list[dict[str, Any]]) -> str:
    effects = [node.get("emotion_effect") for node in nodes if node.get("emotion_effect")]
    return _safe_excerpt(effects, 1500) if effects else "暂无情绪变化候选"


def _runtime_block(
    context: DecisionContext,
    activity: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    phase: str,
    current_node: Optional[dict[str, Any]] = None,
    settlement_reason: str = "",
    prior_activities: Optional[list[dict[str, Any]]] = None,
    run: Optional[dict[str, Any]] = None,
) -> str:
    event_type = EventType(activity["activity_type"])
    strategy = strategy_for(event_type)
    now = context.current_time or datetime.now().strftime("%Y/%m/%d %H:%M")
    contract = (
        f"event_type={event_type.value}；名称={strategy.display_name}；"
        f"目标={activity['goal_mode']}:{activity.get('goal_value')}；"
        f"timer_ends_at={activity.get('timer_ends_at') or '无'}；"
        f"next_node_at={activity.get('next_node_at') or '无'}；activity_started_at={activity.get('created_at') or '无'}"
    )
    parts = [
        f"[本次调用阶段]\n{phase}",
        f"[当前时间]\n{now}",
        f"[用户状态]\n{context.user_status or '未知'}",
        *([f"[状态持续时间]\n{context.status_duration}"] if context.status_duration else []),
        *([f"[距最后私聊]\n{context.private_message_idle}"] if context.private_message_idle else []),
        *([f"[设备本地物理空闲]\n{context.physical_idle}"] if context.physical_idle else []),
        *([f"[离开时长]\n{context.away_duration}"] if not any((context.status_duration, context.private_message_idle, context.physical_idle)) and context.away_duration else []),
        f"[今日对话]\n{context.today_conversation or '暂无'}",
        f"[今日漫想]\n{context.today_wander or '暂无'}",
        f"[本事件段先前活动]\n{_prior_activity_progress(prior_activities or [])}",
        f"[当前活动合同]\n{contract}",
        f"[活动时间事实]\ncreated_at={activity.get('created_at') or '未知'}；next_node_at={activity.get('next_node_at') or '无'}",
        f"[已完成进度]\n{_completed_progress(nodes)}",
    ]
    if run:
        timing = (run.get("context_snapshot") or {}).get("timing") or {}
        parts.append(f"[计划时间事实]\nhorizon_mode={timing.get('horizon_mode', 'deadline')}；horizon_at={run.get('next_wake_at') or '无'}")
    if current_node is not None:
        parts.append(
            "[当前节点真实材料]\n"
            f"{current_node.get('source_summary') or '无摘要'}\n"
            f"{_safe_excerpt(current_node.get('source_payload') or {}, 2500)}"
        )
    parts.append(f"[累积情绪候选]\n{_accumulated_emotion(nodes)}")
    if settlement_reason:
        parts.append(f"[结算原因]\n{settlement_reason}")
    return "\n".join(parts)


def _prior_activity_progress(activities: list[dict[str, Any]]) -> str:
    """Project only settled predecessor facts into the next activity."""
    lines: list[str] = []
    for item in sorted(activities, key=lambda row: int(row.get("order_index") or 0)):
        if item.get("state") not in {"completed", "aborted", "interrupted"}:
            continue
        nodes = item.get("nodes") if isinstance(item.get("nodes"), list) else []
        reflections = [str(node.get("reflection") or "").strip() for node in nodes if str(node.get("reflection") or "").strip()]
        lines.append(
            f"- {item.get('activity_type') or '未知活动'}：{item.get('summary') or '无总结'}"
            + (f"；节点感想：{'；'.join(reflections[-4:])}" if reflections else "")
        )
    return "\n".join(lines[-4:]) or "本事件段尚无先前活动"


def _predecessor_activities(store: WanderRuntimeStore, activity: dict[str, Any]) -> list[dict[str, Any]]:
    predecessors: list[dict[str, Any]] = []
    current_order = int(activity.get("order_index") or 0)
    for item in store.list_activities(activity["run_id"]):
        if int(item.get("order_index") or 0) >= current_order:
            continue
        projected = dict(item)
        projected["nodes"] = store.list_nodes(str(item.get("activity_id") or ""))
        predecessors.append(projected)
    return predecessors


_NODE_REVIEW_INSTRUCTION = """只输出 JSON 对象：
{"reflection":"没有深入感想时写没有感想","emotion_effect":{"changed":false,"from":"","to":"","delta":"变化描述","confidence":0.0},"continue_activity":true,"abort_reason":"","switch_to":null,"next_node_delay_seconds":900,"timing_reason":""}
continue_activity 表示是否继续当前活动。当为 false 且你不想就此结束漫想、而是想换去做另一件事时，用 switch_to 指定新活动（{"event_type":"事件类型","reason":"为什么想做这个"}）；否则为 null。若继续，请自主填写到下一节点前等待的秒数 next_node_delay_seconds 和 timing_reason；900 只是 JSON 示例，不是固定要求。你可以选择很久的休息；系统只会把 0 调整为至少 5 秒以避免空转。
emotion_effect 只描述 AI 因这次当前活动而新产生或变化的自身心情；材料里其他人的情绪、旧记录里的历史情绪、t/l/s 等机器状态都不是 AI 当前的心情。from/to 必须是自然语言心情，无法确认时 changed=false。
不要声称做过真实材料中没有发生的事情。"""

_SETTLEMENT_INSTRUCTION = """只输出 JSON 对象：
{"summary":"作为K对整件事的综合感受","share":<true或false>,"continue_next":false,"next_inclination_note":"","emotion_effect":{"changed":false,"from":"","to":"","delta":"变化描述","confidence":0.0},"next_run_delay_seconds":900,"timing_reason":""}
share 字段：这次漫想若有真实的感触、新的想法、或此刻想念用户而想主动说些什么，输出 true；否则输出 false。由你自主决定。next_run_delay_seconds 是这轮结束后下次漫想前由你自主选择的等待秒数，并填写 timing_reason；它和 continue_next 独立，continue_next 仅表示是否保留下一轮的倾向笔记。900 只是示例，不是固定要求；0 会由系统调整为至少 5 秒。
emotion_effect 只描述 AI 因本次已完成活动而形成的自身即时心情；不要把材料中其他人的情绪、旧记录中的历史情绪或 t/l/s 机器状态抄成 AI 的心情。from/to 必须是自然语言心情，无法确认时 changed=false。
只根据已完成节点总结，不要补造行动。"""


class _DecisionAdapterBase:
    def __init__(
        self,
        store: WanderRuntimeStore,
        llm_caller: Callable[..., Awaitable[FlashJsonResult]] = call_flash_json_detailed,
        context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        self.store = store
        self.llm_caller = llm_caller
        self.context_builder = context_builder
        self.clock: Callable[[], datetime] = datetime.now

    async def _messages(
        self,
        context: DecisionContext,
        run: dict[str, Any],
        runtime_block: str,
        instruction: str,
    ) -> list[dict[str, str]]:
        if not context.persona.strip():
            raise DecisionContextError("persona_is_required")
        if context.session_id != run["session_id"]:
            raise DecisionContextError("session_id_mismatch")
        try:
            if self.context_builder is not None:
                built = await self.context_builder(
                    "WANDER_ACTIVITY",
                    wander_runtime_text=runtime_block,
                    persona=context.persona,
                    session_id=context.session_id,
                )
            else:
                from context_builder.builder import ContextBuilder

                built = await ContextBuilder.build(
                    "WANDER_ACTIVITY",
                    wander_runtime_text=runtime_block,
                    persona=context.persona,
                    session_id=context.session_id,
                )
        except DecisionContextError:
            raise
        except Exception as exc:
            raise DecisionContextError(f"recipe_build:{type(exc).__name__}") from exc

        system_content = getattr(built, "system_content", "")
        sections = getattr(built, "sections", {}) or {}
        runtime_section = sections.get("wander_runtime", {})
        if (
            not system_content
            or not runtime_section.get("text")
            or not system_content.rstrip().endswith(runtime_block.rstrip())
        ):
            raise DecisionContextError("runtime_block_not_final_or_missing")
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": instruction},
        ]

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> Optional[int]:
        for key in keys:
            try:
                value = usage.get(key)
                return int(value) if value is not None else None
            except (AttributeError, TypeError, ValueError):
                continue
        return None


class NodeReviewAdapter(_DecisionAdapterBase):
    async def review(
        self,
        node_id: str,
        context: DecisionContext,
        *,
        allowed_event_types: Optional[frozenset[EventType]] = None,
    ) -> Optional[NodeReviewDecision]:
        node = self.store.get_node(node_id)
        if node is None:
            raise ValueError("node not found")
        activity = self.store.get_activity(node["activity_id"])
        if activity is None:
            raise ValueError("activity not found")
        run = self.store.get_run(activity["run_id"])
        if run is None:
            raise ValueError("run not found")
        if (
            node["state"] != NodeState.RUNNING.value
            or node["execution_status"] != "succeeded"
        ):
            raise ValueError("review requires a succeeded running node")

        nodes = self.store.list_nodes(activity["activity_id"])
        runtime_block = _runtime_block(
            context,
            activity,
            nodes,
            phase="node_review",
            run=run,
            current_node=node,
            prior_activities=_predecessor_activities(self.store, activity),
        )
        if allowed_event_types is not None:
            runtime_block += "\n[当前可续作事件]\n" + (
                "、".join(sorted(event.value for event in allowed_event_types)) or "无"
            )
        try:
            messages = await self._messages(
                context,
                run,
                runtime_block,
                _NODE_REVIEW_INSTRUCTION,
            )
        except DecisionContextError as exc:
            self._record_review_failure(
                run,
                activity,
                node,
                runtime_block,
                "context_error",
                str(exc),
            )
            return None

        try:
            detailed = await self.llm_caller(
                messages=messages,
                temperature=0.0,
                max_tokens=16384,
            )
        except Exception as exc:
            self._record_review_failure(
                run,
                activity,
                node,
                runtime_block,
                "llm_error",
                type(exc).__name__,
            )
            return None

        raw = detailed.parsed if detailed.status == "ok" and isinstance(detailed.parsed, dict) else None
        decision = self._normalize_review(raw, allowed_event_types) if raw is not None else None
        if decision is not None:
            anchor = self.clock()
            delay, source = normalize_delay_seconds(decision.next_node_delay_seconds, now=anchor)
            if source == "fallback":
                decision.timing_source = source
                decision.next_node_delay_seconds = delay
            decision.next_node_at = (anchor + timedelta(seconds=delay)).isoformat()
        status = detailed.status if decision is not None else (
            "validation_error" if raw is not None else detailed.status
        )
        error = detailed.error or ("invalid_node_review" if raw is not None and decision is None else "")
        if decision is not None and decision.abort_reason == "switch_event_unavailable":
            error = "switch_event_unavailable"
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node["node_id"],
            phase=DecisionPhase.NODE_REVIEW,
            model=detailed.model,
            recipe="WANDER_ACTIVITY",
            temperature=detailed.temperature,
            input_context={
                "system": messages[0]["content"],
                "runtime": runtime_block,
                "user": _NODE_REVIEW_INSTRUCTION,
            },
            raw_output=detailed.raw_content,
            reasoning=detailed.reasoning,
            parsed_output={
                "raw": raw or {},
                "normalized": asdict(decision) if decision else {},
            },
            status=status,
            error=error,
            duration_ms=detailed.duration_ms,
            prompt_tokens=self._usage_value(detailed.usage, "prompt_tokens"),
            completion_tokens=self._usage_value(detailed.usage, "completion_tokens"),
            cache_tokens=self._usage_value(detailed.usage, "cached_tokens", "cache_tokens"),
        )
        return decision

    @staticmethod
    def _normalize_review(
        raw: dict[str, Any],
        allowed_event_types: Optional[frozenset[EventType]] = None,
    ) -> NodeReviewDecision:
        reflection = str(raw.get("reflection") or "没有感想").strip()
        continue_activity = _as_bool(raw.get("continue_activity"), default=True)
        abort_reason = str(raw.get("abort_reason") or "").strip()
        switch_to = None
        sw = raw.get("switch_to")
        if not continue_activity and isinstance(sw, dict):
            try:
                ev = EventType(str(sw.get("event_type") or ""))
                strategy_for(ev)  # 校验在 EVENT_CATALOG 中
                if allowed_event_types is not None and ev not in allowed_event_types:
                    abort_reason = "switch_event_unavailable"
                else:
                    switch_to = (ev, str(sw.get("reason") or "").strip()[:200])
            except (ValueError, KeyError):
                switch_to = None
        if not continue_activity and not abort_reason and switch_to is None:
            abort_reason = "自然停下"
        delay, source = normalize_delay_seconds(raw.get("next_node_delay_seconds"))
        return NodeReviewDecision(
            reflection=reflection,
            emotion_effect=_normalize_effect(raw.get("emotion_effect")),
            continue_activity=continue_activity,
            abort_reason=abort_reason,
            switch_to=switch_to,
            next_node_delay_seconds=delay,
            timing_reason=str(raw.get("timing_reason") or "").strip()[:300],
            timing_source=source,
        )

    def _record_review_failure(
        self,
        run: dict[str, Any],
        activity: dict[str, Any],
        node: dict[str, Any],
        runtime_block: str,
        status: str,
        error: str,
    ) -> None:
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node["node_id"],
            phase=DecisionPhase.NODE_REVIEW,
            model="",
            recipe="WANDER_ACTIVITY",
            input_context={"runtime": runtime_block},
            parsed_output={"raw": {}, "normalized": {}},
            status=status,
            error=error,
        )


class SettlementAdapter(_DecisionAdapterBase):
    async def settle(
        self,
        activity_id: str,
        reason: SettlementReason,
        context: DecisionContext,
    ) -> SettlementDecision:
        activity = self.store.get_activity(activity_id)
        if activity is None:
            raise ValueError("activity not found")
        if activity["state"] != ActivityState.SETTLING.value:
            raise ValueError("settlement requires a settling activity")
        run = self.store.get_run(activity["run_id"])
        if run is None:
            raise ValueError("run not found")
        nodes = self.store.list_nodes(activity_id)
        fallback = self._fallback(activity, nodes, reason)
        runtime_block = _runtime_block(
            context,
            activity,
            nodes,
            phase="settlement",
            run=run,
            settlement_reason=reason.value,
            prior_activities=_predecessor_activities(self.store, activity),
        )

        try:
            messages = await self._messages(
                context,
                run,
                runtime_block,
                _SETTLEMENT_INSTRUCTION,
            )
        except DecisionContextError as exc:
            self._record_settlement_failure(
                run,
                activity,
                runtime_block,
                fallback,
                "context_error",
                str(exc),
            )
            return fallback

        try:
            detailed = await self.llm_caller(
                messages=messages,
                temperature=0.0,
                max_tokens=16384,
            )
        except Exception as exc:
            self._record_settlement_failure(
                run,
                activity,
                runtime_block,
                fallback,
                "llm_error",
                type(exc).__name__,
            )
            return fallback

        raw = detailed.parsed if detailed.status == "ok" and isinstance(detailed.parsed, dict) else None
        decision = self._normalize_settlement(raw, fallback) if raw is not None else fallback
        anchor = self.clock()
        delay, source = normalize_delay_seconds(decision.next_run_delay_seconds, now=anchor)
        if source == "fallback":
            decision.timing_source = source
            decision.next_run_delay_seconds = delay
        decision.next_plan_at = (anchor + timedelta(seconds=delay)).isoformat()
        if reason in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR):
            decision.share = False
        status = detailed.status if raw is not None else f"fallback_{detailed.status}"
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            phase=DecisionPhase.SETTLEMENT,
            model=detailed.model,
            recipe="WANDER_ACTIVITY",
            temperature=detailed.temperature,
            input_context={
                "system": messages[0]["content"],
                "runtime": runtime_block,
                "user": _SETTLEMENT_INSTRUCTION,
            },
            raw_output=detailed.raw_content,
            reasoning=detailed.reasoning,
            parsed_output={"raw": raw or {}, "normalized": asdict(decision)},
            status=status,
            error=detailed.error,
            duration_ms=detailed.duration_ms,
            prompt_tokens=self._usage_value(detailed.usage, "prompt_tokens"),
            completion_tokens=self._usage_value(detailed.usage, "completion_tokens"),
            cache_tokens=self._usage_value(detailed.usage, "cached_tokens", "cache_tokens"),
        )
        return decision

    @staticmethod
    def _normalize_settlement(
        raw: dict[str, Any],
        fallback: SettlementDecision,
    ) -> SettlementDecision:
        delay, source = normalize_delay_seconds(raw.get("next_run_delay_seconds"))
        return SettlementDecision(
            summary=str(raw.get("summary") or fallback.summary).strip(),
            share=_as_bool(raw.get("share")),
            continue_next=_as_bool(raw.get("continue_next")),
            next_inclination_note=str(raw.get("next_inclination_note") or "").strip(),
            emotion_effect=_normalize_effect(raw.get("emotion_effect")),
            next_run_delay_seconds=delay,
            timing_reason=str(raw.get("timing_reason") or "").strip()[:300],
            timing_source=source,
        )

    @staticmethod
    def _fallback(
        activity: dict[str, Any],
        nodes: list[dict[str, Any]],
        reason: SettlementReason,
    ) -> SettlementDecision:
        label = strategy_for(EventType(activity["activity_type"])).display_name
        completed = [node for node in nodes if node.get("state") == NodeState.COMPLETED.value]
        if completed:
            facts = "、".join(node.get("source_summary") or "一个节点" for node in completed[-5:])
            summary = f"刚才在{label}，完成了：{facts}。"
        elif reason == SettlementReason.USER_INTERRUPT:
            summary = f"刚才准备{label}，用户回来后停下了。"
        else:
            summary = f"这次{label}结束了，没有形成可记录的节点感想。"
        return SettlementDecision(summary=summary)

    def _record_settlement_failure(
        self,
        run: dict[str, Any],
        activity: dict[str, Any],
        runtime_block: str,
        fallback: SettlementDecision,
        status: str,
        error: str,
    ) -> None:
        fallback.next_plan_at = (self.clock() + timedelta(seconds=900)).isoformat()
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            phase=DecisionPhase.SETTLEMENT,
            model="",
            recipe="WANDER_ACTIVITY",
            input_context={"runtime": runtime_block},
            parsed_output={"raw": {}, "normalized": asdict(fallback)},
            status=f"fallback_{status}",
            error=error,
        )


@dataclass
class HorizonReviewDecision:
    continue_activity: bool = False
    extend_minutes: int = 0
    next_node_delay_seconds: float = 900.0
    reason: str = ""
    new_horizon: str = ""
    next_node_at: str = ""


_HORIZON_REVIEW_INSTRUCTION = """只输出 JSON 对象：
{"continue_activity":false,"extend_minutes":0,"next_node_delay_seconds":900,"reason":""}
这是原先预计时间到达后的复核：只根据真实已完成材料决定是否继续。继续时填写正整数 extend_minutes 和下一节点等待秒数；停止时不要假装已经执行新节点。"""


class HorizonReviewAdapter(_DecisionAdapterBase):
    """One auditable model call only when an estimated plan reaches its edge."""
    async def review(self, activity_id: str, context: DecisionContext) -> Optional[HorizonReviewDecision]:
        activity = self.store.get_activity(activity_id)
        if activity is None:
            raise ValueError("activity not found")
        run = self.store.get_run(activity["run_id"])
        if run is None:
            raise ValueError("run not found")
        old_horizon = str(run.get("next_wake_at") or "")
        for item in reversed(self.store.list_decisions(run["run_id"], DecisionPhase.TIMING_REVIEW)):
            normalized = item.get("parsed_output", {}).get("normalized") or {}
            if item.get("activity_id") == activity_id and normalized.get("expected_horizon") == old_horizon:
                if item.get("status") == "ok":
                    if normalized.get("continue_activity"):
                        try:
                            datetime.fromisoformat(normalized.get("new_horizon") or "")
                            datetime.fromisoformat(normalized.get("next_node_at") or "")
                        except (TypeError, ValueError):
                            return None
                    return HorizonReviewDecision(**{k: normalized.get(k) for k in (
                        "continue_activity", "extend_minutes", "next_node_delay_seconds", "reason", "new_horizon", "next_node_at")})
                return None
        nodes = self.store.list_nodes(activity_id)
        block = _runtime_block(context, activity, nodes, phase="timing_review", run=run,
            prior_activities=_predecessor_activities(self.store, activity)) + (
                f"\n[计划时间事实]\nhorizon_mode=estimate；expected_horizon={old_horizon}；"
                f"next_node_at={activity.get('next_node_at') or '无'}"
            )
        raw, detailed, messages = {}, None, None
        try:
            messages = await self._messages(context, run, block, _HORIZON_REVIEW_INSTRUCTION)
            detailed = await self.llm_caller(messages=messages, temperature=0.0, max_tokens=16384)
            raw = detailed.parsed if detailed.status == "ok" and isinstance(detailed.parsed, dict) else None
            if raw is None:
                raise ValueError("invalid_timing_review")
            continuing = _as_bool(raw.get("continue_activity"))
            extend = int(raw.get("extend_minutes") or 0) if continuing else 0
            if continuing and (isinstance(raw.get("extend_minutes"), bool) or extend <= 0):
                raise ValueError("invalid_extend_minutes")
            delay, _ = normalize_delay_seconds(raw.get("next_node_delay_seconds"))
            decision = HorizonReviewDecision(continuing, extend if continuing else 0, delay,
                str(raw.get("reason") or "").strip()[:300])
            if not decision.continue_activity:
                decision.extend_minutes = 0
            else:
                anchor = self.clock()
                decision.new_horizon = (anchor + timedelta(minutes=extend)).isoformat()
                decision.next_node_at = (anchor + timedelta(seconds=delay)).isoformat()
            status, error = detailed.status, detailed.error
        except Exception as exc:
            decision, status, error = None, "timing_review_error", type(exc).__name__
        normalized = {"expected_horizon": old_horizon, **(asdict(decision) if decision else {})}
        self.store.record_decision(run_id=run["run_id"], activity_id=activity_id,
            phase=DecisionPhase.TIMING_REVIEW, model=getattr(detailed, "model", ""),
            recipe="WANDER_ACTIVITY", input_context={"runtime": block, "user": _HORIZON_REVIEW_INSTRUCTION},
            raw_output=getattr(detailed, "raw_content", ""), reasoning=getattr(detailed, "reasoning", ""),
            duration_ms=getattr(detailed, "duration_ms", None),
            parsed_output={"raw": raw or {}, "normalized": normalized}, status=status, error=error)
        return decision
