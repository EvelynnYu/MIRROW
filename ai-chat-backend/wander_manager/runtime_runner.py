"""Production orchestration for the persistent Wander runtime.

The runner is the single owner that consumes controller actions. It does not
send chat messages: settlement intent and actual delivery status remain
separate, so an unfinished share adapter cannot create a fake push.
"""

from __future__ import annotations

from .memory_fetch_result import empty_lookup

import asyncio
from .affect_bridge import persist_settlement_emotion
import hashlib
import inspect
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from body_admission import ordinary

logger = logging.getLogger(__name__)

_PLAN_FAILURE_WAKE_SECONDS = 300.0
_EXECUTION_FAILURE_WAKES = (900.0, 1800.0, 3600.0)
_PLANNER_TIMEOUT_SECONDS = 180.0



from .event_catalog import strategy_for
from .event_types import EventType
from .node_execution_adapter import NodeExecutionAdapter, NodeExecutionResult, NodeExecutionStatus
from .plan_decision_adapter import ActivitySpec, PlanDecision, PlanDecisionAdapter, RuntimeContext
from .runtime_controller import RuntimeAction, RuntimeActionType, WanderRuntimeController
from .runtime_decision_adapters import (
    DecisionContext, EmotionEffect, NodeReviewAdapter, NodeReviewDecision,
    SettlementAdapter, SettlementDecision, HorizonReviewAdapter,
    _normalize_effect,
)
from .runtime_models import (
    ActivityState, DecisionPhase, GoalMode, NodeState, RunState,
    SettlementReason, WanderActivity, WanderRun, now_iso,
)
from .runtime_store import WanderRuntimeStore
from .timing_policy import DEFAULT_DELAY_SECONDS, normalize_delay_seconds
from .goal_policy import normalize_extension
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
        for adapter in (self.reviewer, self.settlement, self.horizon_reviewer, self.self_reflection):
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
        self._install_opportunity_context_hooks()

    def _install_opportunity_context_hooks(self) -> None:
        """Only decisions that can choose an activity need opportunity facts."""
        adapters = (self.planner, self.reviewer)
        for adapter in adapters:
            if adapter is None:
                continue
            original = getattr(adapter, "context_builder", None)
            if getattr(original, "_opportunity_runtime_wrapper", False):
                continue

            async def _builder(recipe: str, *args: Any, _original=original, **kwargs: Any) -> Any:
                if recipe in {"WANDER_V2", "WANDER_ACTIVITY"} and "wander_runtime_text" in kwargs:
                    from .activity_opportunities import augment_runtime
                    kwargs["wander_runtime_text"] = augment_runtime(kwargs["wander_runtime_text"], self.store)
                if _original is not None:
                    result = _original(recipe, *args, **kwargs)
                    if inspect.isawaitable(result):
                        return await result
                    return result
                from context_builder.builder import ContextBuilder

                return await ContextBuilder.build(recipe, *args, **kwargs)

            _builder._opportunity_runtime_wrapper = True
            try:
                adapter.context_builder = _builder
            except Exception:
                logger.debug("unable to attach opportunity context wrapper to %s", type(adapter).__name__)

    def initialize(self) -> None:
        if self._initialized:
            return
        self.store.initialize()
        recovered = self.store.recover_stale_planning_runs(
            now=self.clock(),
            retry_delay_seconds=_PLAN_FAILURE_WAKE_SECONDS,
        )
        # This external idle entry is not driven by the optional Wander loop.
        # A process exit must not leave it blocking admission forever. Preserve
        # real node receipts; never replay an uncertain external write.
        stale = self.store.get_active_run()
        if (stale and stale.get('trigger_reason') == 'chat_tool'
                and (stale.get('context_snapshot') or {}).get('phone_tasks')):
            from .xhs_chat_runtime import recover_receipt
            recover_receipt(self, stale)
            stale = self.store.get_active_run()
        if stale and stale.get('trigger_reason') == 'xhs_interactions_idle':
            from .xhs_interaction_runtime import recover_idle_run
            recover_idle_run(self, stale)
            stale = self.store.get_active_run()
        if stale and stale.get('trigger_reason') == 'social_mentions_idle':
            for activity in self.store.list_activities(stale['run_id']):
                if activity['state'] in {'completed', 'interrupted', 'aborted'}:
                    continue
                for node in self.store.list_nodes(activity['activity_id']):
                    if node['state'] not in {'completed', 'failed', 'aborted'}:
                        self.controller.complete_node_review(node['node_id'],
                            reflection='', continue_activity=False,
                            abort_reason='process_exit', execution_failed=True)
                if self.share_adapter:
                    self.share_adapter.prepare_activity_event(activity['activity_id'])
                self.controller.settle_activity(activity['activity_id'], SettlementReason.USER_INTERRUPT,
                    summary='共域艾特查看因进程退出而中断；已保存节点回执，未确认的操作不重放。', share_decision=False)
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

    async def invite_game(self, game: str, persist_invitation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, str]:
        """Accept a deliberate UI invitation into the same durable activity pipeline."""
        from .cedartoy_client import GAMES, configured

        if game not in GAMES or not configured():
            raise ValueError("game_unavailable")
        self.initialize()
        async with self._tick_lock:
            bundle = await self.context_provider()
            session_id = bundle.plan.session_id
            if not session_id or not bundle.plan.persona.strip():
                raise RuntimeError("game_context_unavailable")
            if self.store.get_active_run(session_id=session_id):
                raise RuntimeError("wander_activity_busy")
            invitation = await persist_invitation()
            if invitation.get("session_id") != session_id:
                raise RuntimeError("game_session_changed")
            run = WanderRun(session_id=session_id, trigger_reason="ui_action:play_game",
                            inclination_text=f"人类伙伴邀请我玩{GAMES[game]}",
                            context_snapshot={"game_invitation": {
                                "game": game,
                                "invitation_message_id": invitation["invitation_message_id"],
                            }})
            plan = PlanDecision(
                activities=(ActivitySpec(EventType.PLAY_GAME, GoalMode.OPEN_ENDED, None,
                    reason=f"人类伙伴邀请 AI 玩{GAMES[game]}；AI 自己决定存档、选择和何时停下",
                    completion="estimate"),),
                horizon_min=30, horizon_mode="estimate", autonomous_goals=True,
                raw_plan={"source": "ui_action", "invited_game": game},
            )
            self.controller.apply_plan(run, plan)
            self.wake()
            return {"run_id": run.run_id, "activity_id": self.store.list_activities(run.run_id)[0]["activity_id"],
                    "invitation_message_id": invitation["invitation_message_id"]}

    async def settle_interrupt(self, activity_id: str) -> Optional[dict]:
        """Finish one already-requested user interruption without starting a new run.

        The manager owns this task independently of foreground chat waiting.
        It uses the same runner
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

    async def execute_chat_social(self, *, session_id: str, activity_reason: str = "",
                                  source_turn_id: str = "", initial_request: dict | None = None) -> dict:
        """One foreground common-circle visit, with durable nodes and no push."""
        return await self._execute_social_visit(session_id=session_id,activity_reason=activity_reason,
            source_turn_id=source_turn_id,initial_request=initial_request,trigger='chat_tool')

    async def execute_idle_social_mentions(self, rows: list[dict], batch_id: str) -> dict:
        """Non-waiting idle admission; same node/context/receipt chain as a visit."""
        import body_admission as focus
        from . import game_chat_gate
        if not rows:
            return {'status':'already_completed'}
        self.initialize()
        if self._tick_lock.locked() or self.store.get_active_run() or game_chat_gate.status()['busy']:
            return {'status':'busy'}
        try:
            owner = focus.reserve(require_idle=True, direction='social')
        except RuntimeError:
            return {'status':'busy'}
        try:
            with focus.owned(owner):
                bundle = await self.context_provider()
                first = rows[0]
                references = [{key: row.get(key) for key in
                    ('site_id','site_name','id','actor','actor_name','kind','moment_id','comment_id','created_at')}
                    for row in rows[:100]]
                return await self._execute_social_visit(session_id=bundle.decision.session_id,
                    activity_reason=f'共域有 {len(rows)} 条未处理艾特，来自 {len({r.get("site_id", "") for r in rows})} 家；空闲时查看实际内容。',
                    source_turn_id=batch_id, trigger='social_mentions_idle',
                    initial_request={'kind':'unread','site_id':first.get('site_id',''),
                                     'moment_id':first['moment_id'],'opening':True,
                                     'mention_references':references})
        finally:
            focus.release(owner)

    async def execute_idle_xhs_interactions(self, work: dict) -> dict:
        from .xhs_interaction_runtime import execute_idle
        return await execute_idle(self, work)

    async def _execute_social_visit(self, *, session_id: str, activity_reason: str,
                                    source_turn_id: str, initial_request: dict | None,
                                    trigger: str) -> dict:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "invalid_request", "error": "session_id_required"}
        from .social_circle_visit import MAX_NODES, activity_summary
        from social_feed import get_social_feed_store

        async with self._tick_lock:
            if source_turn_id:
                prefix = 'chat-social' if trigger == 'chat_tool' else 'social-mentions'
                identity = hashlib.sha256(f"{prefix}:{sid}:{source_turn_id}".encode()).hexdigest()
                existing = self.store.get_run(identity[:32])
                if existing:
                    prior = next(iter(self.store.list_activities(identity[:32])), None)
                    return {
                        "status": ("already_completed" if trigger == 'social_mentions_idle' and prior and
                                   prior.get('state') in {'completed','aborted','interrupted'} else
                                   "success" if prior and prior.get("state") == ActivityState.COMPLETED.value else "already_running"),
                        "summary": str((prior or {}).get("summary") or "")[:2000],
                        "run_id": identity[:32],
                        "activity_id": str((prior or {}).get("activity_id") or ""),
                        "source": trigger, "delivery_status": "not_requested",
                        'handled_mentions': self._social_handled_mentions(
                            self.store.list_nodes(prior['activity_id'])) if prior else [],
                    }
            # Context acquisition can yield to a deliberate invitation. Recheck
            # the durable activity authority inside the runner lock as well.
            if trigger == 'social_mentions_idle' and self.store.get_active_run():
                return {'status':'busy'}
            bundle = await self.context_provider()
            if bundle.decision.session_id != sid or not bundle.decision.persona:
                return {"status": "context_unavailable", "error": "social_context_unavailable"}
            now = self.clock()
            run = WanderRun(trigger_reason=trigger, session_id=sid,
                            plan_horizon_min=5,
                            next_wake_at=(now + timedelta(minutes=5)).isoformat())
            if source_turn_id:
                run.run_id = identity[:32]
            run.transition_to(RunState.ACTIVE_WAIT)
            activity = WanderActivity(
                activity_type=EventType.BROWSE_SOCIAL_FEED.value, run_id=run.run_id,
                goal_mode=GoalMode.OPEN_ENDED,
                reason=str(activity_reason or "人类伙伴邀请 AI 打开共友圈")[:300],
            )
            activity.transition_to(ActivityState.ACTIVE_WAIT)
            activity.next_node_at = now.isoformat()
            self.store.save_plan(run, [activity])
            self.store.record_delivery(run_id=run.run_id, activity_id=activity.activity_id,
                                       delivery_type=trigger, status="not_requested")
            barrier = ""
            cancelled = False
            for _ in range(MAX_NODES):
                node = None
                try:
                    node = self.controller.begin_node(activity.activity_id)
                    if node.round_index == 1 and initial_request:
                        node.source_payload = {'social_initial_request':dict(initial_request)}
                        self.store.save_node(node)
                    execution = await self.executor.execute(node.node_id, decision_context=bundle.decision)
                except asyncio.CancelledError:
                    cancelled, barrier = True, 'CancelledError'
                    if node is not None:
                        self.controller.complete_node_review(node.node_id,
                            reflection='',
                            continue_activity=False, abort_reason=barrier, execution_failed=True)
                    break
                except Exception as exc:
                    logger.error("chat social node execution: %s", type(exc).__name__)
                    barrier = type(exc).__name__
                    if node is not None:
                        self.controller.complete_node_review(
                            node.node_id, reflection="", continue_activity=False,
                            abort_reason=barrier, execution_failed=True,
                        )
                    break
                payload = execution.payload or {}
                if execution.status != NodeExecutionStatus.SUCCEEDED:
                    barrier = str(payload.get("status") or execution.error or execution.status.value)[:100]
                    self.controller.complete_node_review(
                        node.node_id, reflection="",
                        continue_activity=False, abort_reason=barrier, execution_failed=True,
                    )
                    break
                leave = bool(payload.get("exit"))
                self.controller.complete_node_review(
                    node.node_id, reflection=str(payload.get("reflection") or "")[:300],
                    continue_activity=not leave,
                    next_node_at=self.clock(),
                )
                if leave:
                    break
            nodes = self.store.list_nodes(activity.activity_id)
            try:
                summary = ('共域艾特查看被中断；已保存节点回执，未确认的操作不重放。' if cancelled else
                           await activity_summary(get_social_feed_store(), nodes))
            except asyncio.CancelledError:
                cancelled, barrier = True, 'CancelledError'
                summary = '共域艾特查看被中断；已保存节点回执，未确认的操作不重放。'
            except Exception as exc:
                barrier = type(exc).__name__
                summary = '共域活动摘要读取失败；实际节点及操作回执已保留。'
            reason = SettlementReason.USER_INTERRUPT if cancelled else SettlementReason.EXECUTION_ERROR if barrier else SettlementReason.NATURAL_STOP
            if trigger == 'social_mentions_idle' and self.share_adapter:
                self.share_adapter.prepare_activity_event(activity.activity_id)
            self.controller.settle_activity(
                activity.activity_id, reason, summary=summary, share_decision=False,
            )
            if trigger == 'social_mentions_idle':
                await persist_settlement_emotion(self.store, activity.activity_id, service=self.affect_service)
                if self.share_adapter:
                    await self.share_adapter.record_activity(activity.activity_id)
                if self.on_activity_settled:
                    self.on_activity_settled(EventType.BROWSE_SOCIAL_FEED)
            if cancelled:
                raise asyncio.CancelledError
            successes = sum(1 for row in nodes if row.get("execution_status") == NodeExecutionStatus.SUCCEEDED.value)
            return {
                "status": "success" if successes and not barrier else "partial_success" if successes else "failed",
                "summary": summary[:2000], "node_count": successes,
                "run_id": run.run_id, "activity_id": activity.activity_id,
                "barrier": barrier, "source": trigger, "delivery_status": "not_requested",
                'handled_mentions': self._social_handled_mentions(nodes),
            }

    @staticmethod
    def _social_handled_mentions(nodes: list[dict]) -> list[dict]:
        handled = {}
        for node in nodes:
            payload = node.get('source_payload') or {}
            site_id = (payload.get('scope') or {}).get('site_id','')
            for identifier in payload.get('read_notification_ids') or []:
                handled[(site_id,str(identifier))] = {'site_id':site_id, 'id':str(identifier)}
        return list(handled.values())

    async def execute_chat_xhs(self, *, session_id: str, instruction: str,
                               source_turn_id: str = '') -> dict:
        from .xhs_chat_runtime import execute_chat_task
        return await execute_chat_task(self, session_id=session_id, instruction=instruction,
                                       source_turn_id=source_turn_id)

    @ordinary
    async def tick(self, *, only_run_id: str = "") -> RunnerTickResult:
        async with self._tick_lock:
            if only_run_id:
                target = self.store.get_run(only_run_id)
                if not target or target.get("state") in {state.value for state in (RunState.COMPLETED, RunState.INTERRUPTED, RunState.ABORTED)}:
                    return RunnerTickResult(0.0, only_run_id, "invitation_finished")
            # The runner's clock remains authoritative even if an adapter was
            # replaced by the host (or by an isolated test fixture).
            for adapter in (self.reviewer, self.settlement, self.horizon_reviewer):
                if adapter is not None:
                    adapter.clock = self.clock
            bundle = await self.context_provider()
            session_id = bundle.plan.session_id
            if only_run_id and session_id != target.get("session_id"):
                return RunnerTickResult(15.0, only_run_id, "invitation_waiting_for_session")
            active = self.store.get_active_run(session_id=session_id)
            if (active and active.get('trigger_reason') == 'chat_tool'
                    and (active.get('context_snapshot') or {}).get('phone_tasks')):
                from .xhs_chat_runtime import recover_receipt
                recover_receipt(self, active)
                return RunnerTickResult(0.0, active['run_id'], 'chat_task_recovered')
            current_run_id = active["run_id"] if active else ""
            if active is None:
                if only_run_id:
                    return RunnerTickResult(0.0, only_run_id, "invitation_finished")
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
            inclination_note=(f"{pending['note']}\n来源：{pending['created_at']} 的结算想法，"
                              f"活动 {pending['source_activity_id']}。") if pending else bundle.plan.inclination_note,
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
            # Legacy only: new plans map defer to a short sleep activity in
            # PlanDecisionAdapter._normalize. Keep this branch so saved
            # checkpoints and older direct planners still recover correctly.
            if plan.action == 'defer':
                anchor = self.clock()
                delay, source = normalize_delay_seconds(plan.defer_seconds, now=anchor)
                if source != 'model':
                    raise ValueError('invalid_defer_delay')
                due = (anchor + timedelta(seconds=delay)).isoformat()
                # The accepted absolute due is a resumable checkpoint; the
                # following transaction also consumes this run's note claim.
                self.store.record_decision(run_id=run.run_id, phase=DecisionPhase.PLAN,
                    model='', recipe='WANDER_V2', status='accepted_defer',
                    input_context={'scope': 'accepted scheduler wait; no activity executed'},
                    parsed_output={'normalized': {'action': 'defer', 'next_plan_at': due,
                        'accepted_at': anchor.isoformat(), 'defer_reason': plan.defer_reason}})
                if not self.store.accept_deferred_plan(run.run_id, next_plan_at=due,
                        accepted_at=anchor.isoformat(), reason=plan.defer_reason):
                    raise ValueError('defer_run_not_planning')
                return RunnerTickResult(delay, run.run_id, 'plan_deferred')
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
                closeout_ready=decision.closeout is not None,
            )
            return None

        execution = await self.executor.execute(node.node_id, decision_context=bundle.decision)
        refreshed_activity = self.store.get_activity(node.activity_id)
        if execution.status == NodeExecutionStatus.WAITING_EXTERNAL:
            due = execution.side_effect_refs.get("expected_completion_at")
            delay = self._delay_until(due) if due else 60.0
            return RunnerTickResult(delay, action.run_id or "", "waiting_external")
        memory_empty = empty_lookup({'execution_status': execution.status.value, 'source_payload': execution.payload})
        if execution.status == NodeExecutionStatus.SKIPPED and not memory_empty:
            # A truthful capability gate or natural material exhaustion is a
            # completed bounded node, not an execution error.  Keep the node's
            # ``skipped`` evidence while letting settlement classify the
            # activity as a natural stop instead of FAILED/EXECUTION_ERROR.
            self.controller.complete_node_review(
                node.node_id,
                reflection='',
                continue_activity=False,
                abort_reason=execution.error or "node_skipped",
                execution_failed=False,
            )
            return None
        if execution.status != NodeExecutionStatus.SUCCEEDED and not memory_empty:
            self.controller.complete_node_review(
                node.node_id,
                reflection='',
                continue_activity=False,
                abort_reason=execution.error or execution.status.value,
                execution_failed=True,
            )
            return None
        if event_type == EventType.CHECK_XIAOHONGSHU_INTERACTIONS:
            self.controller.complete_node_review(node.node_id,
                reflection=(execution.payload or {}).get('reflection', ''), continue_activity=False)
            return None
        if event_type == EventType.BROWSE_SOCIAL_FEED:
            payload = execution.payload or {}
            self.controller.complete_node_review(
                node.node_id,
                reflection=str(payload.get("reflection") or "")[:300],
                continue_activity=not bool(payload.get("exit")),
                next_node_at=self.clock() + timedelta(seconds=1),
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
                reflection="",
                continue_activity=False,
                abort_reason="node_review_unavailable",
                execution_failed=False,
            )
            return None
        await self._apply_music_library_action(node.node_id, review, refreshed_activity)
        self._apply_xhs_post_action(node.node_id, review, refreshed_activity)
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
        if activity['activity_type'] == EventType.CHECK_XIAOHONGSHU_INTERACTIONS.value:
            from .xhs_interaction_runtime import recover_node
            payload = recover_node(self.store, node)
            self.controller.complete_node_review(node['node_id'], reflection=payload.get('reflection', ''),
                continue_activity=False, abort_reason='interaction_recovered_without_replay',
                execution_failed=payload.get('status') != 'completed')
            return None
        if (activity['activity_type'] == EventType.PUBLISH_XIAOHONGSHU.value
                and node['execution_status'] == NodeExecutionStatus.RUNNING.value):
            self.executor.recover_publication(node['node_id'])
            node = self.store.get_node(node['node_id'])
        if activity["interrupt_requested"]:
            execution_complete = (
                node.get("execution_status") == NodeExecutionStatus.SUCCEEDED.value
                or empty_lookup(node)
            )
            saved_review = self._existing_node_review(node["node_id"])
            reflection = saved_review.reflection if saved_review else str(node.get("reflection") or "")
            if not reflection and execution_complete:
                payload = node.get("source_payload") or {}
                if activity["activity_type"] == EventType.SELF_REFLECTION.value:
                    reflection = str(payload.get("overall_reflection") or "")
                elif activity["activity_type"] == EventType.BROWSE_SOCIAL_FEED.value:
                    reflection = str(payload.get("reflection") or "")[:300]
            self.controller.complete_node_review(
                node["node_id"],
                reflection=reflection,
                account_source='k_node_review' if saved_review else '',
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
            receipt = await self.executor.poll_external_completion(node["node_id"])
            if receipt.status == NodeExecutionStatus.WAITING_EXTERNAL:
                slow_states = {"playback_paused", "playback_unknown", "playback_unavailable"}
                delay = 30.0 if receipt.completion_signal in slow_states else 5.0
                return RunnerTickResult(delay, action.run_id or "", "waiting_external")
            node = self.store.get_node(node["node_id"])
        if node["execution_status"] == NodeExecutionStatus.SUCCEEDED.value or empty_lookup(node):
            if event_type == EventType.BROWSE_SOCIAL_FEED:
                payload = node.get("source_payload") or {}
                self.controller.complete_node_review(
                    node["node_id"],
                    reflection=str(payload.get("reflection") or "")[:300],
                    continue_activity=not bool(payload.get("exit")),
                    next_node_at=self.clock() + timedelta(seconds=1),
                )
                return None
            if event_type == EventType.SELF_REFLECTION:
                wish_results = self.wish_commit.commit(node["node_id"])
                await self._notify_wish_board_if_needed(
                    bundle.plan.session_id, node["node_id"], wish_results
                )
                self.controller.complete_node_review(
                    node["node_id"],
                    reflection=str(node["source_payload"].get("overall_reflection") or ""),
                    continue_activity=False,
                    closeout_ready=isinstance(node['source_payload'].get('closeout'), dict),
                )
                return None
            review = self._existing_node_review(node["node_id"])
            if review is None:
                review = await self.reviewer.review(
                    node["node_id"], bundle.decision,
                    allowed_event_types=self.planner.effective_allowed_event_types(),
                )
            if review is not None:
                await self._apply_music_library_action(node["node_id"], review, activity)
                self._apply_xhs_post_action(node["node_id"], review, activity)
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
                reflection="",
                continue_activity=False,
                abort_reason="node_review_unavailable",
                execution_failed=False,
            )
            return None
        if node["execution_status"] == NodeExecutionStatus.SKIPPED.value:
            self.controller.complete_node_review(
                node["node_id"],
                reflection='',
                continue_activity=False,
                abort_reason=node.get("execution_error") or "node_skipped",
                execution_failed=False,
            )
            return None
        self.controller.complete_node_review(
            node["node_id"],
            reflection='',
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
            title = str(payload.get("title") or "").strip()
            artist = str(payload.get("artist") or "").strip()
            if (not title or title.casefold() in {"未知", "unknown", "?"}
                    or not artist or artist.casefold() in {"未知", "unknown", "?"}
                    or not payload.get("fingerprint")):
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

    async def _apply_music_library_action(
        self,
        node_id: str,
        review: NodeReviewDecision,
        activity: dict,
    ) -> None:
        """Execute one reviewed AI-library action and persist its real receipt."""
        if not activity or activity.get("activity_type") != EventType.LISTEN_MUSIC.value:
            return
        stored = self.store.get_node(node_id) or {}
        payload = stored.get("source_payload") or {}
        try:
            if review.music_library_action:
                from music_system.library import apply_wander_action
                receipt = await apply_wander_action(
                    node_id, payload, review.music_library_action, review.reflection,
                )
            elif review.collect_music:
                # Backward-compatible recovery for reviews created before the
                # structured action contract was introduced.
                from music_system.library import collect
                receipt = await collect(node_id, payload, review.reflection)
            else:
                return
        except Exception as exc:
            receipt = {
                "status": "error", "action": "unknown",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
        self.store.merge_node_side_effect_refs(
            node_id, {"music_library_action": receipt},
        )

    def _apply_xhs_post_action(self, node_id: str, review: NodeReviewDecision, activity: dict) -> None:
        """Commit AI's explicit local keep decision once, with a recoverable receipt."""
        if not review.keep_post or not activity or activity.get("activity_type") != EventType.BROWSE_XIAOHONGSHU.value:
            return
        try:
            from xhs_posts.store import XhsPostStore
            action = XhsPostStore(self.store.db_path).save_from_node(
                node_id, reason=review.keep_post_reason or review.reflection,
                idempotency_key="wander-keep:" + node_id,
            )
            receipt = {"status": "completed", "action_id": action["action_id"],
                       "post_key": action["post_key"], "changed": bool(action["changed"])}
        except Exception as exc:
            receipt = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        self.store.merge_node_side_effect_refs(node_id, {"xhs_post_action": receipt})

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
        if (review.continue_activity or review.switch_to is not None) and not review.abort_reason:
            delay, _ = normalize_delay_seconds(review.next_node_delay_seconds)
            next_node_at = datetime.fromisoformat(review.next_node_at) if review.next_node_at else self.clock() + timedelta(seconds=delay)
        self.controller.complete_node_review(
            node_id,
            reflection=review.reflection,
            account_source='k_node_review',
            emotion_effect=review.emotion_effect.to_payload(),
            continue_activity=review.continue_activity,
            abort_reason=review.abort_reason,
            next_node_at=next_node_at,
            switch_to=review.switch_to,
            extend_count=review.extend_count,
            goal_reason=review.goal_reason,
            switch_goal=review.switch_goal,
            goal_assessment=review.goal_assessment,
            closeout_ready=review.closeout is not None,
        )

    async def _review_horizon(self, action: RuntimeAction, bundle: RuntimeContextBundle) -> None:
        activity = self.store.get_activity(action.activity_id or "")
        run = self.store.get_run(action.run_id or "")
        if not activity or not run or activity.get("interrupt_requested"):
            await self._settle(RuntimeAction(RuntimeActionType.SETTLE, action.run_id, action.activity_id,
                reason=(self.controller.requested_stop_reason(action.activity_id or "")
                        if activity and activity.get("interrupt_requested") else SettlementReason.USER_INTERRUPT)), bundle)
            return
        expected = str(run.get("next_wake_at") or "")
        decision = await self.horizon_reviewer.review(action.activity_id or "", bundle.decision)
        # Never let an awaited LLM revive a run that was interrupted while it
        # was thinking.  Invalid/failing timing review is an honest horizon end.
        activity = self.store.get_activity(action.activity_id or "")
        if decision is None or not decision.continue_activity or not activity or activity.get("interrupt_requested"):
            await self._settle(RuntimeAction(RuntimeActionType.SETTLE, action.run_id, action.activity_id,
                reason=(self.controller.requested_stop_reason(action.activity_id or "")
                        if activity and activity.get("interrupt_requested") else SettlementReason.PLAN_HORIZON)), bundle)
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
            from .review_closeout import from_closing_review
            decision = from_closing_review(self.store, activity_id, reason)
        activity_type = (self.store.get_activity(activity_id) or {}).get("activity_type")
        if decision is None and activity_type == EventType.CHECK_XIAOHONGSHU_INTERACTIONS.value:
            from .xhs_interaction_runtime import summary
            decision = SettlementDecision(summary=summary(self.store.list_nodes(activity_id)),
                                          share=False, continue_next=False, emotion_effect=EmotionEffect())
        if decision is None and activity_type == EventType.BROWSE_SOCIAL_FEED.value and not chat_tool_run:
            from .social_circle_visit import activity_summary
            from social_feed import get_social_feed_store
            nodes = self.store.list_nodes(activity_id)
            summary = await activity_summary(get_social_feed_store(), nodes)
            last_payload = next((row.get("source_payload") or {} for row in reversed(nodes)
                                 if row.get("execution_status") == NodeExecutionStatus.SUCCEEDED.value), {})
            decision = SettlementDecision(
                summary=summary, share=bool(last_payload.get("share_intent")),
                continue_next=False, emotion_effect=EmotionEffect(),
            )
        if decision is None and not chat_tool_run:
            decision = await self.settlement.settle(activity_id, reason, bundle.decision)
        latest = self.store.get_activity(activity_id) or {}
        if self._interrupt_epoch != interrupt_epoch:
            reason = SettlementReason.USER_INTERRUPT
        elif latest.get("interrupt_requested"):
            reason = self.controller.requested_stop_reason(activity_id) or SettlementReason.USER_INTERRUPT
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
        elif reason == SettlementReason.GAME_STOP:
            decision.continue_next = False
            decision.next_inclination_note = ""
        from .settlement_outcome import constrain_unexecuted
        constrain_unexecuted(decision, latest, self.store.list_nodes(activity_id), reason)
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
        if reason in (SettlementReason.USER_INTERRUPT, SettlementReason.GAME_STOP) or chat_tool_run:
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
        if self.share_adapter is not None and not chat_tool_run:
            self.share_adapter.prepare_activity_event(activity_id)
        result = self.controller.settle_activity(
            activity_id,
            reason,
            summary=decision.summary,
            emotion_effect=decision.emotion_effect.to_payload(),
            continue_next=decision.continue_next,
            share_decision=decision.share,
            next_inclination_note=decision.next_inclination_note,
        )
        # Settlement is already committed before this optional sidecar.  A
        # provider/storage failure here is diagnostic-only and must not reopen
        # or otherwise change the settled activity.
        activity = self.store.get_activity(activity_id)
        await persist_settlement_emotion(self.store, activity_id, service=self.affect_service)
        if self._interrupt_epoch != interrupt_epoch:
            decision.share = False
            decision.continue_next = False
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
        if self.share_adapter is not None and not chat_tool_run:
            try:
                if (not await self.share_adapter.record_activity(activity_id)
                        and self.store.get_activity_event_projection(activity_id)):
                    logger.warning("漫想活动事件尚未确认写入，保留待恢复记录: %s", activity_id)
            except Exception:
                logger.exception("漫想活动事件未能写入对话时间线: %s", activity_id)
        if status == "deferred" and self.share_adapter is not None:
            await self.share_adapter.deliver(activity_id)
        activity = self.store.get_activity(activity_id)
        if activity and self.on_activity_settled:
            self.on_activity_settled(EventType(activity["activity_type"]))
        if result.action == RuntimeActionType.WAIT:
            self.wake()

    def request_interrupt(self, *, source: str = '') -> Optional[dict]:
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
        self.controller.request_interrupt(activity["activity_id"], source=source)
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
        from .activity_purpose import normalize_outcome
        from .review_closeout import restore_closeout
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
                reflection=str(raw.get("reflection") or ""),
                purpose_outcome=normalize_outcome(raw.get('purpose_outcome')),
                collect_music=bool(raw.get("collect_music")),
                music_library_action=(
                    dict(raw.get("music_library_action"))
                    if isinstance(raw.get("music_library_action"), dict) else None
                ),
                keep_post=bool(raw.get("keep_post")),
                keep_post_reason=str(raw.get("keep_post_reason") or ""),
                emotion_effect=_normalize_effect(effect),
                continue_activity=bool(raw.get("continue_activity", True)),
                abort_reason=str(raw.get("abort_reason") or ""),
                next_node_delay_seconds=raw.get("next_node_delay_seconds"),
                timing_reason=str(raw.get("timing_reason") or ""),
                timing_source=str(raw.get("timing_source") or "fallback"),
                next_node_at=str(raw.get("next_node_at") or ""),
                switch_to=self._restore_switch_to(raw, item),
                extend_count=normalize_extension(raw.get("extend_count")),
                goal_reason=str(raw.get("goal_reason") or ""),
                switch_goal=raw.get("switch_goal") if isinstance(raw.get("switch_goal"), dict) else None,
                goal_assessment=raw.get('goal_assessment') if isinstance(raw.get('goal_assessment'), dict) else None,
                closeout=restore_closeout(raw.get('closeout')),
            )
        return None

    def _existing_settlement(self, activity_id: str) -> Optional[SettlementDecision]:
        from .activity_purpose import normalize_outcome
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
                purpose_outcome=normalize_outcome(raw.get('purpose_outcome')),
                retained_impression=str(raw.get('retained_impression') or ''),
                retained_node_ids=list(raw.get('retained_node_ids') or []),
                share=bool(raw.get("share")),
                continue_next=bool(raw.get("continue_next")),
                next_inclination_note=str(raw.get("next_inclination_note") or ""),
                emotion_effect=_normalize_effect(effect),
                next_run_delay_seconds=raw.get("next_run_delay_seconds"),
                timing_reason=str(raw.get("timing_reason") or ""),
                timing_source=str(raw.get("timing_source") or "fallback"),
                next_plan_at=str(raw.get("next_plan_at") or ""),
                xhs_share_node_id=str(raw.get("xhs_share_node_id") or ""),
                xhs_comment_draft=str(raw.get("xhs_comment_draft") or ""),
            )
        return None

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

    def _delivery_exists(self, activity_id: str) -> bool:
        return self.store.has_delivery_for_activity(activity_id)
