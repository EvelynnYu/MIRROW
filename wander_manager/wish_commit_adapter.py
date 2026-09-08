"""Crash-safe handoff from an audited self-reflection to the real wish store.

The idempotency boundary is the production wishes SQLite database itself. A
retry after a process crash may repeat this adapter, but it cannot increment the
same reflection observation twice.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .event_types import EventType
from .runtime_models import DecisionPhase, NodeState, WanderNode, now_iso
from .runtime_store import WanderRuntimeStore
from .wish_board_service import WishMigrationRequired
from .wish_store import WishStore


class WishCommitAdapter:
    def __init__(self, runtime_store: WanderRuntimeStore, wish_store: WishStore):
        self.runtime_store = runtime_store
        self.wish_store = wish_store

    def commit(self, node_id: str) -> list[dict[str, Any]]:
        node = self._required_node(node_id)
        activity = self.runtime_store.get_activity(node.activity_id)
        if activity is None:
            raise ValueError("activity not found")
        run = self.runtime_store.get_run(activity["run_id"])
        if run is None:
            raise ValueError("run not found")
        if EventType(activity["activity_type"]) != EventType.SELF_REFLECTION:
            raise ValueError("node is not a self-reflection activity")
        if node.execution_status != "succeeded":
            raise ValueError("self-reflection generation has not succeeded")

        payload = dict(node.source_payload or {})
        if payload.get("wish_commit_status") in {"committed", "migration_required"}:
            return list(payload.get("wish_commit_results") or [])
        action = payload.get("wish_action")
        comment_reply = payload.get("comment_reply")
        # New nodes carry the field even for an explicit ``type=none``.  The
        # presence of the contract marker is therefore the boundary; deciding
        # based on the action type would incorrectly fall back to the old
        # three-wish array on a perfectly valid no-op reflection.
        structured = (
            payload.get("wish_contract_version") == 2
            or "wish_action" in payload
            or "comment_reply" in payload
        )
        wishes = payload.get("wishes") or []
        if not structured and not isinstance(wishes, list):
            raise ValueError("self-reflection wishes payload is invalid")

        audit_id = hashlib.sha256(f"wish_commit:{node.node_id}".encode("utf-8")).hexdigest()[:32]
        action_type = str(action.get("type") or "none").strip().lower() if isinstance(action, dict) else "none"
        comment_content = comment_reply.get("content") if isinstance(comment_reply, dict) else None
        is_noop = structured and action_type == "none" and not str(comment_content or "").strip()
        try:
            # A structured no-op is a runtime fact, not a wish-store write.
            # It must work while an old board schema remains read-only and
            # must never fall back to the legacy wishes[] path.
            if is_noop:
                results = []
            elif structured:
                results = self.wish_store.commit_reflection_action(
                    run_id=run["run_id"],
                    activity_id=activity["activity_id"],
                    node_id=node.node_id,
                    action=action,
                    comment_reply=comment_reply,
                )
            else:
                results = self.wish_store.commit_reflection_wishes(
                    run_id=run["run_id"],
                    activity_id=activity["activity_id"],
                    node_id=node.node_id,
                    wishes=wishes,
                )
        except WishMigrationRequired as exc:
            self.runtime_store.record_decision(
                run_id=run["run_id"],
                activity_id=activity["activity_id"],
                node_id=node.node_id,
                phase=DecisionPhase.WISH_COMMIT,
                model="code",
                recipe="NONE",
                input_context={
                    "wish_count": len(wishes) if isinstance(wishes, list) else 0,
                    "structured_action": structured,
                    "action_type": action_type if structured else "legacy",
                },
                parsed_output={"mutations": [], "mutation": False},
                status="migration_required",
                error=f"{type(exc).__name__}: {exc}",
            )
            payload["wish_commit_status"] = "migration_required"
            payload["wish_commit_error"] = str(exc)
            payload["wish_commit_results"] = []
            payload["wish_commit_mutation"] = False
            node.source_payload = payload
            node.side_effect_refs = {
                **node.side_effect_refs,
                "wish_observation_ids": [],
                "wish_mutation": False,
                "wish_commit_status": "migration_required",
            }
            self.runtime_store.save_node(node)
            return []
        except Exception as exc:
            self.runtime_store.record_decision(
                run_id=run["run_id"],
                activity_id=activity["activity_id"],
                node_id=node.node_id,
                phase=DecisionPhase.WISH_COMMIT,
                model="code",
                recipe="NONE",
                input_context={
                    "wish_count": len(wishes) if isinstance(wishes, list) else 0,
                    "structured_action": structured,
                    "action_type": action_type if structured else "legacy",
                },
                parsed_output={},
                status="error",
                error=type(exc).__name__,
            )
            raise

        if self.runtime_store.get_decision(audit_id) is None:
            self.runtime_store.record_decision(
                decision_id=audit_id,
                run_id=run["run_id"],
                activity_id=activity["activity_id"],
                node_id=node.node_id,
                phase=DecisionPhase.WISH_COMMIT,
                model="code",
                recipe="NONE",
                input_context={
                    "wish_count": len(wishes) if isinstance(wishes, list) else 0,
                    "structured_action": structured,
                    "action_type": action_type if structured else "legacy",
                },
                parsed_output={"mutations": results},
                status="ok",
            )

        payload["wish_commit_status"] = "committed"
        payload["wish_commit_outcome"] = "no_op" if is_noop else "mutated" if self.has_mutation(results) else "unchanged"
        payload["wish_committed_at"] = now_iso()
        payload["wish_commit_results"] = results
        payload["wish_commit_mutation"] = self.has_mutation(results)
        node.source_payload = payload
        node.side_effect_refs = {
            **node.side_effect_refs,
            "wish_observation_ids": [item["id"] for item in results if item.get("id") is not None],
            "wish_mutation": self.has_mutation(results),
        }
        self.runtime_store.save_node(node)
        return results

    @staticmethod
    def has_mutation(results: list[dict[str, Any]]) -> bool:
        """Return whether a commit changed the board or comment timeline.

        Structured service results carry ``mutation`` directly.  The legacy
        observation result has no such field, so only its actual create/merge
        outcomes count; replay, duplicate, fulfilled-audit and no-op outcomes
        do not cause a user-facing board notification.
        """
        for item in results or []:
            if item.get("mutation") is True:
                return True
            if item.get("idempotent_replay"):
                continue
            if item.get("outcome") in {"created", "merged", "reaffirmed", "commented", "retained", "deleted"}:
                return True
        return False

    def _required_node(self, node_id: str) -> WanderNode:
        data = self.runtime_store.get_node(node_id)
        if data is None:
            raise ValueError("node not found")
        data["state"] = NodeState(data["state"])
        return WanderNode(**data)
