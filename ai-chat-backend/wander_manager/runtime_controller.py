"""Persistent, side-effect-free scheduler for the new Wander runtime.

It decides *what should happen next*.  Handler execution, LLM calls, push
delivery, and production scheduler ownership intentionally remain outside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from .event_catalog import strategy_for
from .event_types import EventType
from .goal_policy import get_activity_goal_policy, is_autonomous_run, normalize_switch_goal, normalize_extension
from .plan_decision_adapter import PlanDecision
from .runtime_models import (ActivityState, GoalMode, NodeState, RunState,
    SettlementReason, WanderActivity, WanderNode, WanderRun)
from .runtime_store import WanderRuntimeStore


class RuntimeActionType(str, Enum):
    WAIT = "wait"
    START_NODE = "start_node"
    SETTLE = "settle"
    RECOVER_NODE = "recover_node"
    REVIEW_TIMING = "review_timing"
    IDLE = "idle"


@dataclass(frozen=True)
class RuntimeAction:
    action: RuntimeActionType
    run_id: Optional[str] = None
    activity_id: Optional[str] = None
    node_id: Optional[str] = None
    reason: Optional[SettlementReason] = None


class WanderRuntimeController:
    def __init__(self, store: WanderRuntimeStore, clock: Callable[[], datetime] = datetime.now,
                 max_open_nodes: int = 8):
        self.store, self.clock = store, clock
        self.max_open_nodes = max(1, max_open_nodes)

    def apply_plan(self, run: WanderRun, plan: PlanDecision) -> list[WanderActivity]:
        """Persist the accepted plan; first activity waits, remaining ones stay planned."""
        if run.state != RunState.PLANNING:
            raise ValueError("only a planning run can accept a plan")
        if not plan.activities:
            raise ValueError("plan has no activities")
        for spec in plan.activities:
            strategy = strategy_for(spec.event_type)
            if spec.goal_mode not in strategy.allowed_goal_modes:
                raise ValueError(f"unsupported goal mode for {spec.event_type.value}")
        now = self.clock()
        activities: list[WanderActivity] = []
        for index, spec in enumerate(plan.activities):
            activity = WanderActivity(activity_type=spec.event_type.value, run_id=run.run_id,
                order_index=index, goal_mode=spec.goal_mode, goal_value=spec.goal_value, reason=spec.reason)
            if index == 0:
                activity.transition_to(ActivityState.ACTIVE_WAIT)
                self._schedule_initial(activity, now)
            activities.append(activity)
        run.plan_horizon_min = plan.horizon_min
        run.next_wake_at = self._iso(now + timedelta(minutes=plan.horizon_min))
        # This is intentionally snapshot data rather than a schema migration:
        # old in-flight runs have no key and therefore retain deadline semantics.
        run.context_snapshot = dict(run.context_snapshot or {})
        run.context_snapshot["timing"] = {"horizon_mode": plan.horizon_mode}
        planned_xhs = {item.activity_id: {'version': 1, 'spec': spec.xhs_task, 'initial_cursor': {}}
                       for item, spec in zip(activities, plan.activities) if spec.xhs_task}
        if planned_xhs:
            run.context_snapshot['phone_tasks'] = {**dict(run.context_snapshot.get('phone_tasks') or {}),
                                                   **planned_xhs}
        if plan.autonomous_goals and run.trigger_reason != "chat_tool":
            run.context_snapshot["autonomy"] = {
                "version": 1,
                "activity_goal_policies": {
                    item.activity_id: {
                        "completion": spec.completion,
                        "initial_goal_value": item.goal_value,
                        "extensions_total": 0,
                        "reason": spec.reason,
                    }
                    for item, spec in zip(activities, plan.activities)
                },
            }
        run.transition_to(RunState.ACTIVE_WAIT)
        self.store.save_plan(run, activities)
        return activities

    def poll(self, run_id: Optional[str] = None, session_id: Optional[str] = None) -> RuntimeAction:
        run_row = self.store.get_run(run_id) if run_id else self.store.get_active_run(session_id)
        if not run_row:
            return RuntimeAction(RuntimeActionType.IDLE)
        run = self._run(run_row)
        activities = [self._activity(item) for item in self.store.list_activities(run.run_id)]
        active = next((item for item in activities if item.state not in (ActivityState.PLANNED, ActivityState.COMPLETED, ActivityState.INTERRUPTED, ActivityState.ABORTED)), None)
        if active is None:
            return RuntimeAction(RuntimeActionType.IDLE, run_id=run.run_id)
        if active.state == ActivityState.SETTLING:
            return RuntimeAction(
                RuntimeActionType.SETTLE,
                run.run_id,
                active.activity_id,
                reason=self._requested_stop_reason(run, active) or active.settlement_reason or SettlementReason.NATURAL_STOP,
            )
        nodes = [self._node(item) for item in self.store.list_nodes(active.activity_id)]
        in_flight = next((node for node in reversed(nodes) if node.state in (NodeState.RUNNING, NodeState.REVIEWED)), None)
        if in_flight is not None or active.state in (ActivityState.NODE_RUNNING, ActivityState.NODE_REVIEW):
            # Never replay an external action after a process restart; caller must recover/review it.
            return RuntimeAction(RuntimeActionType.RECOVER_NODE, run.run_id, active.activity_id,
                in_flight.node_id if in_flight else None,
                self._requested_stop_reason(run, active))
        if active.interrupt_requested:
            return RuntimeAction(RuntimeActionType.SETTLE, run.run_id, active.activity_id,
                                 reason=self._requested_stop_reason(run, active))
        now = self.clock()
        if active.timer_ends_at and now >= self._parse(active.timer_ends_at):
            return RuntimeAction(RuntimeActionType.SETTLE, run.run_id, active.activity_id, reason=SettlementReason.TIMER_ENDED)
        if run.next_wake_at and now >= self._parse(run.next_wake_at):
            if self._horizon_mode(run) == "estimate":
                return RuntimeAction(RuntimeActionType.REVIEW_TIMING, run.run_id, active.activity_id, reason=SettlementReason.PLAN_HORIZON)
            return RuntimeAction(RuntimeActionType.SETTLE, run.run_id, active.activity_id, reason=SettlementReason.PLAN_HORIZON)
        if active.next_node_at and now >= self._parse(active.next_node_at):
            return RuntimeAction(RuntimeActionType.START_NODE, run.run_id, active.activity_id)
        if active.goal_mode == GoalMode.EXTERNAL_SIGNAL:
            return RuntimeAction(RuntimeActionType.WAIT, run.run_id, active.activity_id)
        return RuntimeAction(RuntimeActionType.WAIT, run.run_id, active.activity_id)

    def signal_external(self, activity_id: str, next_node_at: Optional[datetime] = None) -> None:
        """An external system confirms a real signal; this only schedules a node."""
        activity = self._activity_required(activity_id)
        if activity.goal_mode != GoalMode.EXTERNAL_SIGNAL or activity.state != ActivityState.ACTIVE_WAIT:
            raise ValueError("activity is not waiting for an external signal")
        activity.next_node_at = self._iso(next_node_at or self.clock())
        self.store.save_activity(activity)

    def apply_horizon_review(self, activity_id: str, expected_horizon: str, *, extend_minutes: int,
                             next_node_at: Optional[datetime], new_horizon: Optional[datetime] = None) -> bool:
        """Atomic state update guarded by the old absolute horizon.

        The decision audit is the idempotency key.  A restarted runner cannot
        add the same extension twice because its expected horizon no longer
        matches.
        """
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        if activity.interrupt_requested or activity.state != ActivityState.ACTIVE_WAIT:
            return False
        if run.next_wake_at != expected_horizon or self._horizon_mode(run) != "estimate":
            return False
        try:
            minutes = int(extend_minutes)
            if minutes <= 0:
                return False
            # Extension starts from the real review moment, never from a
            # stale planned estimate after a process was asleep/offline.
            new_horizon = new_horizon or (self.clock() + timedelta(minutes=minutes))
            if new_horizon <= self._parse(expected_horizon):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        run.next_wake_at = self._iso(new_horizon)
        if activity.activity_type == "sleep":
            activity.next_node_at = None
        elif activity.goal_mode != GoalMode.EXTERNAL_SIGNAL:
            activity.next_node_at = self._iso(next_node_at) if next_node_at else None
        self.store.save_plan(run, [activity])
        return True

    def begin_node(self, activity_id: str) -> WanderNode:
        activity = self._activity_required(activity_id)
        if activity.state != ActivityState.ACTIVE_WAIT or activity.interrupt_requested:
            raise ValueError("activity cannot start a node")
        now = self.clock()
        if activity.next_node_at and now < self._parse(activity.next_node_at):
            raise ValueError("next node is not due")
        if activity.timer_ends_at and now >= self._parse(activity.timer_ends_at):
            raise ValueError("activity timer has ended")
        run = self._run_required(activity.run_id)
        if run.next_wake_at and now >= self._parse(run.next_wake_at):
            raise ValueError("plan horizon has ended")
        if activity.goal_mode == GoalMode.EXTERNAL_SIGNAL and not activity.next_node_at:
            raise ValueError("external activity requires an explicit signal")
        nodes = [self._node(item) for item in self.store.list_nodes(activity_id)]
        node = WanderNode(activity_id=activity_id, round_index=len(nodes) + 1)
        node.transition_to(NodeState.RUNNING)
        node.started_at = self._iso(now)
        activity.transition_to(ActivityState.NODE_RUNNING)
        run.transition_to(RunState.NODE_RUNNING)
        self.store.save_node(node)
        self.store.save_activity(activity)
        self.store.save_run(run)
        return node

    def complete_node_review(self, node_id: str, *, reflection: str = "", emotion_effect: Optional[dict] = None,
                             continue_activity: bool = True, abort_reason: str = "",
                             next_node_at: Optional[datetime] = None,
                             execution_failed: bool = False,
                             switch_to: Optional[tuple[EventType, str]] = None,
                             extend_count: int = 0,
                             goal_reason: str = "",
                             switch_goal: Optional[dict] = None,
                             account_source: str = "",
                             goal_assessment: Optional[dict] = None,
                             closeout_ready: bool = False) -> RuntimeAction:
        node = self._node_required(node_id)
        if node.state != NodeState.RUNNING:
            raise ValueError("only a running node can be reviewed")
        activity = self._activity_required(node.activity_id)
        run = self._run_required(activity.run_id)
        node.reflection, node.emotion_effect = reflection, dict(emotion_effect or {})
        if node.source_payload.get('checkup_version') == 1 and goal_assessment is not None:
            from checkup_assessment import normalize_goal_assessment, source_status
            node.source_payload = dict(node.source_payload)
            items = node.source_payload.get('checkup_items') or []
            node.source_payload.update(source_status=source_status(items),
                goal_assessment=normalize_goal_assessment(goal_assessment, items))
        if account_source == 'k_node_review':
            node.side_effect_refs = dict(node.side_effect_refs or {})
            node.side_effect_refs['reflection_source'] = account_source
        node.continue_activity, node.abort_reason = continue_activity, abort_reason
        if execution_failed:
            node.transition_to(NodeState.ABORTED)
        else:
            node.transition_to(NodeState.REVIEWED)
            node.transition_to(NodeState.COMPLETED)
        node.completed_at = self._iso(self.clock())
        activity.transition_to(ActivityState.NODE_REVIEW)
        run.transition_to(RunState.NODE_REVIEW)
        now = self.clock()
        policy = get_activity_goal_policy(run, activity)
        autonomous_goal = is_autonomous_run(run) and policy.get("completion") == "estimate"
        count_reached = activity.goal_mode in (GoalMode.COUNT, GoalMode.SINGLE) and node.round_index >= (activity.goal_value or 1)
        timer_done = bool(activity.timer_ends_at and now >= self._parse(activity.timer_ends_at))
        horizon_done = bool(run.next_wake_at and now >= self._parse(run.next_wake_at) and self._horizon_mode(run) == "deadline")
        if (activity.goal_mode == GoalMode.COUNT and count_reached and autonomous_goal
                and not activity.interrupt_requested and not execution_failed and not timer_done and not horizon_done):
            extension = normalize_extension(extend_count)
            if continue_activity and not abort_reason and extension:
                if node.round_index > 2**63 - 1 - extension:
                    extension = 0
                else:
                    activity.goal_value = node.round_index + extension
                    policies = run.context_snapshot["autonomy"]["activity_goal_policies"]
                    policy = dict(policies.get(activity.activity_id) or policy)
                    policy["extensions_total"] = int(policy.get("extensions_total") or 0) + extension
                    policy["reason"] = goal_reason
                    policies[activity.activity_id] = policy
                    count_reached = False
        open_limit = activity.goal_mode == GoalMode.OPEN_ENDED and not autonomous_goal and node.round_index >= self.max_open_nodes
        # An estimate is reviewed at a node boundary by the runner.  It is not
        # a hidden deadline; explicit activity timers remain authoritative.
        if activity.interrupt_requested:
            stop_reason = self._requested_stop_reason(run, activity)
            self._move_to_settling(run, activity, stop_reason, save=False)
            self.store.save_plan(run, [activity], reviewed_node=node)
            return RuntimeAction(RuntimeActionType.SETTLE, run.run_id, activity.activity_id,
                                 reason=stop_reason)
        if execution_failed or abort_reason or not continue_activity or count_reached or open_limit or timer_done or horizon_done:
            reason = (
                SettlementReason.EXECUTION_ERROR if execution_failed else
                SettlementReason.TIMER_ENDED if timer_done else
                SettlementReason.PLAN_HORIZON if horizon_done else
                SettlementReason.GOAL_REACHED if count_reached else
                SettlementReason.NATURAL_STOP
            )
            # 判定时换活动：AI 在 review 明确要换去做另一件事时，现场创建续作活动
            # （只响应 AI 的主动自然停止；中断/失败/带 abort_reason 不换）
            if (
                switch_to is not None
                and not activity.interrupt_requested
                and not execution_failed
                and not abort_reason
                and not horizon_done
            ):
                successor = self._add_switch_activity(run, switch_to, switch_goal, next_node_at)
            else:
                successor = None
            self._move_to_settling(run, activity, reason, save=False)
            if closeout_ready and not execution_failed:
                node.side_effect_refs = dict(node.side_effect_refs or {})
                node.side_effect_refs['closeout_from_review'] = True
            self.store.save_plan(run, [activity, *([successor] if successor else [])], reviewed_node=node)
            return RuntimeAction(RuntimeActionType.SETTLE, run.run_id, activity.activity_id, reason=reason)
        activity.transition_to(ActivityState.ACTIVE_WAIT)
        run.transition_to(RunState.ACTIVE_WAIT)
        # The executor chooses when the next real node may begin; no invented duration here.
        activity.next_node_at = self._iso(next_node_at) if next_node_at else None
        self.store.save_plan(run, [activity], reviewed_node=node)
        return RuntimeAction(RuntimeActionType.WAIT, run.run_id, activity.activity_id)

    def _add_switch_activity(self, run: WanderRun, switch_to: tuple[EventType, str], switch_goal: Optional[dict] = None,
                             next_node_at: Optional[datetime] = None) -> WanderActivity:
        """判定换活动：现场创建一个 PLANNED 续作活动，settle 后由 settle_activity 激活。"""
        event_type, reason = switch_to
        strategy = strategy_for(event_type)
        activities = [self._activity(item) for item in self.store.list_activities(run.run_id)]
        next_idx = max((a.order_index for a in activities), default=0) + 1
        goal_mode, goal_value, completion = normalize_switch_goal(event_type, switch_goal or {}, autonomous=is_autonomous_run(run))
        activity = WanderActivity(
            activity_type=event_type.value,
            run_id=run.run_id,
            order_index=next_idx,
            goal_mode=goal_mode,
            goal_value=goal_value,
            reason=reason,
            next_node_at=self._iso(next_node_at) if next_node_at else None,
        )
        activity.transition_to(ActivityState.PLANNED)
        if is_autonomous_run(run):
            autonomy = run.context_snapshot.setdefault("autonomy", {"version": 1, "activity_goal_policies": {}})
            policies = autonomy.setdefault("activity_goal_policies", {})
            policies[activity.activity_id] = {"completion": completion, "initial_goal_value": goal_value, "extensions_total": 0, "reason": reason}
        return activity

    @staticmethod
    def _horizon_mode(run: WanderRun) -> str:
        timing = (run.context_snapshot or {}).get("timing")
        return "estimate" if isinstance(timing, dict) and timing.get("horizon_mode") == "estimate" else "deadline"

    def request_interrupt(self, activity_id: str, *, source: str = '') -> None:
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        activity.interrupt_requested = True
        if source in {'user_message', 'manual_stop'}:
            activity.abort_reason = source
        snapshot = dict(run.context_snapshot or {})
        if (snapshot.get("game_end_request") or {}).get("activity_id") == activity_id:
            snapshot.pop("game_end_request", None)
            run.context_snapshot = snapshot
        self.store.save_plan(run, [activity])

    def request_game_stop(self, activity_id: str) -> None:
        """End an invited game after the current confirmed node, with a recap."""
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        if run.trigger_reason != "ui_action:play_game" or activity.activity_type != EventType.PLAY_GAME.value:
            raise ValueError("not_an_invited_game")
        if activity.state in (ActivityState.SETTLING, ActivityState.COMPLETED,
                              ActivityState.INTERRUPTED, ActivityState.ABORTED):
            return
        if activity.interrupt_requested:
            return
        activity.interrupt_requested = True
        run.context_snapshot = dict(run.context_snapshot or {})
        run.context_snapshot["game_end_request"] = {
            "activity_id": activity_id, "requested_at": self._iso(self.clock()),
        }
        self.store.save_plan(run, [activity])

    def requested_stop_reason(self, activity_id: str) -> Optional[SettlementReason]:
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        return self._requested_stop_reason(run, activity)

    @staticmethod
    def _requested_stop_reason(run: WanderRun, activity: WanderActivity) -> Optional[SettlementReason]:
        if not activity.interrupt_requested:
            return None
        request = (run.context_snapshot or {}).get("game_end_request") or {}
        if (run.trigger_reason == "ui_action:play_game"
                and activity.activity_type == EventType.PLAY_GAME.value
                and request.get("activity_id") == activity.activity_id):
            return SettlementReason.GAME_STOP
        return SettlementReason.USER_INTERRUPT

    def begin_settlement(
        self,
        activity_id: str,
        reason: Optional[SettlementReason] = None,
    ) -> None:
        """Claim the single settlement path before any settlement LLM call."""
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        if activity.state == ActivityState.SETTLING:
            if reason is not None and activity.settlement_reason is None:
                activity.settlement_reason = reason
                self.store.save_activity(activity)
            return
        if activity.state in (ActivityState.COMPLETED, ActivityState.INTERRUPTED, ActivityState.ABORTED):
            raise ValueError("activity is already terminal")
        self._move_to_settling(run, activity, reason)

    def settle_activity(self, activity_id: str, reason: SettlementReason, *, summary: str = "",
                        emotion_effect: Optional[dict] = None, continue_next: bool = False,
                        share_decision: Optional[bool] = None,
                        next_inclination_note: str = "") -> RuntimeAction:
        """One terminal path for normal ending, interruption, and execution failure."""
        activity = self._activity_required(activity_id)
        run = self._run_required(activity.run_id)
        if activity.state not in (ActivityState.SETTLING, ActivityState.COMPLETED, ActivityState.INTERRUPTED, ActivityState.ABORTED):
            self._move_to_settling(run, activity, reason)
        for node_row in self.store.list_nodes(activity_id):
            node = self._node(node_row)
            if node.state in (NodeState.RUNNING, NodeState.REVIEWED):
                node.transition_to(NodeState.ABORTED)
                node.abort_reason = reason.value
                node.completed_at = self._iso(self.clock())
                self.store.save_node(node)
        activity.summary, activity.emotion_effect = summary, dict(emotion_effect or {})
        activity.continue_next, activity.settlement_reason = continue_next, reason
        activity.share_decision = share_decision
        activity.next_inclination_note = next_inclination_note
        activity.ended_at = self._iso(self.clock())
        terminal_activity = ActivityState.INTERRUPTED if reason == SettlementReason.USER_INTERRUPT else (ActivityState.ABORTED if reason == SettlementReason.EXECUTION_ERROR else ActivityState.COMPLETED)
        activity.transition_to(terminal_activity)
        activities = [self._activity(item) for item in self.store.list_activities(run.run_id)]
        changed_activities = [activity]
        # A run-level execution error or user interruption closes the whole
        # accepted plan.  Planned activities have never acquired a node and
        # must remain truthful about that fact: persist a terminal activity
        # with an explicit reason, rather than letting the log/UI render them
        # as indefinitely pending or as self-failures.
        if reason in (SettlementReason.EXECUTION_ERROR, SettlementReason.USER_INTERRUPT):
            not_started_reason = (
                "not_started_after_user_interrupt"
                if reason == SettlementReason.USER_INTERRUPT
                else "not_started_after_prior_execution_error"
            )
            for pending in activities:
                if pending.activity_id == activity.activity_id or pending.state != ActivityState.PLANNED:
                    continue
                pending.abort_reason = not_started_reason
                pending.settlement_reason = reason
                pending.ended_at = self._iso(self.clock())
                if reason == SettlementReason.USER_INTERRUPT:
                    pending.transition_to(ActivityState.SETTLING)
                    pending.transition_to(ActivityState.INTERRUPTED)
                else:
                    pending.transition_to(ActivityState.ABORTED)
                changed_activities.append(pending)
        next_activity = next((item for item in activities if item.order_index > activity.order_index and item.state == ActivityState.PLANNED), None)
        if reason not in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR) and next_activity:
            next_activity.transition_to(ActivityState.ACTIVE_WAIT)
            self._schedule_initial(next_activity, self.clock())
            run.transition_to(RunState.ACTIVE_WAIT)
            self.store.save_plan(run, [*changed_activities, next_activity])
            return RuntimeAction(RuntimeActionType.WAIT, run.run_id, next_activity.activity_id)
        terminal_run = RunState.INTERRUPTED if reason == SettlementReason.USER_INTERRUPT else (RunState.ABORTED if reason == SettlementReason.EXECUTION_ERROR else RunState.COMPLETED)
        if run.state != RunState.SETTLING:
            run.transition_to(RunState.SETTLING)
        run.transition_to(terminal_run)
        run.ended_at, run.outcome = self._iso(self.clock()), reason.value
        self.store.save_plan(run, changed_activities, finalize_continuation=True)
        return RuntimeAction(RuntimeActionType.IDLE, run.run_id, activity.activity_id, reason=reason)

    def _schedule_initial(self, activity: WanderActivity, now: datetime) -> None:
        # A switch carries the review's absolute due time, including on recovery.
        # Its duration starts at that due time, not while waiting for the switch.
        start = max(now, self._parse(activity.next_node_at)) if activity.next_node_at else now
        if activity.goal_mode == GoalMode.DURATION:
            activity.timer_ends_at = self._iso(start + timedelta(minutes=activity.goal_value or 1))
            # Sleep is a pure timer; other duration events need an executor-selected first node.
            if activity.activity_type != "sleep":
                activity.next_node_at = self._iso(start)
            else:
                activity.next_node_at = None
        elif activity.goal_mode != GoalMode.EXTERNAL_SIGNAL:
            activity.next_node_at = self._iso(start)

    def _move_to_settling(
        self,
        run: WanderRun,
        activity: WanderActivity,
        reason: Optional[SettlementReason] = None,
        *,
        save: bool = True,
    ) -> None:
        if activity.state != ActivityState.SETTLING:
            activity.transition_to(ActivityState.SETTLING)
        if reason is not None:
            activity.settlement_reason = reason
        if run.state != RunState.SETTLING:
            run.transition_to(RunState.SETTLING)
        if save:
            self.store.save_plan(run, [activity])

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _run(row: dict) -> WanderRun:
        row = dict(row); row["state"] = RunState(row["state"]); return WanderRun(**row)
    @staticmethod
    def _activity(row: dict) -> WanderActivity:
        row = dict(row); row["state"] = ActivityState(row["state"]); row["goal_mode"] = GoalMode(row["goal_mode"])
        if row.get("settlement_reason"): row["settlement_reason"] = SettlementReason(row["settlement_reason"])
        return WanderActivity(**row)
    @staticmethod
    def _node(row: dict) -> WanderNode:
        row = dict(row); row["state"] = NodeState(row["state"]); return WanderNode(**row)
    def _run_required(self, run_id: str) -> WanderRun:
        row = self.store.get_run(run_id)
        if row is None: raise ValueError("run not found")
        return self._run(row)
    def _activity_required(self, activity_id: str) -> WanderActivity:
        row = self.store.get_activity(activity_id)
        if row is None: raise ValueError("activity not found")
        return self._activity(row)
    def _node_required(self, node_id: str) -> WanderNode:
        row = self.store.get_node(node_id)
        if row is None: raise ValueError("node not found")
        return self._node(row)
