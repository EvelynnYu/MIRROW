"""Persistent, auditable contracts for the production Wander runtime.

The runtime preserves one authoritative identity chain from autonomous plan to
real action and outward delivery: run -> activity -> node -> decision/delivery.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional
from uuid import uuid4


def now_iso() -> str:
    """Return a local ISO timestamp, matching the project's SQLite convention."""
    return datetime.now().isoformat()


class RunState(str, Enum):
    PLANNING = "planning"
    ACTIVE_WAIT = "active_wait"
    NODE_RUNNING = "node_running"
    NODE_REVIEW = "node_review"
    SETTLING = "settling"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


class ActivityState(str, Enum):
    PLANNED = "planned"
    ACTIVE_WAIT = "active_wait"
    NODE_RUNNING = "node_running"
    NODE_REVIEW = "node_review"
    SETTLING = "settling"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


class NodeState(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    REVIEWED = "reviewed"
    COMPLETED = "completed"
    ABORTED = "aborted"


class GoalMode(str, Enum):
    COUNT = "count"
    DURATION = "duration"
    OPEN_ENDED = "open_ended"
    SINGLE = "single"
    EXTERNAL_SIGNAL = "external_signal"


class SettlementReason(str, Enum):
    GOAL_REACHED = "goal_reached"
    TIMER_ENDED = "timer_ended"
    NATURAL_STOP = "natural_stop"
    USER_INTERRUPT = "user_interrupt"
    EMOTION_SHIFT = "emotion_shift"
    EXECUTION_ERROR = "execution_error"
    PLANNER_ABORT = "planner_abort"
    PLAN_HORIZON = "plan_horizon"


class DecisionPhase(str, Enum):
    PLAN = "plan"
    NODE_REVIEW = "node_review"
    SETTLEMENT = "settlement"
    SHARE = "share"
    NODE_EXECUTION = "node_execution"
    SELF_REFLECTION = "self_reflection"
    WISH_COMMIT = "wish_commit"
    TIMING_REVIEW = "timing_review"


TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.INTERRUPTED, RunState.ABORTED})
TERMINAL_ACTIVITY_STATES = frozenset({ActivityState.COMPLETED, ActivityState.INTERRUPTED, ActivityState.ABORTED})
TERMINAL_NODE_STATES = frozenset({NodeState.COMPLETED, NodeState.ABORTED})

_RUN_TRANSITIONS = {
    RunState.PLANNING: {RunState.ACTIVE_WAIT, RunState.SETTLING, RunState.ABORTED},
    RunState.ACTIVE_WAIT: {RunState.NODE_RUNNING, RunState.SETTLING, RunState.INTERRUPTED, RunState.ABORTED},
    RunState.NODE_RUNNING: {RunState.NODE_REVIEW, RunState.SETTLING, RunState.INTERRUPTED, RunState.ABORTED},
    RunState.NODE_REVIEW: {RunState.ACTIVE_WAIT, RunState.SETTLING, RunState.INTERRUPTED, RunState.ABORTED},
    # A normal settlement may activate the next already-planned activity.
    RunState.SETTLING: {RunState.ACTIVE_WAIT, RunState.COMPLETED, RunState.INTERRUPTED, RunState.ABORTED},
}
_ACTIVITY_TRANSITIONS = {
    ActivityState.PLANNED: {ActivityState.ACTIVE_WAIT, ActivityState.SETTLING, ActivityState.ABORTED},
    ActivityState.ACTIVE_WAIT: {ActivityState.NODE_RUNNING, ActivityState.SETTLING, ActivityState.INTERRUPTED, ActivityState.ABORTED},
    ActivityState.NODE_RUNNING: {ActivityState.NODE_REVIEW, ActivityState.SETTLING, ActivityState.INTERRUPTED, ActivityState.ABORTED},
    ActivityState.NODE_REVIEW: {ActivityState.ACTIVE_WAIT, ActivityState.SETTLING, ActivityState.INTERRUPTED, ActivityState.ABORTED},
    ActivityState.SETTLING: {ActivityState.COMPLETED, ActivityState.INTERRUPTED, ActivityState.ABORTED},
}
_NODE_TRANSITIONS = {
    NodeState.PLANNED: {NodeState.RUNNING, NodeState.ABORTED},
    NodeState.RUNNING: {NodeState.REVIEWED, NodeState.ABORTED},
    NodeState.REVIEWED: {NodeState.COMPLETED, NodeState.ABORTED},
}


def _ensure_transition(current: Enum, target: Enum, transitions: Mapping[Enum, set[Enum]]) -> None:
    if current == target:
        return
    if current in transitions and target in transitions[current]:
        return
    raise ValueError(f"Illegal state transition: {current.value} -> {target.value}")


def ensure_run_transition(current: RunState, target: RunState) -> None:
    _ensure_transition(current, target, _RUN_TRANSITIONS)


def ensure_activity_transition(current: ActivityState, target: ActivityState) -> None:
    _ensure_transition(current, target, _ACTIVITY_TRANSITIONS)


def ensure_node_transition(current: NodeState, target: NodeState) -> None:
    _ensure_transition(current, target, _NODE_TRANSITIONS)


@dataclass
class WanderRun:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    state: RunState = RunState.PLANNING
    trigger_reason: str = "interval"
    session_id: str = ""
    inclination_text: str = ""
    probability_snapshot: dict[str, Any] = field(default_factory=dict)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    plan_horizon_min: Optional[int] = None
    next_wake_at: Optional[str] = None
    ended_at: Optional[str] = None
    outcome: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def transition_to(self, target: RunState) -> None:
        ensure_run_transition(self.state, target)
        self.state = target
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass
class WanderActivity:
    activity_type: str
    run_id: str
    activity_id: str = field(default_factory=lambda: uuid4().hex)
    state: ActivityState = ActivityState.PLANNED
    order_index: int = 0
    goal_mode: GoalMode = GoalMode.SINGLE
    goal_value: Optional[int] = None
    reason: str = ""
    interrupt_requested: bool = False
    next_node_at: Optional[str] = None
    timer_ends_at: Optional[str] = None
    ended_at: Optional[str] = None
    settlement_reason: Optional[SettlementReason] = None
    abort_reason: str = ""
    summary: str = ""
    emotion_effect: dict[str, Any] = field(default_factory=dict)
    share_decision: Optional[bool] = None
    continue_next: Optional[bool] = None
    next_inclination_note: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def transition_to(self, target: ActivityState) -> None:
        ensure_activity_transition(self.state, target)
        self.state = target
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        result["goal_mode"] = self.goal_mode.value
        result["settlement_reason"] = self.settlement_reason.value if self.settlement_reason else None
        return result


@dataclass
class WanderNode:
    activity_id: str
    round_index: int
    node_id: str = field(default_factory=lambda: uuid4().hex)
    state: NodeState = NodeState.PLANNED
    source_summary: str = ""
    source_payload: dict[str, Any] = field(default_factory=dict)
    reflection: str = ""
    emotion_effect: dict[str, Any] = field(default_factory=dict)
    continue_activity: Optional[bool] = None
    abort_reason: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    execution_status: str = "pending"
    completion_signal: str = ""
    retry_safe: bool = False
    side_effect_refs: dict[str, Any] = field(default_factory=dict)
    execution_error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def transition_to(self, target: NodeState) -> None:
        ensure_node_transition(self.state, target)
        self.state = target
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result
