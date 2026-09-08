"""SQLite persistence for the production Wander runtime.

Construction is explicit: importing this module never creates or opens a DB.
The caller chooses when and where runtime tables are initialized.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from .runtime_models import (
    ActivityState, DecisionPhase, GoalMode, NodeState, RunState,
    SettlementReason, WanderActivity, WanderNode, WanderRun, now_iso,
)

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token", "credential")
_INLINE_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token|credential)\s*([:=])\s*(?:Bearer\s+)?([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def redact_sensitive(value: Any) -> Any:
    """Recursively redact secret-bearing keys while retaining useful audit context."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        value = _INLINE_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
        value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        return _IPV4_PATTERN.sub("[REDACTED_IP]", value)
    return value


def _json(value: Any) -> str:
    return json.dumps(redact_sensitive(value), ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any) -> str:
    """Redact free-form audit text without JSON-encoding the stored value."""
    redacted = redact_sensitive("" if value is None else str(value))
    return str(redacted)


def _load_json(value: Optional[str]) -> Any:
    return json.loads(value) if value else {}


def _duration_seconds(started_at: Any, ended_at: Any = None) -> Optional[float]:
    """Calculate a small, timezone-tolerant duration projection for logs."""
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(str(started_at))
        end = datetime.fromisoformat(str(ended_at)) if ended_at else datetime.now()
        # Runtime timestamps are normally local naive ISO values.  Normalize
        # aware values too so historical/migrated rows cannot raise a
        # naive-vs-aware subtraction error while rendering the log.
        if start.tzinfo is not None:
            start = start.astimezone().replace(tzinfo=None)
        if end.tzinfo is not None:
            end = end.astimezone().replace(tzinfo=None)
        return max(0.0, round((end - start).total_seconds(), 3))
    except (TypeError, ValueError, OverflowError):
        return None


def _format_duration(value: Optional[float]) -> str:
    if value is None:
        return ""
    seconds = max(0, int(round(value)))
    if seconds < 60:
        return f"{seconds}秒"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}分{remainder:02d}秒"


class WanderRuntimeStore:
    """Explicit SQLite store; no singleton and no import-time initialization."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS wander_runs (
                run_id TEXT PRIMARY KEY, state TEXT NOT NULL, trigger_reason TEXT NOT NULL,
                session_id TEXT NOT NULL, inclination_text TEXT NOT NULL,
                probability_snapshot TEXT NOT NULL, context_snapshot TEXT NOT NULL,
                plan_horizon_min INTEGER, next_wake_at TEXT, ended_at TEXT, outcome TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wander_activities (
                activity_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES wander_runs(run_id),
                activity_type TEXT NOT NULL, state TEXT NOT NULL, order_index INTEGER NOT NULL,
                goal_mode TEXT NOT NULL, goal_value INTEGER, reason TEXT NOT NULL,
                interrupt_requested INTEGER NOT NULL DEFAULT 0, next_node_at TEXT, timer_ends_at TEXT,
                ended_at TEXT, settlement_reason TEXT, abort_reason TEXT NOT NULL, summary TEXT NOT NULL,
                emotion_effect TEXT NOT NULL, share_decision INTEGER, continue_next INTEGER,
                next_inclination_note TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wander_nodes (
                node_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES wander_activities(activity_id),
                round_index INTEGER NOT NULL, state TEXT NOT NULL, source_summary TEXT NOT NULL,
                source_payload TEXT NOT NULL, reflection TEXT NOT NULL, emotion_effect TEXT NOT NULL,
                continue_activity INTEGER, abort_reason TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                execution_status TEXT NOT NULL DEFAULT 'pending', completion_signal TEXT NOT NULL DEFAULT '',
                retry_safe INTEGER NOT NULL DEFAULT 0, side_effect_refs TEXT NOT NULL DEFAULT '{}', execution_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(activity_id, round_index)
            );
            CREATE TABLE IF NOT EXISTS wander_decisions (
                decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES wander_runs(run_id),
                activity_id TEXT REFERENCES wander_activities(activity_id),
                node_id TEXT REFERENCES wander_nodes(node_id), phase TEXT NOT NULL, model TEXT NOT NULL,
                recipe TEXT NOT NULL, temperature REAL, input_context TEXT NOT NULL, raw_output TEXT NOT NULL,
                reasoning TEXT NOT NULL, parsed_output TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL,
                duration_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, cache_tokens INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wander_deliveries (
                delivery_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES wander_runs(run_id),
                activity_id TEXT NOT NULL REFERENCES wander_activities(activity_id),
                message_id TEXT, delivery_type TEXT NOT NULL, message_content TEXT NOT NULL,
                tool_calls TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wander_self_book_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_key TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES wander_runs(run_id),
                activity_id TEXT NOT NULL REFERENCES wander_activities(activity_id),
                node_id TEXT NOT NULL REFERENCES wander_nodes(node_id),
                decision_id TEXT NOT NULL REFERENCES wander_decisions(decision_id),
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                statement TEXT NOT NULL,
                keywords TEXT NOT NULL,
                evidence_node_ids TEXT NOT NULL,
                evidence_summary TEXT NOT NULL,
                confidence REAL NOT NULL,
                stability TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT NOT NULL DEFAULT '',
                entry_name TEXT NOT NULL DEFAULT '',
                UNIQUE(node_id, candidate_key)
            );
            CREATE TABLE IF NOT EXISTS wander_pending_inclinations (
                source_activity_id TEXT PRIMARY KEY REFERENCES wander_activities(activity_id),
                session_id TEXT NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_run_id TEXT REFERENCES wander_runs(run_id),
                created_at TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS wander_scheduler_wakes (
                wake_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                next_plan_at TEXT NOT NULL,
                wake_reason TEXT NOT NULL,
                source_activity_id TEXT NOT NULL DEFAULT '',
                source_run_id TEXT NOT NULL DEFAULT '',
                failure_streak INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wander_activity_run ON wander_activities(run_id);
            CREATE INDEX IF NOT EXISTS idx_wander_node_activity ON wander_nodes(activity_id);
            CREATE INDEX IF NOT EXISTS idx_wander_decision_run ON wander_decisions(run_id);
            CREATE INDEX IF NOT EXISTS idx_wander_delivery_activity ON wander_deliveries(activity_id);
            CREATE INDEX IF NOT EXISTS idx_wander_self_candidate_status
                ON wander_self_book_candidates(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_wander_inclination_session_status
                ON wander_pending_inclinations(session_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_wander_scheduler_next_plan
                ON wander_scheduler_wakes(next_plan_at, updated_at);
            """)
            # Explicit initialize() may be called on a database created by an
            # earlier migration stage. Add new node audit columns in place.
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(wander_nodes)").fetchall()
            }
            additions = {
                "execution_status": "TEXT NOT NULL DEFAULT 'pending'",
                "completion_signal": "TEXT NOT NULL DEFAULT ''",
                "retry_safe": "INTEGER NOT NULL DEFAULT 0",
                "side_effect_refs": "TEXT NOT NULL DEFAULT '{}'",
                "execution_error": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE wander_nodes ADD COLUMN {name} {definition}")

    def get_scheduler_wake(self, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Return the persisted next-plan wake, if one exists."""
        query = "SELECT * FROM wander_scheduler_wakes"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            params = (session_id,)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def save_scheduler_wake(
        self,
        *,
        session_id: str,
        next_plan_at: str,
        wake_reason: str,
        source_activity_id: str = "",
        source_run_id: str = "",
        failure_streak: int = 0,
    ) -> dict[str, Any]:
        """Persist one auditable scheduler decision outside activity rows."""
        cleaned_session = _text(session_id).strip()
        if not cleaned_session:
            raise ValueError("session_id is required for scheduler wake")
        wake_id = uuid4().hex
        created_at = now_iso()
        updated_at = created_at
        bounded_streak = max(0, int(failure_streak))
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO wander_scheduler_wakes
                   (wake_id, session_id, next_plan_at, wake_reason,
                    source_activity_id, source_run_id, failure_streak, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     next_plan_at=excluded.next_plan_at,
                     wake_reason=excluded.wake_reason,
                     source_activity_id=excluded.source_activity_id,
                     source_run_id=excluded.source_run_id,
                     failure_streak=excluded.failure_streak,
                     updated_at=excluded.updated_at""",
                (
                    wake_id, cleaned_session, _text(next_plan_at), _text(wake_reason),
                    _text(source_activity_id), _text(source_run_id), bounded_streak,
                    created_at, updated_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM wander_scheduler_wakes WHERE session_id=?",
                (cleaned_session,),
            ).fetchone()
        return dict(row)

    def clear_scheduler_wake(self, session_id: Optional[str] = None) -> None:
        """Clear a future wake after a user stop or a successful claim."""
        with self._connection() as conn:
            if session_id is None:
                conn.execute("DELETE FROM wander_scheduler_wakes")
            else:
                conn.execute("DELETE FROM wander_scheduler_wakes WHERE session_id=?", (session_id,))

    def claim_scheduler_wake(self, session_id: str, now: datetime) -> Optional[dict[str, Any]]:
        """Atomically consume a due wake before creating its successor run."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM wander_scheduler_wakes WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                due = datetime.fromisoformat(row["next_plan_at"]) <= now
            except (TypeError, ValueError):
                due = True
            if not due:
                return None
            conn.execute("DELETE FROM wander_scheduler_wakes WHERE session_id=?", (session_id,))
            return dict(row)

    def recover_stale_planning_runs(
        self,
        *,
        now: Optional[datetime] = None,
        retry_delay_seconds: float = 300.0,
        wake_reason: str = "recovered_stale_planning",
    ) -> list[dict[str, Any]]:
        """Close planner claims which cannot have an accepted plan.

        A planner creates its ``planning`` run before the model call.  If the
        process disappears during that call, there is no activity row and no
        accepted plan to resume.  Treating such a row as an active run makes
        the controller return ``IDLE`` forever.  Recovery is deliberately one
        SQLite transaction: the run is terminal, any inclination claimed by
        that run is released, and one auditable successor wake is written
        together.  The ``NOT EXISTS`` predicate also makes repeated startup
        recovery idempotent.
        """
        recovered_at = now or datetime.now()
        recovered_iso = recovered_at.isoformat()
        next_plan_at = (
            recovered_at + timedelta(seconds=max(0.05, float(retry_delay_seconds)))
        ).isoformat()
        cleaned_reason = _text(wake_reason).strip() or "recovered_stale_planning"
        recovered: list[dict[str, Any]] = []

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.session_id
                  FROM wander_runs r
                 WHERE r.state=?
                   AND NOT EXISTS (
                       SELECT 1 FROM wander_activities a WHERE a.run_id=r.run_id
                   )
                 ORDER BY r.created_at, r.rowid
                """,
                (RunState.PLANNING.value,),
            ).fetchall()

            for row in rows:
                run_id = str(row["run_id"])
                session_id = str(row["session_id"] or "main")
                updated = conn.execute(
                    """
                    UPDATE wander_runs
                       SET state=?, ended_at=?, outcome=?, updated_at=?
                     WHERE run_id=? AND state=?
                       AND NOT EXISTS (
                           SELECT 1 FROM wander_activities a WHERE a.run_id=wander_runs.run_id
                       )
                    """,
                    (
                        RunState.ABORTED.value,
                        recovered_iso,
                        cleaned_reason,
                        recovered_iso,
                        run_id,
                        RunState.PLANNING.value,
                    ),
                )
                if updated.rowcount != 1:
                    # Another runner/initializer won the compare-and-set.  Do
                    # not release its inclination or create a duplicate wake.
                    continue

                released = conn.execute(
                    """
                    UPDATE wander_pending_inclinations
                       SET status='pending', claimed_run_id=NULL
                     WHERE status='claimed' AND claimed_run_id=?
                    """,
                    (run_id,),
                ).rowcount

                wake_id = uuid4().hex
                conn.execute(
                    """
                    INSERT INTO wander_scheduler_wakes
                        (wake_id, session_id, next_plan_at, wake_reason,
                         source_activity_id, source_run_id, failure_streak,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, '', ?, 0, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (
                        wake_id,
                        session_id,
                        next_plan_at,
                        cleaned_reason,
                        run_id,
                        recovered_iso,
                        recovered_iso,
                    ),
                )
                wake = conn.execute(
                    "SELECT * FROM wander_scheduler_wakes WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                recovered.append({
                    "run_id": run_id,
                    "session_id": session_id,
                    "outcome": cleaned_reason,
                    "ended_at": recovered_iso,
                    "released_inclinations": int(released),
                    "next_plan_at": str(wake["next_plan_at"]) if wake else next_plan_at,
                    "wake_reason": str(wake["wake_reason"]) if wake else cleaned_reason,
                })
        return recovered

    def get_runtime_status(
        self,
        session_id: Optional[str] = None,
        *,
        planning_timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        """Project scheduler health without conflating creator and runtime state."""
        run = self.get_active_run(session_id=session_id)
        wake = self.get_scheduler_wake(session_id=session_id)
        active_state = str(run.get("state") or "") if run else ""
        runtime_state = "idle"
        blocked_reason = ""

        if run is not None:
            if active_state == RunState.PLANNING.value:
                # A live planner legitimately has no activity until its model
                # call returns.  Only project it as blocked after the same
                # timeout enforced by RuntimeRunner; otherwise the monitor
                # reports a healthy in-flight plan as an incident.
                planning_age = _duration_seconds(run.get("created_at"))
                if planning_age is None:
                    planning_age = float("inf")
                if planning_age >= max(0.05, float(planning_timeout_seconds)):
                    runtime_state = "blocked"
                    blocked_reason = "planner_without_accepted_plan"
                else:
                    runtime_state = "planning"
            else:
                runtime_state = "active"
        elif wake is not None:
            runtime_state = "scheduled"

        return {
            "runtime_state": runtime_state,
            "active_run_id": str(run.get("run_id") or "") if run else "",
            "active_run_state": active_state,
            "blocked_reason": blocked_reason,
            "next_plan_at": str(wake.get("next_plan_at") or "") if wake else "",
            "wake_reason": str(wake.get("wake_reason") or "") if wake else "",
            "schedule_source_run_id": str(wake.get("source_run_id") or "") if wake else "",
            "schedule_failure_streak": int(wake.get("failure_streak") or 0) if wake else 0,
        }

    def save_pending_inclination(
        self,
        source_activity_id: str,
        session_id: str,
        note: str,
    ) -> None:
        """Persist one continuation note without duplicating settlement retries."""
        cleaned = _text(note).strip()
        if not cleaned:
            return
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO wander_pending_inclinations
                   (source_activity_id, session_id, note, status, created_at)
                   VALUES (?, ?, ?, 'pending', ?)
                   ON CONFLICT(source_activity_id) DO NOTHING""",
                (source_activity_id, session_id, cleaned, now_iso()),
            )

    def claim_pending_inclination(self, session_id: str, run_id: str) -> Optional[dict[str, Any]]:
        """Claim the oldest pending note for one planning attempt."""
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM wander_pending_inclinations
                   WHERE session_id=? AND status='pending'
                   ORDER BY created_at, rowid LIMIT 1""",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE wander_pending_inclinations
                   SET status='claimed', claimed_run_id=?
                   WHERE source_activity_id=? AND status='pending'""",
                (run_id, row["source_activity_id"]),
            )
            return dict(row)

    def release_pending_inclination(self, source_activity_id: str, run_id: str) -> None:
        """Make a note available again when its planning attempt fails."""
        with self._connection() as conn:
            conn.execute(
                """UPDATE wander_pending_inclinations
                   SET status='pending', claimed_run_id=NULL
                   WHERE source_activity_id=? AND status='claimed' AND claimed_run_id=?""",
                (source_activity_id, run_id),
            )

    def consume_pending_inclination(self, source_activity_id: str, run_id: str) -> None:
        """Consume a note only after the successor plan is durably accepted."""
        with self._connection() as conn:
            conn.execute(
                """UPDATE wander_pending_inclinations
                   SET status='consumed', consumed_at=?
                   WHERE source_activity_id=? AND status='claimed' AND claimed_run_id=?""",
                (now_iso(), source_activity_id, run_id),
            )

    def get_pending_inclination(self, source_activity_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM wander_pending_inclinations WHERE source_activity_id=?",
                (source_activity_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_run(self, run: WanderRun) -> None:
        data = run.to_dict()
        with self._connection() as conn:
            conn.execute("""INSERT INTO wander_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, inclination_text=excluded.inclination_text,
                probability_snapshot=excluded.probability_snapshot, context_snapshot=excluded.context_snapshot,
                plan_horizon_min=excluded.plan_horizon_min, next_wake_at=excluded.next_wake_at,
                ended_at=excluded.ended_at, outcome=excluded.outcome, updated_at=excluded.updated_at""",
                (data["run_id"], data["state"], data["trigger_reason"], data["session_id"], _text(data["inclination_text"]),
                 _json(data["probability_snapshot"]), _json(data["context_snapshot"]), data["plan_horizon_min"],
                 data["next_wake_at"], data["ended_at"], _text(data["outcome"]), data["created_at"], data["updated_at"]))

    def save_activity(self, activity: WanderActivity) -> None:
        data = activity.to_dict()
        with self._connection() as conn:
            conn.execute("""INSERT INTO wander_activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET state=excluded.state, interrupt_requested=excluded.interrupt_requested,
                next_node_at=excluded.next_node_at, timer_ends_at=excluded.timer_ends_at, ended_at=excluded.ended_at,
                settlement_reason=excluded.settlement_reason, abort_reason=excluded.abort_reason, summary=excluded.summary,
                emotion_effect=excluded.emotion_effect, share_decision=excluded.share_decision,
                continue_next=excluded.continue_next, next_inclination_note=excluded.next_inclination_note,
                updated_at=excluded.updated_at""",
                (data["activity_id"], data["run_id"], data["activity_type"], data["state"], data["order_index"],
                 data["goal_mode"], data["goal_value"], _text(data["reason"]), int(data["interrupt_requested"]),
                 data["next_node_at"], data["timer_ends_at"], data["ended_at"], data["settlement_reason"],
                 _text(data["abort_reason"]), _text(data["summary"]), _json(data["emotion_effect"]),
                 None if data["share_decision"] is None else int(data["share_decision"]),
                 None if data["continue_next"] is None else int(data["continue_next"]), _text(data["next_inclination_note"]),
                 data["created_at"], data["updated_at"]))

    def save_plan(self, run: WanderRun, activities: list[WanderActivity]) -> None:
        """Atomically persist one accepted plan; callers validate it before state changes."""
        if not activities:
            raise ValueError("plan has no activities")
        if any(activity.run_id != run.run_id for activity in activities):
            raise ValueError("every activity must belong to the plan run")
        if len({activity.activity_id for activity in activities}) != len(activities):
            raise ValueError("activity_id must be unique within a plan")
        if len({activity.order_index for activity in activities}) != len(activities):
            raise ValueError("order_index must be unique within a plan")
        run_data = run.to_dict()
        with self._connection() as conn:
            conn.execute("""INSERT INTO wander_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, inclination_text=excluded.inclination_text,
                probability_snapshot=excluded.probability_snapshot, context_snapshot=excluded.context_snapshot,
                plan_horizon_min=excluded.plan_horizon_min, next_wake_at=excluded.next_wake_at,
                ended_at=excluded.ended_at, outcome=excluded.outcome, updated_at=excluded.updated_at""",
                (run_data["run_id"], run_data["state"], run_data["trigger_reason"], run_data["session_id"], _text(run_data["inclination_text"]),
                 _json(run_data["probability_snapshot"]), _json(run_data["context_snapshot"]), run_data["plan_horizon_min"],
                 run_data["next_wake_at"], run_data["ended_at"], _text(run_data["outcome"]), run_data["created_at"], run_data["updated_at"]))
            for activity in activities:
                data = activity.to_dict()
                conn.execute("""INSERT INTO wander_activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(activity_id) DO UPDATE SET state=excluded.state, interrupt_requested=excluded.interrupt_requested,
                    next_node_at=excluded.next_node_at, timer_ends_at=excluded.timer_ends_at, ended_at=excluded.ended_at,
                    settlement_reason=excluded.settlement_reason, abort_reason=excluded.abort_reason, summary=excluded.summary,
                    emotion_effect=excluded.emotion_effect, share_decision=excluded.share_decision,
                    continue_next=excluded.continue_next, next_inclination_note=excluded.next_inclination_note, updated_at=excluded.updated_at""",
                    (data["activity_id"], data["run_id"], data["activity_type"], data["state"], data["order_index"], data["goal_mode"], data["goal_value"], _text(data["reason"]), int(data["interrupt_requested"]),
                     data["next_node_at"], data["timer_ends_at"], data["ended_at"], data["settlement_reason"], _text(data["abort_reason"]), _text(data["summary"]), _json(data["emotion_effect"]),
                     None if data["share_decision"] is None else int(data["share_decision"]), None if data["continue_next"] is None else int(data["continue_next"]), _text(data["next_inclination_note"]), data["created_at"], data["updated_at"]))

    def save_node(self, node: WanderNode) -> None:
        data = node.to_dict()
        with self._connection() as conn:
            conn.execute("""INSERT INTO wander_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET state=excluded.state, source_summary=excluded.source_summary,
                source_payload=excluded.source_payload, reflection=excluded.reflection, emotion_effect=excluded.emotion_effect,
                continue_activity=excluded.continue_activity, abort_reason=excluded.abort_reason, started_at=excluded.started_at,
                completed_at=excluded.completed_at, execution_status=excluded.execution_status, completion_signal=excluded.completion_signal,
                retry_safe=excluded.retry_safe, side_effect_refs=excluded.side_effect_refs, execution_error=excluded.execution_error, updated_at=excluded.updated_at""",
                (data["node_id"], data["activity_id"], data["round_index"], data["state"], _text(data["source_summary"]),
                 _json(data["source_payload"]), _text(data["reflection"]), _json(data["emotion_effect"]),
                 None if data["continue_activity"] is None else int(data["continue_activity"]), _text(data["abort_reason"]),
                 data["started_at"], data["completed_at"], data["execution_status"], _text(data["completion_signal"]), int(data["retry_safe"]),
                 _json(data["side_effect_refs"]), _text(data["execution_error"]), data["created_at"], data["updated_at"]))

    @staticmethod
    def _ensure_identity_chain(conn: sqlite3.Connection, run_id: str, activity_id: Optional[str], node_id: Optional[str]) -> None:
        """Reject a valid-but-unrelated FK combination before it becomes bad audit data."""
        if activity_id:
            row = conn.execute("SELECT run_id FROM wander_activities WHERE activity_id=?", (activity_id,)).fetchone()
            if row is None or row["run_id"] != run_id:
                raise ValueError("activity_id does not belong to run_id")
        if node_id:
            row = conn.execute("SELECT activity_id FROM wander_nodes WHERE node_id=?", (node_id,)).fetchone()
            if row is None or row["activity_id"] != activity_id:
                raise ValueError("node_id does not belong to activity_id")

    def record_decision(self, *, run_id: str, phase: DecisionPhase | str, model: str, recipe: str,
                        input_context: Any, raw_output: Any = "", reasoning: Any = "", parsed_output: Any = None,
                        activity_id: Optional[str] = None, node_id: Optional[str] = None, temperature: Optional[float] = None,
                        status: str = "ok", error: str = "", duration_ms: Optional[int] = None,
                        prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
                        cache_tokens: Optional[int] = None, decision_id: Optional[str] = None) -> str:
        decision_id = decision_id or uuid4().hex
        phase_value = DecisionPhase(phase).value
        with self._connection() as conn:
            self._ensure_identity_chain(conn, run_id, activity_id, node_id)
            conn.execute("INSERT INTO wander_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, run_id, activity_id, node_id, phase_value, model, recipe, temperature,
                 _json(input_context), _json(raw_output), _json(reasoning), _json(parsed_output or {}), status, _text(error),
                 duration_ms, prompt_tokens, completion_tokens, cache_tokens, now_iso()))
        return decision_id

    def record_delivery(self, *, run_id: str, activity_id: str, delivery_type: str, message_id: Optional[str] = None,
                        message_content: str = "", tool_calls: Any = None, status: str = "sent",
                        delivery_id: Optional[str] = None) -> str:
        delivery_id = delivery_id or uuid4().hex
        with self._connection() as conn:
            self._ensure_identity_chain(conn, run_id, activity_id, None)
            conn.execute("INSERT INTO wander_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (delivery_id, run_id, activity_id, message_id, delivery_type, _text(message_content), _json(tool_calls or []), status, now_iso()))
        return delivery_id

    def save_self_book_candidates(
        self,
        *,
        run_id: str,
        activity_id: str,
        node_id: str,
        decision_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[str]:
        """Idempotently store pending evidence; never writes the real self book."""
        candidate_ids = []
        with self._connection() as conn:
            self._ensure_identity_chain(conn, run_id, activity_id, node_id)
            decision = conn.execute(
                "SELECT run_id, activity_id, node_id FROM wander_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if (
                decision is None
                or decision["run_id"] != run_id
                or decision["activity_id"] != activity_id
                or decision["node_id"] != node_id
            ):
                raise ValueError("decision_id does not belong to the self-reflection node")
            for candidate in candidates:
                candidate_id = str(candidate["candidate_id"])
                candidate_key = str(candidate["candidate_key"])
                conn.execute(
                    """INSERT INTO wander_self_book_candidates (
                        candidate_id, candidate_key, run_id, activity_id, node_id, decision_id,
                        title, category, statement, keywords, evidence_node_ids,
                        evidence_summary, confidence, stability, status, created_at,
                        resolved_at, resolution_note, entry_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, '', '')
                    ON CONFLICT(node_id, candidate_key) DO NOTHING""",
                    (
                        candidate_id, candidate_key, run_id, activity_id, node_id, decision_id,
                        _text(candidate.get("title", "")), _text(candidate.get("category", "")),
                        _text(candidate.get("statement", "")), _json(candidate.get("keywords", [])),
                        _json(candidate.get("evidence_node_ids", [])),
                        _text(candidate.get("evidence_summary", "")),
                        float(candidate.get("confidence", 0.0)),
                        _text(candidate.get("stability", "tentative")), now_iso(),
                    ),
                )
                row = conn.execute(
                    "SELECT candidate_id FROM wander_self_book_candidates WHERE node_id=? AND candidate_key=?",
                    (node_id, candidate_key),
                ).fetchone()
                candidate_ids.append(row["candidate_id"])
        return candidate_ids

    def list_self_book_candidates(
        self,
        status: Optional[str] = "pending",
        run_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM wander_self_book_candidates WHERE 1=1"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " AND status=?"
            params += (status,)
        if run_id is not None:
            query += " AND run_id=?"
            params += (run_id,)
        query += " ORDER BY created_at, rowid"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["keywords"] = _load_json(item["keywords"])
            item["evidence_node_ids"] = _load_json(item["evidence_node_ids"])
            results.append(item)
        return results

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM wander_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("probability_snapshot", "context_snapshot"):
            result[key] = _load_json(result[key])
        return result

    @staticmethod
    def _decode_activity(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["interrupt_requested"] = bool(result["interrupt_requested"])
        for key in ("emotion_effect",):
            result[key] = _load_json(result[key])
        for key in ("share_decision", "continue_next"):
            if result[key] is not None:
                result[key] = bool(result[key])
        return result

    @staticmethod
    def _decode_node(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("source_payload", "emotion_effect", "side_effect_refs"):
            result[key] = _load_json(result[key])
        result["retry_safe"] = bool(result["retry_safe"])
        if result["continue_activity"] is not None:
            result["continue_activity"] = bool(result["continue_activity"])
        return result

    def get_activity(self, activity_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM wander_activities WHERE activity_id=?", (activity_id,)).fetchone()
        return self._decode_activity(row) if row is not None else None

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM wander_nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._decode_node(row) if row is not None else None

    def list_activities(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM wander_activities WHERE run_id=? ORDER BY order_index", (run_id,)).fetchall()
        return [self._decode_activity(row) for row in rows]

    def list_activities_created_on(self, date_str: str) -> list[dict[str, Any]]:
        """按本地创建日期前缀查活动（created_at 形如 '2026-08-23T...'），供今日漫想频率参考。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wander_activities WHERE created_at LIKE ? ORDER BY created_at",
                (f"{date_str}%",),
            ).fetchall()
        return [self._decode_activity(row) for row in rows]

    def list_nodes(self, activity_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM wander_nodes WHERE activity_id=? ORDER BY round_index", (activity_id,)).fetchall()
        return [self._decode_node(row) for row in rows]

    def list_recent_listen_music_fingerprints(self, days: int = 3) -> set[str]:
        """近期 listen_music 活动已听过的歌曲 fingerprint（跨活动去重用）。

        每首听过的歌带唯一 fingerprint，AI 跨 activity 重复听时据此排除，
        防止「整夜反复听同一首」——单 activity 内的 seen_nodes 排除覆盖不到新 activity。
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT n.source_payload FROM wander_nodes n
                    JOIN wander_activities a ON n.activity_id = a.activity_id
                    WHERE a.activity_type = ? AND n.created_at >= ? AND n.source_payload IS NOT NULL
                    """,
                    ("listen_music", cutoff),
                ).fetchall()
        except Exception:
            return set()
        fingerprints: set[str] = set()
        for (raw,) in rows:
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("fingerprint"):
                fingerprints.add(payload["fingerprint"])
        return fingerprints

    def list_recent_listen_music_identities(self, days: int = 3) -> set[str]:
        """Return normalized ``title|artist`` identities from recent nodes.

        Fingerprints are the preferred identity, but old nodes and external
        search results can use different hashing/normalization.  Keeping the
        normalized human identity as a second factual key closes that gap
        without changing the persisted node payload.
        """
        import re

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT n.source_payload FROM wander_nodes n
                    JOIN wander_activities a ON n.activity_id = a.activity_id
                    WHERE a.activity_type = ? AND n.created_at >= ? AND n.source_payload IS NOT NULL
                    """,
                    ("listen_music", cutoff),
                ).fetchall()
        except Exception:
            return set()

        identities: set[str] = set()
        for (raw,) in rows:
            try:
                payload = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                payload = None
            if not isinstance(payload, dict):
                continue
            title = str(payload.get("title") or payload.get("song_name") or "").strip()
            artist = str(payload.get("artist") or "").strip()
            if not title or not artist or artist.casefold() in {"未知", "unknown", "?"}:
                continue
            key = re.sub(r"[^\w\u4e00-\u9fff]+", "", f"{title}|{artist}".casefold())
            if key:
                identities.add(key)
        return identities

    def has_recent_failed_activity(self, activity_type: str, *, minutes: int = 15) -> bool:
        """Return whether a same-type node materially failed recently.

        This is intentionally a read-only query against the v3 runtime DB.  It
        is used only to hide a repeatedly failing material source temporarily;
        successful nodes, natural skips, and user interruptions do not create
        this cooldown.
        """
        try:
            cutoff = (datetime.now() - timedelta(minutes=max(1, int(minutes)))).isoformat()
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM wander_nodes n
                    JOIN wander_activities a ON a.activity_id=n.activity_id
                    WHERE a.activity_type=?
                      AND n.execution_status='failed'
                      AND COALESCE(n.completed_at, n.updated_at, n.created_at)>=?
                    ORDER BY COALESCE(n.completed_at, n.updated_at, n.created_at) DESC
                    LIMIT 1
                    """,
                    (str(activity_type), cutoff),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def get_active_run(self, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        terminal = tuple(state.value for state in (RunState.COMPLETED, RunState.INTERRUPTED, RunState.ABORTED))
        query = "SELECT * FROM wander_runs WHERE state NOT IN (?, ?, ?)"
        params: tuple[Any, ...] = terminal
        if session_id is not None:
            query += " AND session_id=?"
            params += (session_id,)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("probability_snapshot", "context_snapshot"):
            result[key] = _load_json(result[key])
        return result

    def get_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM wander_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("input_context", "raw_output", "reasoning", "parsed_output"):
            result[key] = _load_json(result[key])
        return result

    def list_decisions(
        self,
        run_id: str,
        phase: Optional[DecisionPhase | str] = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM wander_decisions WHERE run_id=?"
        params: tuple[Any, ...] = (run_id,)
        if phase is not None:
            query += " AND phase=?"
            params += (DecisionPhase(phase).value,)
        query += " ORDER BY created_at, rowid"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for key in ("input_context", "raw_output", "reasoning", "parsed_output"):
                item[key] = _load_json(item[key])
            results.append(item)
        return results

    def get_delivery(self, delivery_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM wander_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tool_calls"] = _load_json(result["tool_calls"])
        return result

    def has_delivery_for_activity(self, activity_id: str) -> bool:
        with self._connection() as conn:
            return conn.execute(
                "SELECT 1 FROM wander_deliveries WHERE activity_id=? LIMIT 1",
                (activity_id,),
            ).fetchone() is not None

    def get_delivery_for_activity(self, activity_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM wander_deliveries WHERE activity_id=? ORDER BY created_at, rowid LIMIT 1",
                (activity_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tool_calls"] = _load_json(result["tool_calls"])
        return result

    def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        message_id: Optional[str] = None,
        message_content: Optional[str] = None,
        tool_calls: Any = None,
    ) -> None:
        assignments = ["status=?"]
        values: list[Any] = [status]
        if message_id is not None:
            assignments.append("message_id=?")
            values.append(message_id)
        if message_content is not None:
            assignments.append("message_content=?")
            values.append(_text(message_content))
        if tool_calls is not None:
            assignments.append("tool_calls=?")
            values.append(_json(tool_calls))
        values.append(delivery_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE wander_deliveries SET {', '.join(assignments)} WHERE delivery_id=?",
                tuple(values),
            )
            if cursor.rowcount != 1:
                raise ValueError("delivery not found")

    def list_activity_logs(
        self,
        *,
        limit: int = 50,
        pushed: Optional[bool] = None,
        hours: int = 24,
        date: str = "",
    ) -> list[dict[str, Any]]:
        """Project runtime activities into the stable frontend log shape."""
        clauses = []
        params: list[Any] = []
        if date:
            clauses.append("substr(a.created_at, 1, 10)=?")
            params.append(date)
        elif hours:
            clauses.append("a.created_at>=?")
            params.append((datetime.now() - timedelta(hours=hours)).isoformat())
        if pushed is not None:
            clauses.append(
                ("EXISTS" if pushed else "NOT EXISTS")
                + " (SELECT 1 FROM wander_deliveries d2 WHERE d2.activity_id=a.activity_id AND d2.status='sent')"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # ``limit=0`` is the explicit full-result mode used by the daily log
        # view.  Positive limits remain bounded by the caller's requested
        # value; no arbitrary 500-row ceiling should silently hide today's
        # activity.
        limit_value = int(limit)
        if limit_value < 0:
            limit_value = 50
        limit_clause = ""
        if limit_value > 0:
            limit_clause = " LIMIT ?"
            params.append(limit_value)
        with self._connection() as conn:
            schedule_row = conn.execute(
                "SELECT next_plan_at, wake_reason, source_activity_id, source_run_id, failure_streak "
                "FROM wander_scheduler_wakes ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            schedule_projection = dict(schedule_row) if schedule_row is not None else {}
            rows = conn.execute(
                f"""SELECT a.*, r.trigger_reason, r.state AS run_state, r.outcome AS run_outcome,
                    r.created_at AS run_created_at,
                    r.ended_at AS run_ended_at,
                    COUNT(*) OVER (PARTITION BY a.run_id) AS run_activity_count,
                    (SELECT status FROM wander_deliveries d WHERE d.activity_id=a.activity_id
                     ORDER BY d.created_at, d.rowid LIMIT 1) AS delivery_status
                    FROM wander_activities a
                    JOIN wander_runs r ON r.run_id=a.run_id
                    {where}
                    ORDER BY a.created_at DESC, a.rowid DESC{limit_clause}""",
                tuple(params),
            ).fetchall()
            # Run boundaries are projected separately from node execution so
            # idle scheduling gaps never get counted as work duration.
            prior_run_end_cache: dict[str, Optional[str]] = {}
            logs = []
            for row in rows:
                item = dict(row)
                # Legacy rows are intentionally not migrated.  When an old
                # planned activity belongs to a terminal run, project the
                # truthful UI state at read time instead of mutating history.
                projected_status = item["state"]
                projected_abort_reason = item.get("abort_reason") or ""
                if item["state"] == ActivityState.PLANNED.value and item.get("run_state") in {
                    RunState.COMPLETED.value,
                    RunState.INTERRUPTED.value,
                    RunState.ABORTED.value,
                }:
                    outcome = str(item.get("run_outcome") or "")
                    if outcome == SettlementReason.USER_INTERRUPT.value or item.get("run_state") == RunState.INTERRUPTED.value:
                        projected_abort_reason = "not_started_after_user_interrupt"
                    elif outcome == SettlementReason.EXECUTION_ERROR.value or item.get("run_state") == RunState.ABORTED.value:
                        projected_abort_reason = "not_started_after_prior_execution_error"
                    else:
                        projected_abort_reason = "not_started_after_terminal_run"
                    projected_status = "not_started"
                item_schedule = {}
                if schedule_projection and (
                    schedule_projection.get("source_activity_id") == item["activity_id"]
                    or (
                        not schedule_projection.get("source_activity_id")
                        and schedule_projection.get("source_run_id") == item["run_id"]
                    )
                ):
                    item_schedule = schedule_projection
                nodes = conn.execute(
                    "SELECT round_index, state, source_summary, source_payload, reflection, execution_status, execution_error, "
                    "started_at, completed_at "
                    "FROM wander_nodes WHERE activity_id=? ORDER BY round_index",
                    (item["activity_id"],),
                ).fetchall()
                node_projection = []
                for node in nodes:
                    node_duration = _duration_seconds(node["started_at"], node["completed_at"])
                    payload = _load_json(node["source_payload"])
                    if not isinstance(payload, dict):
                        payload = {}
                    failure_stage = _text(payload.get("failure_stage") or "")[:80]
                    public_error = _text(payload.get("public_error") or "")[:300]
                    node_projection.append({
                        "round_index": node["round_index"],
                        "state": node["state"],
                        "status": node["execution_status"],
                        "started_at": node["started_at"],
                        "completed_at": node["completed_at"],
                        "duration_seconds": node_duration,
                        "source_summary": node["source_summary"],
                        "reflection": node["reflection"],
                        "error": node["execution_error"] or "",
                        "failure_stage": failure_stage,
                        "public_error": public_error,
                    })
                node_lines = [
                    f"节点{node['round_index']} [{node['execution_status']}] "
                    f"{node['source_summary'] or '无摘要'}"
                    + (f"；耗时：{_format_duration(node_duration)}" if (node_duration := _duration_seconds(node['started_at'], node['completed_at'])) is not None else "")
                    + (f"；感想：{node['reflection']}" if node["reflection"] else "")
                    + (f"；错误：{node['execution_error']}" if node["execution_error"] else "")
                    for node in nodes
                ]
                process = "\n".join(node_lines)
                if projected_status == "not_started":
                    process += ("\n" if process else "") + (
                        "未执行·本轮被打断"
                        if projected_abort_reason == "not_started_after_user_interrupt"
                        else "未执行·前序活动失败"
                        if projected_abort_reason == "not_started_after_prior_execution_error"
                        else "未执行·所属运行已终止"
                    )
                if item["summary"]:
                    process += ("\n" if process else "") + f"结算：{item['summary']}"
                activity_duration = _duration_seconds(item["created_at"], item["ended_at"])
                execution_duration = round(sum(
                    _duration_seconds(node["started_at"], node["completed_at"]) or 0.0
                    for node in nodes
                ), 3)
                node_failures = [
                    node for node in node_projection
                    if node.get("failure_stage") or node.get("public_error")
                ]
                failure_stage = node_failures[0].get("failure_stage", "") if node_failures else ""
                public_error = node_failures[0].get("public_error", "") if node_failures else ""
                run_created_at = item.get("run_created_at") or ""
                if run_created_at not in prior_run_end_cache:
                    previous = conn.execute(
                        "SELECT MAX(ended_at) AS ended_at FROM wander_runs "
                        "WHERE ended_at IS NOT NULL AND ended_at <= ?",
                        (run_created_at or item["created_at"],),
                    ).fetchone()
                    prior_run_end_cache[run_created_at] = previous["ended_at"] if previous else None
                previous_run_ended_at = prior_run_end_cache.get(run_created_at)
                idle_schedule_duration = (
                    _duration_seconds(previous_run_ended_at, run_created_at)
                    if previous_run_ended_at and run_created_at else None
                )
                logs.append({
                    "event_type": item["activity_type"],
                    "timestamp": item["created_at"],
                    "description": item["summary"] or item["reason"] or item["activity_type"],
                    "process_log": process,
                    "event_id": item["activity_id"],
                    "created_at": item["created_at"],
                    "ended_at": item["ended_at"],
                    "duration_seconds": activity_duration,
                    "execution_duration_seconds": execution_duration,
                    "state": item["state"],
                    "projected_status": projected_status,
                    "abort_reason": projected_abort_reason,
                    "failure_stage": failure_stage,
                    "public_error": public_error,
                    "nodes": node_projection,
                    "details": {
                        "run_id": item["run_id"],
                        "activity_id": item["activity_id"],
                        "state": item["state"],
                        "projected_status": projected_status,
                        "abort_reason": projected_abort_reason,
                        "failure_stage": failure_stage,
                        "public_error": public_error,
                        "activity_order": item["order_index"],
                        "run_activity_count": item["run_activity_count"],
                        "created_at": item["created_at"],
                        "ended_at": item["ended_at"],
                        "duration_seconds": activity_duration,
                        "execution_duration_seconds": execution_duration,
                        "nodes": node_projection,
                        "goal_mode": item["goal_mode"],
                        "goal_value": item["goal_value"],
                        "settlement_reason": item["settlement_reason"],
                        "delivery_status": item["delivery_status"] or "not_created",
                        "trigger_reason": item["trigger_reason"],
                        "run_created_at": run_created_at,
                        "run_ended_at": item.get("run_ended_at"),
                        "previous_run_ended_at": previous_run_ended_at,
                        "idle_schedule_seconds": idle_schedule_duration,
                        # Scheduler facts are separate from activity duration;
                        # the UI may show them as a future wake, never as work.
                        "next_plan_at": item_schedule.get("next_plan_at"),
                        "wake_reason": item_schedule.get("wake_reason", ""),
                        "schedule_source_activity_id": item_schedule.get("source_activity_id", ""),
                        "schedule_source_run_id": item_schedule.get("source_run_id", ""),
                        "schedule_failure_streak": item_schedule.get("failure_streak", 0),
                    },
                    "judgment_result": {
                        "share": bool(item["share_decision"]) if item["share_decision"] is not None else None,
                        "continue_next": bool(item["continue_next"]) if item["continue_next"] is not None else None,
                    },
                    "pushed": item["delivery_status"] == "sent",
                })
        return logs

    def list_recent_inner_wander(
        self,
        *,
        hours: int = 36,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return a bounded, read-only projection for continuity context.

        The runtime database remains authoritative.  This method deliberately
        does not expose ``source_payload`` (which may contain raw external
        material or device evidence); only settlement/node summaries,
        reflections, emotion deltas and delivery facts cross the context
        boundary.  Unshared activities are ordered first so a later thought
        can still see material that never reached the chat surface.
        """
        try:
            bounded_hours = max(1, min(int(hours), 36))
            bounded_limit = max(1, min(int(limit), 10))
        except (TypeError, ValueError):
            bounded_hours, bounded_limit = 36, 10
        cutoff = (datetime.now() - timedelta(hours=bounded_hours)).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT a.*,
                    (SELECT status FROM wander_deliveries d
                     WHERE d.activity_id=a.activity_id
                     ORDER BY d.created_at, d.rowid LIMIT 1) AS delivery_status
                   FROM wander_activities a
                   WHERE a.created_at >= ?
                     AND a.state IN ('completed', 'interrupted', 'aborted')
                   ORDER BY
                     CASE WHEN EXISTS (
                       SELECT 1 FROM wander_deliveries d2
                       WHERE d2.activity_id=a.activity_id AND d2.status='sent'
                     ) THEN 1 ELSE 0 END,
                     COALESCE(a.ended_at, a.created_at) DESC,
                     a.created_at DESC
                   LIMIT ?""",
                (cutoff, bounded_limit),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                activity = dict(row)
                node_rows = conn.execute(
                    """SELECT round_index, source_summary, reflection,
                              emotion_effect, execution_status, started_at,
                              completed_at
                         FROM wander_nodes
                        WHERE activity_id=?
                        ORDER BY round_index DESC
                        LIMIT 4""",
                    (activity["activity_id"],),
                ).fetchall()
                nodes: list[dict[str, Any]] = []
                for node in reversed(node_rows):
                    emotion = _load_json(node["emotion_effect"])
                    nodes.append({
                        "round_index": node["round_index"],
                        "source_summary": _text(node["source_summary"])[:180],
                        "reflection": _text(node["reflection"])[:180],
                        "emotion_effect": redact_sensitive(emotion),
                        "execution_status": node["execution_status"],
                        "started_at": node["started_at"],
                        "completed_at": node["completed_at"],
                    })
                share = activity["share_decision"]
                result.append({
                    "activity_id": activity["activity_id"],
                    "event_type": activity["activity_type"],
                    "settled_at": activity["ended_at"] or activity["created_at"],
                    "summary": _text(activity["summary"])[:300],
                    "emotion_effect": redact_sensitive(_load_json(activity["emotion_effect"])),
                    "share": None if share is None else bool(share),
                    "delivery": activity["delivery_status"] or "not_created",
                    "nodes": nodes,
                })
            return result

    def activity_log_stats(
        self,
        *,
        pushed: Optional[bool] = None,
        hours: int = 24,
        date: str = "",
        last_viewed_at: str = "",
    ) -> dict[str, Any]:
        logs = self.list_activity_logs(limit=0, pushed=pushed, hours=hours, date=date)
        type_counts: dict[str, int] = {}
        for item in logs:
            event_type = item["event_type"]
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        new_since = 0
        if last_viewed_at:
            try:
                viewed = datetime.fromisoformat(last_viewed_at).replace(tzinfo=None)
                new_since = sum(
                    1 for item in logs
                    if datetime.fromisoformat(item["timestamp"]).replace(tzinfo=None) > viewed
                )
            except ValueError:
                pass
        return {
            "total_entries": len(logs),
            "type_counts": type_counts,
            "pushed_count": sum(1 for item in logs if item["pushed"]),
            "retention_hours": hours,
            "persistence_enabled": True,
            "new_since_last_viewed": new_since,
            "source": "wander_runtime",
        }
