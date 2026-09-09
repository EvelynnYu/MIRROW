"""Production orchestration for the persistent Wander runtime.

The runner is the single owner that consumes controller actions. It does not
send chat messages: settlement intent and actual delivery status remain
separate, so an unfinished share adapter cannot create a fake push.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_PLAN_FAILURE_WAKE_SECONDS = 300.0
_EXECUTION_FAILURE_WAKES = (900.0, 1800.0, 3600.0)
_PLANNER_TIMEOUT_SECONDS = 180.0

from .event_catalog import strategy_for
from .event_types import EventType
from .node_execution_adapter import NodeExecutionAdapter, NodeExecutionResult, NodeExecutionStatus
from .plan_decision_adapter import PlanDecisionAdapter, RuntimeContext
from .runtime_controller import RuntimeAction, RuntimeActionType, WanderRuntimeController
from .runtime_decision_adapters import (
    DecisionContext, EmotionEffect, NodeReviewAdapter, NodeReviewDecision,
    SettlementAdapter, SettlementDecision, HorizonReviewAdapter,
)
from .runtime_models import (
    ActivityState, DecisionPhase, GoalMode, NodeState, RunState,
    SettlementReason, WanderActivity, WanderRun, now_iso,
)
from .runtime_store import WanderRuntimeStore
from .timing_policy import DEFAULT_DELAY_SECONDS, normalize_delay_seconds
from .self_reflection_adapter import SelfReflectionAdapter, SelfReflectionContext
from .wish_commit_adapter import WishCommitAdapter

if False:  # typing only; avoids a runtime import cycle for optional delivery
    from .runtime_share_adapter import RuntimeShareAdapter


@dataclass(frozen=True)
class RuntimeContextBundle:
    plan: RuntimeContext
    decision: DecisionContext
    self_reflection: SelfReflectionContext


@dataclass(frozen=True)
class RunnerTickResult:
    delay_seconds: float
    run_id: str = ""
    action: str = "idle"


def _aggregate_xhs_material(
    payloads: list[dict[str, Any]],
    *,
    node_ids: list[str],
    target_count: int,
    success_count: int,
    barrier: str = "",
) -> dict[str, Any]:
    """Build bounded, evidence-first material for the foreground model.

    The node payload remains the durable source of truth.  This projection is
    deliberately limited to title, summary, evidence excerpt, and the
    provider/url proving where each post came from; screenshots and raw UI
    dumps never enter the follow-up prompt.
    """

    posts: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            continue
        provider = str(payload.get("source_provider") or "unknown")[:120]
        source_url = str(payload.get("source_url") or "")[:500]
        source_id = str(payload.get("source_id") or "")[:200]
        title = str(payload.get("title") or "未命名帖子").strip()[:240]
        summary = str(
            payload.get("content_summary")
            or payload.get("summary")
            or "未取得文字摘要"
        ).strip()[:900]
        excerpt = str(payload.get("evidence_excerpt") or "").strip()[:500]
        reliable_source = {
            "provider": provider,
            "url": source_url,
            "source_id": source_id,
        }
        posts.append({
            "index": index + 1,
            "node_id": node_ids[index] if index < len(node_ids) else "",
            "source_id": source_id,
            "title": title,
            "summary": summary,
            "content_summary": summary,
            "evidence_excerpt": excerpt,
            "source_provider": provider,
            "source_url": source_url,
            "reliable_source": reliable_source,
            "source": reliable_source,
        })

    comparison_items = [
        {
            "title": post["title"],
            "summary": post["summary"],
            "evidence_excerpt": post["evidence_excerpt"],
            "reliable_source": post["reliable_source"],
        }
        for post in posts
    ]
    cross_material = {
        "target_count": int(target_count),
        "success_count": int(success_count),
        "items": comparison_items,
        "comparison_basis": (
            "横向材料仅由各篇独立的专用手机屏幕证据组成；"
            "未把未读到的正文、视频或互动信息补入总结。"
        ),
    }
    title_bits = [post["title"] for post in posts]
    summary = (
        f"已取得{success_count}/{target_count}篇小红书材料"
        + (f"：{'；'.join(title_bits)}" if title_bits else "")
        + (f"；页面障碍：{barrier}" if barrier else "")
    )
    return {
        "target_count": int(target_count),
        "success_count": int(success_count),
        "posts": posts,
        "cross_post_summary_material": cross_material,
        "summary": summary[:2000],
        **({"barrier": barrier} if barrier else {}),
    }


def _effect_value(effect: Any, *names: str, default: Any = "") -> Any:
    """Read both the current EmotionEffect object and legacy dict-shaped effects."""
    for name in names:
        if isinstance(effect, dict) and name in effect:
            value = effect.get(name)
        else:
            value = getattr(effect, name, None)
        if value is not None and value != "":
            return value
    return default


def _bounded_effect_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(0.0, min(1.0, number))


class WanderRuntimeRunner:
    """Advance a persisted run until it reaches a real wait boundary."""

    def __init__(
        self,
        *,
        store: WanderRuntimeStore,
        controller: WanderRuntimeController,
        planner: PlanDecisionAdapter,
        executor: NodeExecutionAdapter,
        reviewer: NodeReviewAdapter,
        settlement: SettlementAdapter,
        self_reflection: SelfReflectionAdapter,
        wish_commit: WishCommitAdapter,
        share_adapter: Optional["RuntimeShareAdapter"] = None,
        context_provider: Callable[[], Awaitable[RuntimeContextBundle]],
        on_activity_settled: Optional[Callable[[EventType], None]] = None,
        on_notification: Optional[Callable[[str, str, list[dict]], Awaitable[None]]] = None,
        affect_service: Optional[Any] = None,
        clock: Callable[[], datetime] = datetime.now,
        default_interval_seconds: float = 900.0,
        planner_timeout_seconds: float = _PLANNER_TIMEOUT_SECONDS,
    ):
        self.store = store
        self.controller = controller
        self.planner = planner
        self.executor = executor
        self.reviewer = reviewer
        # Foreground chat-tool reconciliation can run without autonomous
        # decision adapters; it must not instantiate a background model path.
        self.horizon_reviewer = (
            HorizonReviewAdapter(store, reviewer.llm_caller, reviewer.context_builder)
            if reviewer is not None else None
        )
        self.settlement = settlement
        self.self_reflection = self_reflection
        self.wish_commit = wish_commit
        self.share_adapter = share_adapter
        self.context_provider = context_provider
        self.on_activity_settled = on_activity_settled
        self.on_notification = on_notification
        self.affect_service = affect_service
        self.clock = clock
        for adapter in (self.reviewer, self.settlement, self.horizon_reviewer):
            if adapter is not None:
                adapter.clock = clock
        self.default_interval_seconds = max(5.0, float(default_interval_seconds))
        self.planner_timeout_seconds = max(0.05, float(planner_timeout_seconds))
        self._tick_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._claimed_scheduler_wake: Optional[dict[str, Any]] = None
        self._suppress_next_schedule = False
        self._interrupt_epoch = 0
        # Recovery is a process-start boundary, not a generic "ensure tables"
        # operation.  Foreground chat tools may call initialize() while the
        # background planner is legitimately awaiting its model response; a
        # second recovery pass must never abort that live planning run.
        self._initialized = False


    def initialize(self) -> None:
        if self._initialized:
            return
        self.store.initialize()
        recovered = self.store.recover_stale_planning_runs(
            now=self.clock(),
            retry_delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
        )
        self._initialized = True
        if recovered:
            logger.warning(
                "恢复 %d 个无活动 planner run: %s",
                len(recovered),
                ", ".join(str(item.get("run_id") or "") for item in recovered),
            )

    async def wait(self, delay_seconds: float) -> None:
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=max(0.05, delay_seconds))
        except asyncio.TimeoutError:
            pass

    def wake(self) -> None:
        self._wake_event.set()

    async def settle_interrupt(self, activity_id: str) -> Optional[dict]:
        """Finish one already-requested user interruption without starting a new run.

        This is called from the foreground chat path.  It uses the same runner
        lock as the background loop, so cancelling that loop cannot create a
        second settlement or leave the interrupted activity pending until the
        next wander activation.
        """
        async with self._tick_lock:
            activity = self.store.get_activity(activity_id)
            if activity is None or not activity.get("interrupt_requested"):
                return activity
            if activity["state"] in {"completed", "interrupted", "aborted"}:
                return activity
            bundle = await self.context_provider()
            for _ in range(6):
                action = self.controller.poll(run_id=activity["run_id"])
                if action.activity_id and action.activity_id != activity_id:
                    break
                if action.action == RuntimeActionType.RECOVER_NODE:
                    await self._recover_node(action, bundle)
                    continue
                if action.action == RuntimeActionType.SETTLE:
                    await self._settle(action, bundle)
                    break
                if action.action in {RuntimeActionType.IDLE, RuntimeActionType.WAIT}:
                    break
                # An interrupt may arrive between poll and node start. Never
                # execute a fresh node from the foreground reply path.
                if action.action == RuntimeActionType.START_NODE:
                    self.controller.request_interrupt(activity_id)
                    continue
            return self.store.get_activity(activity_id)

    async def execute_chat_xhs(
        self,
        *,
        session_id: str,
        intent: str,
        query: str = "",
        count: int = 3,
        target_count: Optional[int] = None,
        comment_draft: str = "",
    ) -> dict:
        """Run an explicitly granted XHS batch through the v3 identity chain.

        Each post is one independent persisted node.  The batch is bounded by
        the backend-resolved goal (never a model-supplied tool argument), and
        it is never handed to the autonomous share path: the model receives
        only the aggregate node material and the delivery row is an explicit
        ``chat_tool/not_requested`` fact.  The runner lock serializes this
        foreground entry point with background ticks.
        """

        sid = str(session_id or "").strip()
        selected_intent = str(intent or "").strip().lower()
        selected_query = " ".join(str(query or "").split())[:60]
        draft = " ".join(str(comment_draft or "").split())[:2000]
        try:
            requested_count = target_count if target_count is not None else count
            target = max(1, min(5, int(requested_count)))
        except (TypeError, ValueError):
            target = 3
        if not sid:
            return {"status": "invalid_request", "error": "session_id_required"}
        if selected_intent not in {"recommend", "search", "read"}:
            return {"status": "invalid_request", "error": "intent_out_of_scope"}

        async with self._tick_lock:
            now = self.clock()
            run = WanderRun(
                trigger_reason="chat_tool",
                session_id=sid,
                plan_horizon_min=5,
                next_wake_at=(now + timedelta(minutes=5)).isoformat(),
            )
            run.transition_to(RunState.ACTIVE_WAIT)
            # The handler's existing target-query boundary treats 首页推荐 as
            # non-search.  A missing query therefore cannot become a hidden
            # arbitrary search term.
            activity_reason = selected_query or "首页推荐"
            activity = WanderActivity(
                activity_type=EventType.BROWSE_XIAOHONGSHU.value,
                run_id=run.run_id,
                goal_mode=GoalMode.COUNT,
                goal_value=target,
                reason=activity_reason,
            )
            activity.transition_to(ActivityState.ACTIVE_WAIT)
            activity.next_node_at = now.isoformat()
            self.store.save_plan(run, [activity])
            # Reserve the no-push decision before touching the device.  If a
            # process dies after the node is claimed, recovery still cannot
            # interpret this chat run as a request for proactive delivery.
            delivery_id = self.store.record_delivery(
                run_id=run.run_id,
                activity_id=activity.activity_id,
                delivery_type="chat_tool",
                status="not_requested",
            )

            node_ids: list[str] = []
            successful_payloads: list[dict[str, Any]] = []
            barrier = ""

            for _ in range(target):
                node = None
                execution: Optional[NodeExecutionResult] = None
                try:
                    node = self.controller.begin_node(activity.activity_id)
                    node_ids.append(node.node_id)
                    execution = await self.executor.execute(node.node_id, comment_draft=draft)
                except Exception as exc:
                    # The normal adapter catches handler failures.  This branch
                    # is only a final reconciliation guard for an unexpected
                    # boundary error; it records an honest failed node rather
                    # than leaving the chat run eligible for future execution.
                    error = type(exc).__name__
                    if node is not None:
                        current_activity = self.store.get_activity(activity.activity_id)
                        current_run = self.store.get_run(run.run_id)
                        if current_activity and current_run:
                            node_obj = self.executor._required_node(node.node_id)
                            execution = self.executor._finish(
                                current_run,
                                current_activity,
                                node_obj,
                                NodeExecutionResult(
                                    NodeExecutionStatus.FAILED,
                                    summary="小红书聊天节点执行异常",
                                    error=error,
                                    audit_model="chat_tool",
                                    audit_input={
                                        "event_type": EventType.BROWSE_XIAOHONGSHU.value,
                                        "source": "chat_tool",
                                    },
                                    retry_safe=False,
                                ),
                            )
                    if execution is None:
                        barrier = error
                        break

                if execution is None:
                    barrier = barrier or "node_execution_missing"
                    break
                payload = dict(execution.payload or {})
                if execution.status != NodeExecutionStatus.SUCCEEDED:
                    barrier = str(
                        payload.get("status")
                        or execution.error
                        or execution.status.value
                    )[:200]
                    try:
                        self.controller.complete_node_review(
                            node.node_id,
                            reflection=execution.summary,
                            continue_activity=False,
                            abort_reason=barrier,
                            execution_failed=True,
                        )
                    except Exception as exc:
                        logger.error("chat XHS failed-node reconciliation: %s", type(exc).__name__)
                        barrier = barrier or type(exc).__name__
                    break

                successful_payloads.append(payload)
                final_node = len(successful_payloads) >= target
                try:
                    self.controller.complete_node_review(
                        node.node_id,
                        reflection=execution.summary,
                        continue_activity=not final_node,
                        abort_reason="",
                        execution_failed=False,
                    )
                except Exception as exc:
                    logger.error("chat XHS node reconciliation: %s", type(exc).__name__)
                    barrier = type(exc).__name__
                    break
                if final_node:
                    break

            success_count = len(successful_payloads)
            if success_count < target and not barrier:
                barrier = "node_execution_incomplete"
            settlement_reason = (
                SettlementReason.GOAL_REACHED
                if success_count >= target
                else SettlementReason.EXECUTION_ERROR
            )
            aggregate = _aggregate_xhs_material(
                successful_payloads,
                node_ids=node_ids[:success_count],
                target_count=target,
                success_count=success_count,
                barrier=barrier,
            )
            settlement_summary = str(aggregate.get("summary") or "小红书批量浏览已结束")[:2000]
            try:
                self.controller.settle_activity(
                    activity.activity_id,
                    settlement_reason,
                    summary=settlement_summary,
                    share_decision=False,
                )
            except Exception as exc:
                # Do not claim success if final state reconciliation failed.
                logger.error("chat XHS v3 settlement failed: %s", type(exc).__name__)
                return {
                    "status": "runtime_reconcile_failed",
                    "error": type(exc).__name__,
                    "barrier": type(exc).__name__,
                    "run_id": run.run_id,
                    "activity_id": activity.activity_id,
                    "node_ids": node_ids,
                    "delivery_id": delivery_id,
                }

            outcome = "success" if success_count >= target else (
                "partial_success" if success_count else "failed"
            )
            result: dict[str, Any] = {
                "status": outcome,
                "target_count": target,
                "goal_count": target,
                "count": target,
                "goal_mode": GoalMode.COUNT.value,
                "success_count": success_count,
                "successful_count": success_count,
                "post_count": success_count,
                "posts": aggregate["posts"],
                "aggregate": aggregate,
                "cross_post_summary_material": aggregate["cross_post_summary_material"],
                "cross_post_material": aggregate["cross_post_summary_material"],
                "run_id": run.run_id,
                "activity_id": activity.activity_id,
                "node_ids": node_ids,
                "source": "chat_tool",
                "delivery_status": "not_requested",
            }
            if node_ids:
                result["node_id"] = node_ids[0] if len(node_ids) == 1 else node_ids[-1]
            if barrier:
                result["barrier"] = barrier
                result["failure_barrier"] = barrier
                result["error"] = barrier
            return result

    async def tick(self) -> RunnerTickResult:
        async with self._tick_lock:
            # The runner's clock remains authoritative even if an adapter was
            # replaced by the host (or by an isolated test fixture).
            for adapter in (self.reviewer, self.settlement, self.horizon_reviewer):
                if adapter is not None:
                    adapter.clock = self.clock
            bundle = await self.context_provider()
            session_id = bundle.plan.session_id
            active = self.store.get_active_run(session_id=session_id)
            current_run_id = active["run_id"] if active else ""
            if active is None:
                scheduled = self.store.get_scheduler_wake(session_id)
                if scheduled is not None:
                    delay = self._scheduler_delay(scheduled)
                    if delay > 0:
                        return RunnerTickResult(delay, "", "scheduled_wait")
                result = await self._start_run(bundle)
                if result is not None:
                    return result
                active = self.store.get_active_run(session_id=session_id)
                current_run_id = active["run_id"] if active else ""

            for _ in range(12):
                action = self.controller.poll(session_id=session_id)
                current_run_id = action.run_id or current_run_id
                if action.action == RuntimeActionType.IDLE:
                    scheduled = self.store.get_scheduler_wake(session_id)
                    if scheduled is not None:
                        return RunnerTickResult(
                            self._scheduler_delay(scheduled), current_run_id, "scheduled_wait"
                        )
                    return RunnerTickResult(self.default_interval_seconds, current_run_id, "idle")
                if action.action == RuntimeActionType.WAIT:
                    return RunnerTickResult(self._seconds_until_due(action), action.run_id or "", "wait")
                if action.action == RuntimeActionType.START_NODE:
                    wait_result = await self._start_node(action, bundle)
                    if wait_result is not None:
                        return wait_result
                    continue
                if action.action == RuntimeActionType.RECOVER_NODE:
                    wait_result = await self._recover_node(action, bundle)
                    if wait_result is not None:
                        return wait_result
                    continue
                if action.action == RuntimeActionType.SETTLE:
                    await self._settle(action, bundle)
                    continue
                if action.action == RuntimeActionType.REVIEW_TIMING:
                    await self._review_horizon(action, bundle)
                    continue
            return RunnerTickResult(1.0, current_run_id, "transition_limit")

    async def _start_run(self, bundle: RuntimeContextBundle) -> Optional[RunnerTickResult]:
        session_id = bundle.plan.session_id or "main"
        # A due wake is consumed before planning.  A future wake is checked in
        # tick(), so a restart cannot accidentally plan early.
        self._claimed_scheduler_wake = self.store.claim_scheduler_wake(session_id, self.clock())
        run = WanderRun(session_id=session_id)
        self.store.save_run(run)
        pending = self.store.claim_pending_inclination(run.session_id, run.run_id)
        plan_context = replace(
            bundle.plan,
            inclination_note=pending["note"] if pending else bundle.plan.inclination_note,
        )
        try:
            plan = await asyncio.wait_for(
                self.planner.decide(run, plan_context),
                timeout=self.planner_timeout_seconds,
            )
        except asyncio.CancelledError:
            # 计划决策被打断（漫想创建循环被取消/重启）：run 已落库 planning，
            # 不清理会残留幽灵 run——get_active_run 把 planning 当活跃，poll 无活动返回 IDLE，
            # 后续所有 tick 都找不到新 run 机会，漫想永久空转。
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "plan_cancelled"
            self.store.save_run(run)
            if not self._suppress_next_schedule:
                self._schedule_next(
                    session_id=session_id,
                    delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
                    wake_reason="plan_cancelled",
                    source_run_id=run.run_id,
                )
            self._suppress_next_schedule = False
            raise
        except asyncio.TimeoutError:
            # A timed-out planner has no accepted activities and cannot be
            # resumed safely.  Close its claim and leave one durable retry wake
            # instead of allowing a permanent ``planning`` ghost.
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "planner_timeout"
            self.store.save_run(run)
            if not self._suppress_next_schedule:
                self._schedule_next(
                    session_id=session_id,
                    delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
                    wake_reason="planner_timeout",
                    source_run_id=run.run_id,
                )
            self._suppress_next_schedule = False
            return RunnerTickResult(_PLAN_FAILURE_WAKE_SECONDS, run.run_id, "planner_timeout")
        except Exception:
            # 计划决策失败（Flash 超时/异常等）：同样兜底成 aborted，避免幽灵 run。
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "plan_error"
            self.store.save_run(run)
            if not self._suppress_next_schedule:
                self._schedule_next(
                    session_id=session_id,
                    delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
                    wake_reason="plan_error",
                    source_run_id=run.run_id,
                )
            self._suppress_next_schedule = False
            return RunnerTickResult(_PLAN_FAILURE_WAKE_SECONDS, run.run_id, "plan_error")
        if plan is None:
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "plan_failed"
            self.store.save_run(run)
            if not self._suppress_next_schedule:
                self._schedule_next(
                    session_id=session_id,
                    delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
                    wake_reason="plan_failed",
                    source_run_id=run.run_id,
                )
            self._suppress_next_schedule = False
            return RunnerTickResult(_PLAN_FAILURE_WAKE_SECONDS, run.run_id, "plan_failed")
        if self._suppress_next_schedule:
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "user_interrupt_before_plan"
            self.store.save_run(run)
            self._suppress_next_schedule = False
            return RunnerTickResult(self.default_interval_seconds, run.run_id, "user_interrupt")
        try:
            self.controller.apply_plan(run, plan)
        except Exception:
            # Plan normalization/commit is part of the planner ownership
            # boundary.  If validation or persistence rejects the model's
            # decision, close the claim just like a planner exception instead
            # of leaving a planning run with no recoverable successor.
            if pending:
                self.store.release_pending_inclination(pending["source_activity_id"], run.run_id)
            run.transition_to(RunState.ABORTED)
            run.ended_at = now_iso()
            run.outcome = "plan_commit_error"
            self.store.save_run(run)
            if not self._suppress_next_schedule:
                self._schedule_next(
                    session_id=session_id,
                    delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
                    wake_reason="plan_commit_error",
                    source_run_id=run.run_id,
                )
            self._suppress_next_schedule = False
            return RunnerTickResult(_PLAN_FAILURE_WAKE_SECONDS, run.run_id, "plan_commit_error")
        if pending:
            self.store.consume_pending_inclination(pending["source_activity_id"], run.run_id)
        return None

    async def _start_node(
        self,
        action: RuntimeAction,
        bundle: RuntimeContextBundle,
    ) -> Optional[RunnerTickResult]:
        node = self.controller.begin_node(action.activity_id or "")
        activity = self.store.get_activity(node.activity_id)
        event_type = EventType(activity["activity_type"])
        if event_type == EventType.SELF_REFLECTION:
            decision = await self.self_reflection.generate(node.node_id, bundle.self_reflection)
            if decision is None:
                self.controller.complete_node_review(
                    node.node_id, continue_activity=False, abort_reason="self_reflection_failed",
                    execution_failed=True,
                )
                return None
            wish_results = self.wish_commit.commit(node.node_id)
            await self._notify_wish_board_if_needed(
                bundle.plan.session_id, node.node_id, wish_results
            )
            self.controller.complete_node_review(
                node.node_id,
                reflection=decision.overall_reflection,
                continue_activity=False,
            )
            return None

        execution = await self.executor.execute(node.node_id)
        refreshed_activity = self.store.get_activity(node.activity_id)
        if execution.status == NodeExecutionStatus.WAITING_EXTERNAL:
            due = execution.side_effect_refs.get("expected_completion_at")
            delay = self._delay_until(due) if due else 60.0
            return RunnerTickResult(delay, action.run_id or "", "waiting_external")
        if execution.status == NodeExecutionStatus.SKIPPED:
            # A truthful capability gate or natural material exhaustion is a
            # completed bounded node, not an execution error.  Keep the node's
            # ``skipped`` evidence while letting settlement classify the
            # activity as a natural stop instead of FAILED/EXECUTION_ERROR.
            self.controller.complete_node_review(
                node.node_id,
                reflection=execution.summary,
                continue_activity=False,
                abort_reason=execution.error or "node_skipped",
                execution_failed=False,
            )
            return None
        if execution.status != NodeExecutionStatus.SUCCEEDED:
            self.controller.complete_node_review(
                node.node_id,
                reflection=execution.summary,
                continue_activity=False,
                abort_reason=execution.error or execution.status.value,
                execution_failed=True,
            )
            return None
        review = self._existing_node_review(node.node_id)
        if review is None:
            review = await self.reviewer.review(
                node.node_id, bundle.decision,
                allowed_event_types=self.planner.effective_allowed_event_types(),
            )
        if review is None:
            # The external/real action itself succeeded.  A missing or
            # malformed reflective review is a review-channel availability
            # problem, not an execution failure; keep the truthful source
            # evidence and settle this node naturally.
            self.controller.complete_node_review(
                node.node_id,
                reflection=self.store.get_node(node.node_id).get("source_summary", "") if self.store.get_node(node.node_id) else "",
                continue_activity=False,
                abort_reason="node_review_unavailable",
                execution_failed=False,
            )
            return None
        self._complete_review(node.node_id, review, refreshed_activity)
        await self._maybe_record_listen_music_self_book(
            node.node_id, review.reflection, refreshed_activity
        )
        return None

    async def _recover_node(
        self,
        action: RuntimeAction,
        bundle: RuntimeContextBundle,
    ) -> Optional[RunnerTickResult]:
        if not action.node_id:
            self.controller.begin_settlement(action.activity_id or "")
            return None
        node = self.store.get_node(action.node_id)
        activity = self.store.get_activity(action.activity_id or "")
        if node is None or activity is None:
            raise ValueError("recovery identity is incomplete")
        if activity["interrupt_requested"]:
            execution_complete = (
                node.get("execution_status") == NodeExecutionStatus.SUCCEEDED.value
            )
            self.controller.complete_node_review(
                node["node_id"],
                reflection=node.get("source_summary") or "用户回来时，这个节点尚未确认完成。",
                continue_activity=False,
                abort_reason="" if execution_complete else "user_interrupt_before_completion",
                execution_failed=not execution_complete,
            )
            return None
        event_type = EventType(activity["activity_type"])
        if node["execution_status"] == NodeExecutionStatus.WAITING_EXTERNAL.value:
            due = node["side_effect_refs"].get("expected_completion_at")
            if due and self._delay_until(due) > 0.05:
                return RunnerTickResult(
                    self._delay_until(due), action.run_id or "", "waiting_external"
                )
            await self.executor.confirm_external_completion(node["node_id"], "duration_elapsed")
            node = self.store.get_node(node["node_id"])
        if node["execution_status"] == NodeExecutionStatus.SUCCEEDED.value:
            if event_type == EventType.SELF_REFLECTION:
                wish_results = self.wish_commit.commit(node["node_id"])
                await self._notify_wish_board_if_needed(
                    bundle.plan.session_id, node["node_id"], wish_results
                )
                self.controller.complete_node_review(
                    node["node_id"],
                    reflection=str(node["source_payload"].get("overall_reflection") or "完成了一次自省。"),
                    continue_activity=False,
                )
                return None
            review = self._existing_node_review(node["node_id"])
            if review is None:
                review = await self.reviewer.review(
                    node["node_id"], bundle.decision,
                    allowed_event_types=self.planner.effective_allowed_event_types(),
                )
            if review is not None:
                self._complete_review(node["node_id"], review, activity)
                await self._maybe_record_listen_music_self_book(
                    node["node_id"], review.reflection, activity
                )
                return None
            # Recovery has already observed a succeeded external action.  Do
            # not turn a missing/bad review response into execution_failed;
            # the node remains a completed natural stop with an explicit
            # review availability reason.  The review adapter has already
            # retained its audit failure evidence.
            self.controller.complete_node_review(
                node["node_id"],
                reflection=str(node.get("source_summary") or ""),
                continue_activity=False,
                abort_reason="node_review_unavailable",
                execution_failed=False,
            )
            return None
        if node["execution_status"] == NodeExecutionStatus.SKIPPED.value:
            self.controller.complete_node_review(
                node["node_id"],
                reflection=node.get("source_summary") or "节点没有可取得的新材料。",
                continue_activity=False,
                abort_reason=node.get("execution_error") or "node_skipped",
                execution_failed=False,
            )
            return None
        self.controller.complete_node_review(
            node["node_id"],
            reflection=node.get("source_summary") or "进程恢复时没有重放未确认的外部动作。",
            continue_activity=False,
            abort_reason=f"recovery_{node.get('execution_status') or 'unknown'}",
            execution_failed=True,
        )
        return None

    async def _notify_wish_board_if_needed(
        self,
        session_id: str,
        node_id: str,
        results: list[dict],
    ) -> None:
        """Deliver one generic UI notification after a real wish mutation.

        The callback is deliberately outside the wish SQLite transaction.  The
        service commit is the durable source of truth; a callback failure leaves
        the succeeded node recoverable, while the notification emitter can use
        ``node_id`` as its own idempotency key on retry.
        """
        if self.on_notification is None or not self.wish_commit.has_mutation(results):
            return
        outcome = self.on_notification(session_id, node_id, results)
        if inspect.isawaitable(outcome):
            await outcome

    async def _maybe_record_listen_music_self_book(
        self,
        node_id: str,
        reflection: str,
        activity: dict,
    ) -> None:
        """v3 听歌节点评审后，把真实歌曲名-歌手+感想写入自我书「音乐品味」。

        只在有真实歌曲材料（title+fingerprint）时写入，未知 title 由
        `_update_self_book` 内部跳过，绝不产生「未知-未知」脏记录。
        """
        try:
            if not activity or EventType(activity["activity_type"]) != EventType.LISTEN_MUSIC:
                return
            node = self.store.get_node(node_id)
            if node is None:
                return
            payload = node.get("source_payload") or {}
            if not payload.get("title") or not payload.get("fingerprint"):
                return
            handler = self.executor.handler_factory.get_handler(EventType.LISTEN_MUSIC)
            if handler is None or getattr(handler, "_k_self_book", None) is None:
                return
            await handler._update_self_book(
                {**payload, "cognition_source_id": f"wander-node:{node_id}"},
                reflection,
                str(payload.get("reason") or "") or "想听首歌",
            )
        except Exception as exc:
            logger.warning("记录漫想听歌到自我书失败: %s: %s", type(exc).__name__, exc)

    def _complete_review(
        self,
        node_id: str,
        review: NodeReviewDecision,
        activity: dict,
    ) -> None:
        # Recheck after the awaited review and on recovery: availability may
        # have changed since the model saw the catalog.
        if review.switch_to and review.switch_to[0] not in self.planner.effective_allowed_event_types():
            self.store.record_decision(
                run_id=activity["run_id"], activity_id=activity["activity_id"], node_id=node_id,
                phase=DecisionPhase.NODE_REVIEW, model="", recipe="WANDER_ACTIVITY",
                input_context={"admission": "review_completion"},
                parsed_output={"rejected_event_type": review.switch_to[0].value},
                status="rejected", error="switch_event_unavailable",
            )
            review.switch_to = None
            review.abort_reason = "switch_event_unavailable"
        next_node_at = None
        if review.continue_activity and not review.abort_reason:
            delay, _ = normalize_delay_seconds(review.next_node_delay_seconds)
            next_node_at = datetime.fromisoformat(review.next_node_at) if review.next_node_at else self.clock() + timedelta(seconds=delay)
        self.controller.complete_node_review(
            node_id,
            reflection=review.reflection,
            emotion_effect={
                "changed": review.emotion_effect.changed,
                "from": review.emotion_effect.from_emotion,
                "to": review.emotion_effect.to_emotion,
                "delta": review.emotion_effect.delta,
                "confidence": review.emotion_effect.confidence,
            },
            continue_activity=review.continue_activity,
            abort_reason=review.abort_reason,
            next_node_at=next_node_at,
            switch_to=review.switch_to,
        )

    async def _settle(self, action: RuntimeAction, bundle: RuntimeContextBundle) -> None:
        interrupt_epoch = self._interrupt_epoch
        activity_id = action.activity_id or ""
        reason = action.reason or SettlementReason.NATURAL_STOP
        run_row = self.store.get_run(action.run_id or "") or {}
        run = run_row
        chat_tool_run = run_row.get("trigger_reason") == "chat_tool"
        self.controller.begin_settlement(activity_id, reason)
        decision = self._existing_settlement(activity_id)
        if decision is None and not chat_tool_run:
            decision = await self.settlement.settle(activity_id, reason, bundle.decision)
        latest = self.store.get_activity(activity_id) or {}
        if latest.get("interrupt_requested") or self._interrupt_epoch != interrupt_epoch:
            reason = SettlementReason.USER_INTERRUPT
        if chat_tool_run:
            # A chat-tool result already returned through the foreground model
            # path.  Reconciliation after a crash must close the chain but can
            # never generate a second autonomous share/push.
            decision = SettlementDecision(
                summary="聊天工具节点已完成恢复结算。",
                share=False,
                continue_next=False,
                next_inclination_note="",
                emotion_effect=EmotionEffect(),
            )
        if reason in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR):
            decision.share = False
            decision.continue_next = False
            decision.next_inclination_note = ""
        run_activities = self.store.list_activities(action.run_id or "")
        current_order = int((self.store.get_activity(activity_id) or {}).get("order_index") or 0)
        has_later_activity = any(
            int(item.get("order_index") or 0) > current_order
            and item.get("state") == ActivityState.PLANNED.value
            for item in run_activities
        )
        # Sharing belongs to the completed event segment.  Earlier activity
        # intents are retained, but no chat message is emitted while a real
        # successor is still going to run.  The final activity carries the
        # aggregate intent and produces at most one proactive message.
        if (
            not has_later_activity
            and not chat_tool_run
            and reason not in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR)
        ):
            decision.share = bool(decision.share or any(
                item.get("share_decision") is True
                for item in run_activities
                if int(item.get("order_index") or 0) < current_order
            ))
        # Prepare the durable wake BEFORE committing a terminal run, with no
        # await between them. Recovery reuses the audit's absolute timestamp.
        # Optional sidecars and delivery must never recreate this wake later.
        session_id = str(run_row.get("session_id") or "")
        if reason == SettlementReason.USER_INTERRUPT or chat_tool_run:
            self.store.clear_scheduler_wake(session_id or None)
            self._suppress_next_schedule = False
        elif session_id and (not has_later_activity or reason == SettlementReason.EXECUTION_ERROR):
            existing_wake = self.store.get_scheduler_wake(session_id)
            if not existing_wake or existing_wake.get("source_activity_id") != activity_id:
                delay, wake_reason, streak = self._settlement_schedule(
                    activity_id=activity_id, session_id=session_id, reason=reason,
                    continue_next=decision.continue_next, next_inclination_note=decision.next_inclination_note,
                )
                if reason != SettlementReason.EXECUTION_ERROR:
                    delay, _ = normalize_delay_seconds(decision.next_run_delay_seconds)
                    wake_reason = "model_wait" if decision.timing_source == "model" else "fallback_wait"
                due = decision.next_plan_at if reason != SettlementReason.EXECUTION_ERROR else ""
                self.store.save_scheduler_wake(
                    session_id=session_id,
                    next_plan_at=due or (self.clock() + timedelta(seconds=delay)).isoformat(),
                    wake_reason=wake_reason, source_activity_id=activity_id,
                    source_run_id=action.run_id or "", failure_streak=streak,
                )
        result = self.controller.settle_activity(
            activity_id,
            reason,
            summary=decision.summary,
            emotion_effect={
                "changed": decision.emotion_effect.changed,
                "from": decision.emotion_effect.from_emotion,
                "to": decision.emotion_effect.to_emotion,
                "delta": decision.emotion_effect.delta,
                "confidence": decision.emotion_effect.confidence,
            },
            continue_next=decision.continue_next,
            share_decision=decision.share,
            next_inclination_note=decision.next_inclination_note,
        )
        # Settlement is already committed before this optional sidecar.  A
        # provider/storage failure here is diagnostic-only and must not reopen
        # or otherwise change the settled activity.
        activity = self.store.get_activity(activity_id)
        await self._persist_settlement_emotion(activity, run, decision, reason)
        if self._interrupt_epoch != interrupt_epoch:
            decision.share = False
            decision.continue_next = False
        if (
            decision.continue_next
            and decision.next_inclination_note
            and reason not in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR)
        ):
            run = self.store.get_run(action.run_id or "")
            self.store.save_pending_inclination(
                activity_id,
                str(run.get("session_id") or "") if run else "",
                decision.next_inclination_note,
            )
        status = "deferred" if (
            decision.share
            and not has_later_activity
            and reason not in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR)
        ) else "not_requested"
        if not self._delivery_exists(activity_id):
            self.store.record_delivery(
                run_id=action.run_id or "",
                activity_id=activity_id,
                delivery_type="wander_push",
                status=status,
            )
        if status == "deferred" and self.share_adapter is not None:
            await self.share_adapter.deliver(activity_id)
        activity = self.store.get_activity(activity_id)
        if activity and self.on_activity_settled:
            self.on_activity_settled(EventType(activity["activity_type"]))
        if result.action == RuntimeActionType.WAIT:
            self.wake()

    async def _persist_settlement_emotion(
        self,
        activity: Optional[dict[str, Any]],
        run: Optional[dict[str, Any]],
        decision: SettlementDecision,
        reason: SettlementReason,
    ) -> None:
        """Consume a real settled emotion effect into AI self sidecar."""
        effect = decision.emotion_effect
        if not activity or not run or not _effect_value(effect, "changed", default=False):
            return
        reason_value = reason.value if isinstance(reason, SettlementReason) else str(reason)
        if reason_value == SettlementReason.EXECUTION_ERROR.value:
            return
        if str(activity.get("state") or "") not in {"completed", "interrupted", "aborted"}:
            return
        activity_id = str(activity.get("activity_id") or "").strip()
        if not activity_id:
            return
        try:
            service = self.affect_service
            if service is None:
                return  # Optional host sidecar is not connected.
            to_emotion = str(_effect_value(effect, "to_emotion", "to", default="") or "").strip()[:120]
            from_emotion = str(_effect_value(effect, "from_emotion", "from", default="") or "").strip()[:120]
            delta = str(_effect_value(effect, "delta", "reason", default="") or "").strip()[:500]
            labels = _effect_value(effect, "labels", default=[])
            if not to_emotion and isinstance(labels, (list, tuple)):
                to_emotion = next((str(item).strip()[:120] for item in labels if str(item).strip()), "")
            family = str(_effect_value(effect, "family", "emotion_family", default="复杂/未归类") or "复杂/未归类").strip()[:40]
            persistence = str(_effect_value(effect, "persistence", default="situational") or "situational").strip()
            if persistence not in {"momentary", "situational", "lingering"}:
                persistence = "situational"
            confidence = _bounded_effect_number(_effect_value(effect, "confidence", default=0.0))
            intensity = _bounded_effect_number(_effect_value(effect, "intensity", default=confidence), default=confidence)
            target = str(_effect_value(effect, "target", default="") or "").strip()[:120]
            operation = str(_effect_value(effect, "operation", default="add") or "add").strip()
            if operation not in {"add", "reinforce", "reappraise", "resolve", "no_change"}:
                operation = "add"
            feeling = to_emotion or from_emotion
            if not feeling:
                logger.warning("漫想结算情绪未提供 feeling，跳过 sidecar activity=%s", activity_id[:120])
                return
            self_state = {
                "operation": operation,
                "feeling": feeling,
                "family": family,
                "reason": delta,
                "intensity": intensity,
                "persistence": persistence,
                # Keep the persisted runtime-effect vocabulary for existing
                # diagnostics while the Affect-facing fields above become
                # the canonical submission shape.
                "labels": [feeling] if feeling else [],
                "from": from_emotion,
                "to": to_emotion,
                "delta": delta,
                "description": delta,
                "confidence": confidence,
                **({"target": target} if target else {}),
                "source_activity_id": activity_id,
                "updated_at": now_iso(),
            }
            trace_id = service.trace_id() if hasattr(service, "trace_id") else ""
            submit_result = await asyncio.to_thread(
                service.submit_self_sidecar,
                source_type="wander_settlement",
                source_id=activity_id,
                self_state=self_state,
                trace_id=trace_id,
                reason=delta,
            )
            if not isinstance(submit_result, dict):
                logger.warning("漫想结算情绪 sidecar 返回异常 activity=%s result_type=%s", activity_id[:120], type(submit_result).__name__)
            elif submit_result.get("accepted") is False or submit_result.get("error"):
                diagnostic = str(submit_result.get("reason") or submit_result.get("error") or "rejected").replace("\n", " ")[:240]
                logger.warning("漫想结算情绪 sidecar 被拒绝 activity=%s reason=%s", activity_id[:120], diagnostic)
        except Exception as exc:
            logger.warning(
                "漫想结算情绪 sidecar 写入失败 activity=%s: %s",
                activity_id,
                type(exc).__name__,
            )
    def request_interrupt(self) -> Optional[dict]:
        self._interrupt_epoch += 1
        run = self.store.get_active_run()
        if run is None:
            # A user reply/stop also cancels a future plan wake when there is
            # no active run to interrupt.
            self.store.clear_scheduler_wake()
            self._suppress_next_schedule = False
            self.wake()
            return None
        self._suppress_next_schedule = True
        self.store.clear_scheduler_wake(str(run.get("session_id") or "") or None)
        activities = self.store.list_activities(run["run_id"])
        activity = next((item for item in activities if item["state"] not in {
            "planned", "completed", "interrupted", "aborted"
        }), None)
        if activity is None:
            self.wake()
            return None
        self.controller.request_interrupt(activity["activity_id"])
        nodes = self.store.list_nodes(activity["activity_id"])
        completed = [node for node in nodes if node["state"] == NodeState.COMPLETED.value]
        target = activity.get("goal_value")
        progress = f"已完成 {len(completed)}/{target}" if target else f"已完成 {len(completed)} 个节点"
        latest = completed[-1]["source_summary"] if completed else "还没有完成节点"
        self.wake()
        return {
            "run_id": run["run_id"],
            "activity_id": activity["activity_id"],
            "activity": strategy_for(EventType(activity["activity_type"])).display_name,
            "progress": progress,
            "latest": latest,
            "recent_nodes": [node["source_summary"] for node in completed[-3:]],
        }

    def _scheduler_delay(self, schedule: dict[str, Any]) -> float:
        try:
            due = datetime.fromisoformat(str(schedule.get("next_plan_at") or ""))
            return max(0.0, (due - self.clock()).total_seconds())
        except (TypeError, ValueError):
            # A malformed persisted wake is safer to execute immediately than
            # to strand the runner indefinitely.
            return 0.0

    def _schedule_next(
        self,
        *,
        session_id: str,
        delay_seconds: float,
        wake_reason: str,
        source_activity_id: str = "",
        source_run_id: str = "",
        failure_streak: int = 0,
    ) -> None:
        self.store.save_scheduler_wake(
            session_id=session_id,
            next_plan_at=(self.clock() + timedelta(seconds=max(0.05, delay_seconds))).isoformat(),
            wake_reason=wake_reason,
            source_activity_id=source_activity_id,
            source_run_id=source_run_id,
            failure_streak=failure_streak,
        )

    def _settlement_schedule(
        self,
        *,
        activity_id: str,
        session_id: str,
        reason: SettlementReason,
        continue_next: bool,
        next_inclination_note: str,
    ) -> tuple[float, str, int]:
        if reason == SettlementReason.EXECUTION_ERROR:
            previous = self._claimed_scheduler_wake or self.store.get_scheduler_wake(session_id)
            streak = 1
            if previous and previous.get("wake_reason") == "execution_error_backoff":
                previous_activity = self.store.get_activity(previous.get("source_activity_id") or "")
                current_activity = self.store.get_activity(activity_id)
                if previous_activity and current_activity and previous_activity.get("activity_type") == current_activity.get("activity_type"):
                    streak = int(previous.get("failure_streak") or 0) + 1
            index = min(max(streak, 1), len(_EXECUTION_FAILURE_WAKES)) - 1
            return _EXECUTION_FAILURE_WAKES[index], "execution_error_backoff", streak
        return DEFAULT_DELAY_SECONDS, "fallback_wait", 0

    def _seconds_until_due(self, action: RuntimeAction) -> float:
        activity = self.store.get_activity(action.activity_id or "")
        run = self.store.get_run(action.run_id or "")
        now = self.clock()
        due = []
        for value in (
            activity.get("next_node_at") if activity else None,
            activity.get("timer_ends_at") if activity else None,
            run.get("next_wake_at") if run else None,
        ):
            if value:
                due.append(max(0.05, (datetime.fromisoformat(value) - now).total_seconds()))
        return min(due) if due else min(60.0, self.default_interval_seconds)

    def _delay_until(self, timestamp: str) -> float:
        try:
            return max(0.05, (datetime.fromisoformat(timestamp) - self.clock()).total_seconds())
        except (TypeError, ValueError):
            return 0.05


    def _existing_node_review(self, node_id: str) -> Optional[NodeReviewDecision]:
        node = self.store.get_node(node_id)
        activity = self.store.get_activity(node["activity_id"]) if node else None
        if activity is None:
            return None
        for item in reversed(self.store.list_decisions(activity["run_id"], DecisionPhase.NODE_REVIEW)):
            if item["node_id"] != node_id or item["status"] != "ok":
                continue
            raw = item["parsed_output"].get("normalized") or {}
            effect = raw.get("emotion_effect") or {}
            return NodeReviewDecision(
                reflection=str(raw.get("reflection") or "没有感想"),
                emotion_effect=EmotionEffect(
                    changed=bool(effect.get("changed")),
                    from_emotion=str(effect.get("from_emotion") or effect.get("from") or ""),
                    to_emotion=str(effect.get("to_emotion") or effect.get("to") or ""),
                    delta=str(effect.get("delta") or ""),
                    confidence=float(effect.get("confidence") or 0.0),
                ),
                continue_activity=bool(raw.get("continue_activity", True)),
                abort_reason=str(raw.get("abort_reason") or ""),
                next_node_delay_seconds=raw.get("next_node_delay_seconds"),
                timing_reason=str(raw.get("timing_reason") or ""),
                timing_source=str(raw.get("timing_source") or "fallback"),
                next_node_at=str(raw.get("next_node_at") or ""),
                switch_to=self._restore_switch_to(raw, item),
            )
        return None

    def _existing_settlement(self, activity_id: str) -> Optional[SettlementDecision]:
        activity = self.store.get_activity(activity_id)
        if activity is None:
            return None
        for item in reversed(self.store.list_decisions(activity["run_id"], DecisionPhase.SETTLEMENT)):
            if item["activity_id"] != activity_id or not item["status"].startswith(("ok", "fallback_")):
                continue
            raw = item["parsed_output"].get("normalized") or {}
            effect = raw.get("emotion_effect") or {}
            return SettlementDecision(
                summary=str(raw.get("summary") or ""),
                share=bool(raw.get("share")),
                continue_next=bool(raw.get("continue_next")),
                next_inclination_note=str(raw.get("next_inclination_note") or ""),
                emotion_effect=EmotionEffect(
                    changed=bool(effect.get("changed")),
                    from_emotion=str(effect.get("from_emotion") or effect.get("from") or ""),
                    to_emotion=str(effect.get("to_emotion") or effect.get("to") or ""),
                    delta=str(effect.get("delta") or ""),
                    confidence=float(effect.get("confidence") or 0.0),
                ),
                next_run_delay_seconds=raw.get("next_run_delay_seconds"),
                timing_reason=str(raw.get("timing_reason") or ""),
                timing_source=str(raw.get("timing_source") or "fallback"),
                next_plan_at=str(raw.get("next_plan_at") or ""),
            )
        return None

    def _delivery_exists(self, activity_id: str) -> bool:
        return self.store.has_delivery_for_activity(activity_id)

    @staticmethod
    def _restore_switch_to(raw: dict, item: dict) -> Optional[tuple[EventType, str]]:
        value = raw.get("switch_to")
        if not isinstance(value, (list, tuple)) or not value:
            value = (item.get("parsed_output") or {}).get("raw", {}).get("switch_to")
            if isinstance(value, dict):
                value = (value.get("event_type"), value.get("reason", ""))
        if not isinstance(value, (list, tuple)) or not value:
            return None
        text = str(value[0]).removeprefix("EventType.").lower()
        try:
            return EventType(text), str(value[1] if len(value) > 1 else "")
        except ValueError:
            return None

    async def _review_horizon(self, action: RuntimeAction, bundle: RuntimeContextBundle) -> None:
        activity = self.store.get_activity(action.activity_id or "")
        run = self.store.get_run(action.run_id or "")
        if not activity or not run or activity.get("interrupt_requested"):
            await self._settle(RuntimeAction(RuntimeActionType.SETTLE, action.run_id, action.activity_id,
                reason=SettlementReason.USER_INTERRUPT), bundle)
            return
        expected = str(run.get("next_wake_at") or "")
        decision = await self.horizon_reviewer.review(action.activity_id or "", bundle.decision)
        # Never let an awaited LLM revive a run that was interrupted while it
        # was thinking.  Invalid/failing timing review is an honest horizon end.
        activity = self.store.get_activity(action.activity_id or "")
        if decision is None or not decision.continue_activity or not activity or activity.get("interrupt_requested"):
            await self._settle(RuntimeAction(RuntimeActionType.SETTLE, action.run_id, action.activity_id,
                reason=SettlementReason.USER_INTERRUPT if activity and activity.get("interrupt_requested") else SettlementReason.PLAN_HORIZON), bundle)
            return
        # A pure sleep timer and an external-signal activity do not acquire a
        # synthetic node merely because the model chose to keep the plan open.
        goal_mode = activity.get("goal_mode")
        event_type = activity.get("activity_type")
        due = None if event_type == EventType.SLEEP.value or goal_mode == GoalMode.EXTERNAL_SIGNAL.value else (
            datetime.fromisoformat(decision.next_node_at)
        )
        if not self.controller.apply_horizon_review(action.activity_id or "", expected,
                                                    extend_minutes=decision.extend_minutes,
                                                    next_node_at=due,
                                                    new_horizon=datetime.fromisoformat(decision.new_horizon)):
            # State changed while awaiting: poll again rather than inventing a
            # second extension or launching a node.
            return
