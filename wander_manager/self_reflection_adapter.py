"""Pure, auditable self-reflection generation for the Wander runtime.

The adapter creates pending self-book evidence and records wishes, but it does
not mutate the real self book or the production wish database.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from .event_types import EventType
from .flash_structured import FlashJsonResult, call_flash_json_detailed
from .node_execution_adapter import NodeExecutionStatus
from .runtime_models import DecisionPhase, NodeState, WanderNode
from .runtime_store import WanderRuntimeStore, redact_sensitive


SELF_BOOK_CATEGORIES = {
    "习惯",
    "倾向与偏好",
    "认知与价值观",
    "自我认知",
    "能力与边界",
}


@dataclass
class SelfReflectionContext:
    persona: str
    session_id: str
    current_time: str = ""
    user_status: str = ""
    trigger_reason: str = ""
    capability_catalog: str = ""
    brain_summary: str = ""
    wish_context: str = ""
    existing_self_book: str = ""
    pending_candidates: str = ""
    today_conversation: str = ""


@dataclass
class CapabilityObservation:
    statement: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class WishObservation:
    feature: str
    reason: str = ""
    novelty: str = "new"


@dataclass
class WishAction:
    """The one structured board mutation AI may request in one reflection."""

    type: str = "none"
    wish_id: Optional[int] = None
    title: str = ""
    reason: str = ""
    basis: str = ""


@dataclass
class CommentReply:
    wish_id: int
    content: str
    reply_to_comment_id: Optional[int] = None


@dataclass
class SelfObservationCandidate:
    title: str
    category: str
    statement: str
    keywords: list[str] = field(default_factory=list)
    evidence_node_ids: list[str] = field(default_factory=list)
    evidence_summary: str = ""
    confidence: float = 0.0
    stability: str = "tentative"


@dataclass
class ShareCandidate:
    should_share: bool = False
    reason: str = ""
    summary: str = ""


@dataclass
class SelfReflectionDecision:
    overall_reflection: str
    capabilities: list[CapabilityObservation] = field(default_factory=list)
    wishes: list[WishObservation] = field(default_factory=list)
    wish_action: WishAction = field(default_factory=WishAction)
    comment_reply: Optional[CommentReply] = None
    self_observations: list[SelfObservationCandidate] = field(default_factory=list)
    share_candidate: ShareCandidate = field(default_factory=ShareCandidate)
    validation_rejections: list[str] = field(default_factory=list)


_OUTPUT_INSTRUCTION = """仅输出 JSON 对象：
{
  "overall_reflection":"这次自省的总体感受",
  "capabilities":[{"statement":"我能……","evidence":["可验证事实"],"confidence":0.0}],
  "wish_action":{"type":"none|create|reaffirm|retain_impossible|delete_impossible","wish_id":null,"title":"新愿望标题","reason":"这次具体依据或理由","basis":"支持这次动作的新增事实"},
  "comment_reply":{"wish_id":null,"reply_to_comment_id":null,"content":"对用户评论的回复"},
  "self_observations":[{"title":"短标题","category":"习惯|倾向与偏好|认知与价值观|自我认知|能力与边界","statement":"第一人称稳定倾向，80字内","keywords":["关键词"],"evidence_node_ids":[],"evidence_summary":"证据摘要","confidence":0.0,"stability":"tentative"}],
  "share_candidate":{"should_share":false,"reason":"","summary":""}
}
能力必须来自可验证能力目录；每次最多一个 wish_action 和一个 comment_reply。
先阅读许愿板快照：create 只用于现有愿望、已实现历史和删除记录均未覆盖的新具体愿望；reaffirm 必须使用快照里的稳定 wish_id，且只有新的具体依据才增加次数；不要输出 fulfilled。
用户标记天方夜谭的愿望只能用 retain_impossible 或 delete_impossible，并使用稳定 wish_id。没有动作时使用 none。没有想回复的评论时将 comment_reply 设为 null；一旦回复，reply_to_comment_id 必须引用快照中同一愿望的真实用户留言。历史留言不会因已有 AI 留言而失效，请结合留言时间和已有直接回复时间自由判断是否需要补充。
单次最多一个自我观察，无充分证据则返回空数组。share_candidate 只是候选，不会直接推送。"""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "是"}


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _strings(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:limit]:
        text = str(item or "").strip()[:item_limit]
        if text and text not in result:
            result.append(text)
    return result


def _safe_text(value: Any, limit: int | None) -> str:
    redacted = redact_sensitive(value)
    text = redacted if isinstance(redacted, str) else json.dumps(
        redacted, ensure_ascii=False, default=str
    )
    return text if limit is None else text[:limit]


class SelfReflectionAdapter:
    def __init__(
        self,
        store: WanderRuntimeStore,
        llm_caller: Callable[..., Awaitable[FlashJsonResult]] = call_flash_json_detailed,
        context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        self.store = store
        self.llm_caller = llm_caller
        self.context_builder = context_builder

    async def generate(
        self,
        node_id: str,
        context: SelfReflectionContext,
    ) -> Optional[SelfReflectionDecision]:
        node = self._required_node(node_id)
        activity = self._required_activity(node.activity_id)
        run = self._required_run(activity["run_id"])
        if EventType(activity["activity_type"]) != EventType.SELF_REFLECTION:
            raise ValueError("node is not a self-reflection activity")
        if node.state != NodeState.RUNNING:
            raise ValueError("node must be running")
        if node.execution_status != NodeExecutionStatus.PENDING.value:
            raise ValueError("self-reflection execution was already claimed")

        node.execution_status = NodeExecutionStatus.RUNNING.value
        self.store.save_node(node)
        runtime_block = self._runtime_block(run, activity, context)
        try:
            messages = await self._messages(run, context, runtime_block)
        except ValueError as exc:
            self._record_failure(run, activity, node, runtime_block, "context_error", str(exc))
            self._fail_node(node, str(exc))
            return None

        try:
            detailed = await self.llm_caller(
                messages=messages,
                temperature=0.0,
                max_tokens=16384,
            )
        except Exception as exc:
            self._record_failure(run, activity, node, runtime_block, "llm_error", type(exc).__name__)
            self._fail_node(node, type(exc).__name__)
            return None

        raw = detailed.parsed if detailed.status == "ok" and isinstance(detailed.parsed, dict) else None
        valid_node_ids = self._evidence_node_ids(run["run_id"])
        decision = self._normalize(raw, valid_node_ids) if raw is not None else None
        status = detailed.status if decision is not None else (
            "validation_error" if raw is not None else detailed.status
        )
        error = detailed.error or ("invalid_self_reflection" if raw is not None and decision is None else "")
        decision_id = uuid4().hex
        self.store.record_decision(
            decision_id=decision_id,
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node.node_id,
            phase=DecisionPhase.SELF_REFLECTION,
            model=detailed.model,
            recipe="WANDER_ACTIVITY",
            temperature=detailed.temperature,
            input_context={
                "system": messages[0]["content"],
                "runtime": runtime_block,
                "user": _OUTPUT_INSTRUCTION,
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
        if decision is None:
            self._fail_node(node, error or status)
            return None

        candidates = [
            self._candidate_row(node.node_id, index, candidate)
            for index, candidate in enumerate(decision.self_observations)
        ]
        candidate_ids = self.store.save_self_book_candidates(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node.node_id,
            decision_id=decision_id,
            candidates=candidates,
        )
        node.execution_status = NodeExecutionStatus.SUCCEEDED.value
        node.completion_signal = "self_reflection_generated"
        node.source_summary = f"完成一次自省：{decision.overall_reflection[:80]}"
        decision_payload = asdict(decision)
        # ``wishes`` belonged to the pre-stable-ID contract.  Do not persist
        # even the dataclass compatibility field on newly generated nodes;
        # WishCommitAdapter uses this marker to avoid the legacy write path.
        decision_payload.pop("wishes", None)
        node.source_payload = {
            **decision_payload,
            "decision_id": decision_id,
            "wish_contract_version": 2,
            "self_book_candidate_ids": candidate_ids,
            "wish_commit_status": "not_committed",
        }
        node.execution_error = ""
        node.retry_safe = False
        self.store.save_node(node)
        return decision

    async def _messages(
        self,
        run: dict[str, Any],
        context: SelfReflectionContext,
        runtime_block: str,
    ) -> list[dict[str, str]]:
        if not context.persona.strip():
            raise ValueError("persona_is_required")
        if context.session_id != run["session_id"]:
            raise ValueError("session_id_mismatch")
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
        except Exception as exc:
            raise ValueError(f"recipe_build:{type(exc).__name__}") from exc
        system_content = getattr(built, "system_content", "")
        sections = getattr(built, "sections", {}) or {}
        if (
            not system_content
            or not sections.get("wander_runtime", {}).get("text")
            or not system_content.rstrip().endswith(runtime_block.rstrip())
        ):
            raise ValueError("runtime_block_not_final_or_missing")
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": _OUTPUT_INSTRUCTION},
        ]

    def _runtime_block(
        self,
        run: dict[str, Any],
        activity: dict[str, Any],
        context: SelfReflectionContext,
    ) -> str:
        now = context.current_time or datetime.now().strftime("%Y/%m/%d %H:%M")
        return "\n".join(
            (
                "[本次调用阶段]\nself_reflection_generation",
                f"[当前时间]\n{now}",
                f"[用户状态]\n{context.user_status or '未知'}",
                f"[今日近期对话]\n{_safe_text(context.today_conversation or '暂无', 6000)}",
                f"[触发原因]\n{context.trigger_reason or run.get('trigger_reason') or '自主倾向'}",
                f"[AI可验证能力目录]\n{_safe_text(context.capability_catalog or '暂无', 5000)}",
                f"[大脑结构摘要]\n{_safe_text(context.brain_summary or '暂无', 5000)}",
                # The model and board UI consume the same complete wish/comment
                # projection; never cut a wish or a comment in the middle.
                f"[许愿板历史状态快照]\n{_safe_text(context.wish_context or '暂无', None)}",
                f"[相关既有自我书]\n{_safe_text(context.existing_self_book or '暂无', 3000)}",
                f"[相似待核验候选]\n{_safe_text(context.pending_candidates or '暂无', 2500)}",
                f"[活动身份]\nrun_id={run['run_id']}；activity_id={activity['activity_id']}",
                "[写入边界]\n本次只生成候选；不会直接修改自我书，也不会提交愿望次数。",
            )
        )

    @staticmethod
    def _normalize(
        raw: dict[str, Any],
        valid_node_ids: set[str],
    ) -> Optional[SelfReflectionDecision]:
        overall = str(raw.get("overall_reflection") or "").strip()[:500]
        rejections: list[str] = []

        capabilities = []
        for index, item in enumerate(raw.get("capabilities") or []):
            if isinstance(item, str):
                item = {"statement": item}
            if not isinstance(item, dict):
                rejections.append(f"capability[{index}]:not_object")
                continue
            statement = str(item.get("statement") or "").strip()[:120]
            if not statement:
                rejections.append(f"capability[{index}]:missing_statement")
                continue
            capabilities.append(
                CapabilityObservation(
                    statement,
                    _strings(item.get("evidence"), limit=5, item_limit=160),
                    _confidence(item.get("confidence")),
                )
            )
            if len(capabilities) >= 5:
                break

        # A model response containing the old array is not allowed to create
        # or reaffirm anything.  Already-persisted legacy nodes are handled by
        # WishCommitAdapter, but a fresh generation must use wish_action.
        if "wishes" in raw:
            rejections.append("wishes:legacy_contract_ignored")

        # The current contract has one explicit, stable-ID action.  The old
        # ``wishes`` array above is intentionally still normalized for replay
        # of already-created v2 nodes, but new nodes must use this branch.
        action_raw = raw.get("wish_action")
        action = WishAction()
        if action_raw is not None:
            if not isinstance(action_raw, dict):
                rejections.append("wish_action:not_object")
            else:
                action_type = str(action_raw.get("type") or "none").strip().lower()
                if action_type not in {"none", "create", "reaffirm", "retain_impossible", "delete_impossible"}:
                    rejections.append("wish_action:invalid_type")
                    action_type = "none"
                raw_wish_id = action_raw.get("wish_id")
                try:
                    wish_id = int(raw_wish_id) if raw_wish_id is not None else None
                except (TypeError, ValueError):
                    wish_id = None
                title = str(action_raw.get("title") or action_raw.get("feature") or "").strip()[:120]
                reason = str(action_raw.get("reason") or "").strip()[:500]
                basis = str(action_raw.get("basis") or "").strip()[:500]
                if action_type == "create" and not title:
                    rejections.append("wish_action:create_missing_title")
                    action_type = "none"
                elif action_type in {"reaffirm", "retain_impossible", "delete_impossible"} and (wish_id is None or wish_id <= 0):
                    rejections.append("wish_action:missing_wish_id")
                    action_type = "none"
                elif action_type == "reaffirm" and not basis:
                    rejections.append("wish_action:reaffirm_missing_basis")
                    action_type = "none"
                action = WishAction(action_type, wish_id, title, reason, basis)

        comment_reply = None
        comment_raw = raw.get("comment_reply")
        if comment_raw is not None:
            if not isinstance(comment_raw, dict):
                rejections.append("comment_reply:not_object")
            else:
                try:
                    reply_wish_id = int(comment_raw.get("wish_id"))
                except (TypeError, ValueError):
                    reply_wish_id = 0
                content = str(comment_raw.get("content") or "").strip()[:500]
                raw_reply_to = comment_raw.get("reply_to_comment_id")
                try:
                    reply_to = int(raw_reply_to) if raw_reply_to is not None else None
                except (TypeError, ValueError):
                    reply_to = None
                if reply_wish_id <= 0 or not content:
                    rejections.append("comment_reply:missing_wish_id_or_content")
                elif reply_to is None or reply_to <= 0:
                    rejections.append("comment_reply:missing_candidate_reply_to_comment_id")
                else:
                    comment_reply = CommentReply(reply_wish_id, content, reply_to)

        observations = []
        for index, item in enumerate((raw.get("self_observations") or [])[:1]):
            if not isinstance(item, dict):
                rejections.append(f"self_observation[{index}]:not_object")
                continue
            category = str(item.get("category") or "").strip()
            statement = str(item.get("statement") or "").strip()[:80]
            evidence_summary = str(item.get("evidence_summary") or "").strip()[:500]
            confidence = _confidence(item.get("confidence"))
            evidence_ids = [
                node_id
                for node_id in _strings(item.get("evidence_node_ids"), limit=8, item_limit=64)
                if node_id in valid_node_ids
            ]
            if category not in SELF_BOOK_CATEGORIES:
                rejections.append(f"self_observation[{index}]:invalid_category")
                continue
            if not statement or not evidence_summary:
                rejections.append(f"self_observation[{index}]:missing_evidence")
                continue
            if confidence < 0.5:
                rejections.append(f"self_observation[{index}]:low_confidence")
                continue
            observations.append(
                SelfObservationCandidate(
                    title=str(item.get("title") or statement[:10]).strip()[:20],
                    category=category,
                    statement=statement,
                    keywords=_strings(item.get("keywords"), limit=8, item_limit=30),
                    evidence_node_ids=evidence_ids,
                    evidence_summary=evidence_summary,
                    confidence=confidence,
                    stability="tentative",
                )
            )

        share_raw = raw.get("share_candidate") if isinstance(raw.get("share_candidate"), dict) else {}
        share_candidate = ShareCandidate(
            should_share=_as_bool(share_raw.get("should_share")),
            reason=str(share_raw.get("reason") or "").strip()[:200],
            summary=str(share_raw.get("summary") or "").strip()[:300],
        )
        if not overall and not capabilities and not observations and action.type == "none" and comment_reply is None:
            return None
        if not overall:
            overall = "完成了一次自我检查，没有形成额外结论。"
        return SelfReflectionDecision(
            overall_reflection=overall,
            capabilities=capabilities,
            wishes=[],
            wish_action=action,
            comment_reply=comment_reply,
            self_observations=observations,
            share_candidate=share_candidate,
            validation_rejections=rejections,
        )

    def _evidence_node_ids(self, run_id: str) -> set[str]:
        result = set()
        for activity in self.store.list_activities(run_id):
            for node in self.store.list_nodes(activity["activity_id"]):
                if node["state"] == NodeState.COMPLETED.value:
                    result.add(node["node_id"])
        return result

    @staticmethod
    def _candidate_row(
        node_id: str,
        index: int,
        candidate: SelfObservationCandidate,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"{node_id}|{index}|{candidate.category}|{candidate.statement}".encode("utf-8")
        ).hexdigest()
        return {
            "candidate_id": digest[:32],
            "candidate_key": digest,
            **asdict(candidate),
        }

    def _record_failure(
        self,
        run: dict[str, Any],
        activity: dict[str, Any],
        node: WanderNode,
        runtime_block: str,
        status: str,
        error: str,
    ) -> None:
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node.node_id,
            phase=DecisionPhase.SELF_REFLECTION,
            model="",
            recipe="WANDER_ACTIVITY",
            input_context={"runtime": runtime_block},
            parsed_output={"raw": {}, "normalized": {}},
            status=status,
            error=error,
        )

    def _fail_node(self, node: WanderNode, error: str) -> None:
        node.execution_status = NodeExecutionStatus.FAILED.value
        node.execution_error = error
        node.source_summary = "自省生成失败"
        self.store.save_node(node)

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> Optional[int]:
        for key in keys:
            try:
                value = usage.get(key)
                return int(value) if value is not None else None
            except (AttributeError, TypeError, ValueError):
                continue
        return None

    def _required_node(self, node_id: str) -> WanderNode:
        data = self.store.get_node(node_id)
        if data is None:
            raise ValueError("node not found")
        data["state"] = NodeState(data["state"])
        return WanderNode(**data)

    def _required_activity(self, activity_id: str) -> dict[str, Any]:
        activity = self.store.get_activity(activity_id)
        if activity is None:
            raise ValueError("activity not found")
        return activity

    def _required_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("run not found")
        return run
