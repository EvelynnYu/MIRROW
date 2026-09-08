"""Safe one-node execution boundary for the new Wander runtime.

This adapter executes only handlers whose current API represents one bounded
piece of real work. It never advances the runtime state machine and it never
retries after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging
import re
import inspect
from typing import Any, Callable

from .event_types import EventType, WanderEvent
from .runtime_models import DecisionPhase, NodeState, WanderNode
from .runtime_store import WanderRuntimeStore

logger = logging.getLogger(__name__)


def _safe_handler_error(event_type: EventType, exc: BaseException) -> str:
    """Map handler exceptions to a short public error.

    Handler/provider exceptions may contain credentials, URLs, local paths or
    device details.  Taobao already owns a domain-level public mapper; use it
    when available, but deliberately collapse its configuration hint to the
    UI-safe wording (the environment variable name is not public evidence).
    Other domains expose only a stable generic message while the full
    traceback remains in the server logger.
    """
    if event_type == EventType.BROWSE_TAOBAO:
        try:
            from taobao_shopping_mcp.discovery_errors import public_error

            message = str(public_error(exc) or "").strip()
            if "淘宝原生桥接尚未配置" in message or "MIRROW_TAOBAO_CLI" in message:
                return "淘宝原生桥接尚未配置"
            # The domain mapper is intentionally short and provider-safe, but
            # keep a conservative length cap at this runtime boundary too.
            return message[:200] or "淘宝操作未完成"
        except Exception:
            return "淘宝操作未完成"
    return "RuntimeError:节点执行异常"


class NodeExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    WAITING_EXTERNAL = "waiting_external"


@dataclass
class NodeExecutionResult:
    status: NodeExecutionStatus
    completion_signal: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    side_effect_refs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retry_safe: bool = False
    audit_model: str = "legacy_handler"
    audit_input: dict[str, Any] = field(default_factory=dict)
    audit_raw_output: Any = ""
    audit_reasoning: Any = ""
    audit_usage: dict[str, Any] = field(default_factory=dict)


_SPECIALIZED_EVENTS = {
    EventType.SELF_REFLECTION,
}

# These are truthful execution gates, not successful browse outcomes.  The
# runtime records them as SKIPPED so a later node is never encouraged to
# retry/pretend that a phone interaction happened.  All XHS results still use
# ``retry_safe=False`` below because even a failed UI action may have reached
# the app.
_XHS_SKIP_STATUSES = frozenset({
    "adb_unavailable",
    "adb_unauthorized",
    "ambiguous_devices",
    "device_disconnected",
    "battery_low",
    "battery_unavailable",
    "consent_required",
    "login_required",
    "verification_required",
    "risk_detected",
    "popup_detected",
    "vision_unavailable",
    # These are truthful exhaustion states on the dedicated phone.  There is
    # no new material to read, but the bounded browse node itself completed
    # naturally; recording them as FAILED caused the planner to hammer the
    # same empty feed again.
    "no_feed_cards",
    "no_new_feed_cards",
    "no_public_cards",
})


def _song_identity_key(payload: dict[str, Any] | None) -> str:
    """Build the same normalized title/artist key used by the music handler."""
    if not isinstance(payload, dict):
        return ""
    title = str(payload.get("title") or payload.get("song_name") or "").strip()
    artist = str(payload.get("artist") or "").strip()
    if not title or not artist or artist.casefold() in {"未知", "unknown", "?"}:
        return ""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", f"{title}|{artist}".casefold())


class NodeExecutionAdapter:
    """Execute one already-claimed RUNNING node and persist its evidence."""

    def __init__(
        self,
        store: WanderRuntimeStore,
        handler_factory: Any,
        clock: Callable[[], datetime] = datetime.now,
        music_playback_enabled: Callable[[], bool] | None = None,
        get_idle_seconds: Callable[[], float] | None = None,
        get_setting: Callable[[str], Any] | None = None,
    ):
        self.store = store
        self.handler_factory = handler_factory
        self.clock = clock
        self.music_playback_enabled = music_playback_enabled
        self.get_idle_seconds = get_idle_seconds
        self.get_setting = get_setting

    async def execute(self, node_id: str, *, comment_draft: str = "") -> NodeExecutionResult:
        """Execute one claimed node.

        ``comment_draft`` is an XHS-only, explicit handoff option.  It is
        never inferred by the adapter and defaults to the historical
        read-only path, so autonomous/background callers remain unchanged.
        """
        node = self._required_node(node_id)
        if node.state != NodeState.RUNNING:
            raise ValueError("node must be running")
        if node.execution_status != NodeExecutionStatus.PENDING.value:
            raise ValueError("node execution was already claimed")

        activity = self._required_activity(node.activity_id)
        run = self._required_run(activity["run_id"])
        event_type = EventType(activity["activity_type"])

        if activity["interrupt_requested"]:
            return self._finish(
                run,
                activity,
                node,
                NodeExecutionResult(
                    NodeExecutionStatus.SKIPPED,
                    summary="节点开始前已收到中断",
                    error="interrupt_requested",
                ),
            )

        node.execution_status = NodeExecutionStatus.RUNNING.value
        self.store.save_node(node)

        if event_type == EventType.SLEEP:
            return self._finish(
                run,
                activity,
                node,
                NodeExecutionResult(
                    NodeExecutionStatus.SKIPPED,
                    summary="睡眠由真实计时器管理，不生成普通节点",
                    error="sleep_has_no_nodes",
                ),
            )
        if event_type in _SPECIALIZED_EVENTS:
            return self._finish(
                run,
                activity,
                node,
                NodeExecutionResult(
                    NodeExecutionStatus.SKIPPED,
                    summary="该事件需要专用执行器",
                    error="unsupported_specialized_adapter",
                ),
            )

        seen = self.store.list_nodes(activity["activity_id"])
        try:
            handler = self.handler_factory.get_handler(event_type)
            result = await self._execute_supported(
                event_type, handler, seen, activity_id=activity["activity_id"],
                node_id=node.node_id,
                comment_draft=comment_draft,
            )
        except Exception as exc:
            logger.exception("wander node handler raised: event_type=%s", event_type.value)
            public_error = _safe_handler_error(event_type, exc)
            result = NodeExecutionResult(
                NodeExecutionStatus.FAILED,
                summary=f"节点执行异常：{public_error}",
                error=public_error,
                audit_input={
                    "event_type": event_type.value,
                    "failure_stage": "unknown",
                    "public_error": public_error,
                },
                audit_raw_output={
                    "status": "failed",
                    "failure_stage": "unknown",
                    "public_error": public_error,
                },
            )

        refreshed = self._required_activity(node.activity_id)
        if refreshed["interrupt_requested"]:
            result.side_effect_refs["interrupted_after_execution"] = True
        return self._finish(run, refreshed, node, result)

    async def _execute_supported(
        self,
        event_type: EventType,
        handler: Any,
        seen_nodes: list[dict[str, Any]],
        *,
        activity_id: str,
        node_id: str = "",
        comment_draft: str = "",
    ) -> NodeExecutionResult:
        if event_type == EventType.KEYWORD_EXPANSION:
            excluded = {
                item["source_payload"].get("keyword")
                for item in seen_nodes
                if item["source_payload"].get("keyword")
            }
            payload = await handler.fetch_one_expansion(exclude_keywords=excluded)
            valid = bool(payload and payload.get("keyword") and payload.get("expansion"))
            return self._material_result(
                valid,
                payload,
                f"关键词「{payload.get('keyword')}」的联想" if valid else "关键词扩写未取得有效内容",
            )

        if event_type == EventType.MEMORY_FETCH:
            excluded = {
                item["source_payload"].get("memory_id")
                for item in seen_nodes
                if item["source_payload"].get("memory_id")
            }
            payload = await handler.fetch_one_memory(exclude_ids=excluded)
            valid = bool(payload and payload.get("memory_id"))
            return self._material_result(
                valid,
                payload,
                f"关于「{payload.get('topic', '未命名')}」的回忆" if valid else "未取得新记忆",
            )

        if event_type == EventType.BROWSE_NEWS:
            payload = await handler.fetch_one_news()
            if payload and payload.get("status") == "no_search_tool":
                return NodeExecutionResult(
                    NodeExecutionStatus.SKIPPED,
                    summary="没有可用的新闻搜索工具",
                    payload=payload,
                    error="no_search_tool",
                )
            valid = bool(
                payload
                and payload.get("status") == "success"
                and payload.get("query")
                and payload.get("content")
            )
            return self._material_result(
                valid,
                payload,
                f"阅读「{payload.get('query')}」的搜索结果" if valid else "新闻搜索未取得有效内容",
            )

        if event_type == EventType.BROWSE_XIAOHONGSHU:
            activity = self._required_activity(activity_id)
            excluded = {
                item["source_payload"].get("source_id")
                for item in seen_nodes
                if item["source_payload"].get("source_id")
            }
            fetch_kwargs = {
                "exclude_ids": excluded,
                "activity_reason": str(activity.get("reason") or ""),
            }
            # Do not add the optional keyword for legacy/test handlers unless
            # the chat caller explicitly supplied a draft.
            if str(comment_draft or "").strip():
                fetch_kwargs["comment_draft"] = str(comment_draft)
            payload = await handler.fetch_one_post(**fetch_kwargs)
            status = str(payload.get("status") or "") if payload else ""
            audit = payload.pop("agent_audit", {}) if isinstance(payload, dict) else {}
            calls = (audit.get("calls") or []) if isinstance(audit, dict) else []
            if status in _XHS_SKIP_STATUSES or status in {"no_dots_key", "browser_unavailable"}:
                execution_mode = str((payload or {}).get("execution_mode") or "dedicated_device_direct")
                return NodeExecutionResult(
                    NodeExecutionStatus.SKIPPED,
                    summary=f"小红书真机节点跳过：{status or '能力不可用'}",
                    payload=payload or {}, error=status, audit_model=(calls[-1].get("model") if calls else "adb"),
                    audit_input={"activity_reason": activity.get("reason") or "",
                                 "event_type": event_type.value,
                                 "execution_mode": execution_mode},
                    audit_raw_output=calls or [],
                )
            source_id = str(payload.get("source_id") or "") if payload else ""
            if status == "success" and source_id and source_id in excluded:
                # The handler is expected to honor the exclusion set.  Keep
                # the runtime honest if a legacy/test source ignores it:
                # duplicate material must never count as a second post.
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary="小红书浏览命中了已读帖子",
                    payload=payload or {},
                    error="duplicate_source_id",
                    retry_safe=False,
                    audit_model=(calls[-1].get("model") if calls else "adb"),
                    audit_input={"activity_reason": activity.get("reason") or "",
                                 "event_type": event_type.value},
                    audit_raw_output=calls,
                )
            valid = bool(payload and status == "success" and payload.get("source_id")
                         and payload.get("content_summary"))
            title = payload.get("title") if payload else ""
            return NodeExecutionResult(
                NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED,
                completion_signal="node_material_ready" if valid else "",
                summary=(f"浏览小红书内容「{title or payload.get('query', '未知')}」"
                         if valid else "小红书浏览未取得可靠内容"),
                payload=payload or {}, error="" if valid else status or "missing_required_material",
                retry_safe=False,
                audit_model=(calls[-1].get("model") if calls else "dots"),
                audit_input={"activity_reason": activity.get("reason") or "",
                             "event_type": event_type.value,
                             "execution_mode": str(payload.get("execution_mode") or "dedicated_device_direct")},
                audit_raw_output=calls,
                audit_reasoning=[item.get("reasoning", "") for item in calls],
                audit_usage={"calls": [item.get("usage", {}) for item in calls]},
            )

        if event_type == EventType.VISIT_LOUNGE:
            payload = await handler.visit_once()
            valid = payload.get('status') == 'success'
            return NodeExecutionResult(
                NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.SKIPPED
                if payload.get('status') == 'unavailable' else NodeExecutionStatus.FAILED,
                completion_signal='lounge_visit_complete' if valid else '',
                summary=payload.get('message', ''), payload=payload,
                side_effect_refs={'visit_id': payload.get('visit_id', ''), 'notification_id': payload.get('notification_id', '')},
                error='' if valid else payload.get('status', 'failed'))

        if event_type == EventType.BROWSE_TAOBAO:
            payload = await handler.visit_once()
            payload = payload if isinstance(payload, dict) else {
                "status": "failed",
                "failure_stage": "autonomous_roam",
                "public_error": "淘宝闲逛返回格式无效",
                "message": "淘宝闲逛未返回可靠结果。",
            }
            status = str(payload.get("status") or "failed")
            valid = status == "success"
            # Availability/lock exhaustion is a truthful natural skip.  A
            # staged failure (bridge/search/context/trip read) must remain a
            # real FAILED node so the existing 15-minute execution cooldown
            # and backoff can suppress immediate repeats.
            node_status = (
                NodeExecutionStatus.SUCCEEDED
                if valid else
                NodeExecutionStatus.SKIPPED
                if status in {"unavailable", "busy"}
                else NodeExecutionStatus.FAILED
            )
            failure_stage = str(payload.get("failure_stage") or "")[:80]
            public_error = str(payload.get("public_error") or "")[:300]
            return NodeExecutionResult(
                node_status,
                completion_signal='taobao_outing_complete' if valid else '',
                summary=str(payload.get('message') or '')[:1200], payload=payload,
                side_effect_refs={'trip_id':payload.get('trip_id', ''), 'notification_id':payload.get('notification_id', '')},
                error='' if valid else public_error or status,
                audit_model='wander_flash',
                audit_input={
                    "event_type": event_type.value,
                    "failure_stage": failure_stage,
                    "public_error": public_error,
                },
                audit_raw_output={
                    "status": status,
                    "failure_stage": failure_stage,
                    "public_error": public_error,
                    "warnings": payload.get("warnings") or [],
                },
            )

        if event_type == EventType.BROWSE_SOCIAL_FEED:
            activity = self._required_activity(activity_id)
            payload = await handler.visit_once(
                activity_reason=str(activity.get("reason") or ""),
                run_id=str(activity.get("run_id") or ""),
                activity_id=activity_id,
                node_id=node_id,
            )
            status = str(payload.get("status") or "") if payload else ""
            valid = status == "success"
            action = str(payload.get("action") or "none") if payload else "none"
            return NodeExecutionResult(
                NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED,
                completion_signal="social_feed_visit_complete" if valid else "",
                summary=(
                    f"查看了 {payload.get('selected_date')} 的朋友圈，选择 {action}"
                    if valid else "朋友圈浏览没有完成可靠决策"
                ),
                payload=payload or {},
                side_effect_refs={
                    "moment_id": payload.get("moment_id", "") if payload else "",
                    "comment_id": payload.get("comment_id", "") if payload else "",
                },
                error="" if valid else status or "social_feed_visit_failed",
                retry_safe=False,
                audit_model="wander_flash",
                audit_input={
                    "event_type": event_type.value,
                    "activity_reason": activity.get("reason") or "",
                },
                audit_raw_output={"status": status, "action": action},
            )

        if event_type == EventType.BROWSE_BOOKMARKS:
            excluded = {
                item["source_payload"].get("original_msg_id")
                for item in seen_nodes
                if item["source_payload"].get("original_msg_id")
            }
            activity = self._required_activity(activity_id)
            fetch = handler.fetch_one_bookmark
            fetch_kwargs = {"exclude_ids": excluded}
            # v3 handlers receive the complete identity chain so bookmark
            # actions can derive a stable idempotency key.  Tiny legacy/test
            # handlers keep their old one-argument API.
            try:
                parameters = inspect.signature(fetch).parameters
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                for name, value in (
                    ("run_id", activity.get("run_id", "")),
                    ("activity_id", activity_id),
                    ("node_id", node_id),
                ):
                    if accepts_kwargs or name in parameters:
                        fetch_kwargs[name] = value
            except (TypeError, ValueError):
                pass
            payload = await fetch(**fetch_kwargs)
            valid = bool(
                payload
                and payload.get("original_msg_id")
                and (payload.get("content") or payload.get("displayed_message_count"))
            )
            owner = payload.get("owner", "未知") if payload else "未知"
            action = payload.get("bookmark_action") if isinstance(payload, dict) else None
            action_status = str(action.get("status") or "") if isinstance(action, dict) else ""
            if valid and action_status == "failed":
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary="收藏夹材料已读取，但收藏动作失败",
                    payload=payload,
                    error=str(action.get("reason") or "bookmark_action_failed")[:200],
                    retry_safe=False,
                )
            return self._material_result(
                valid,
                payload,
                (
                    f"回看{payload.get('active_date')}的历史对话"
                    if valid and payload.get("source_mode") in {"unbookmarked_anchor", "random_day"}
                    else f"翻看{owner}收藏夹中的一条收藏"
                    if valid else "未取得新收藏"
                ),
            )

        if event_type == EventType.LISTEN_MUSIC:
            excluded = {
                item["source_payload"].get("fingerprint")
                for item in seen_nodes
                if item["source_payload"].get("fingerprint")
            }
            excluded |= {
                _song_identity_key(item.get("source_payload"))
                for item in seen_nodes
                if _song_identity_key(item.get("source_payload"))
            }
            # 跨活动去重：近期所有已听过的歌也排除，防止整夜反复听同一首。
            try:
                excluded |= self.store.list_recent_listen_music_fingerprints(days=3)
                excluded |= self.store.list_recent_listen_music_identities(days=3)
            except Exception:
                pass
            payload = await handler.pick_and_fetch_song(exclude_fingerprints=excluded)
            picker_audit = {}
            try:
                picker_audit = handler.get_last_selection_audit()
            except Exception:
                picker_audit = {}
            if payload and (
                payload.get("fingerprint") in excluded
                or _song_identity_key(payload) in excluded
            ):
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary="选定歌曲命中本轮或近期去重记录",
                    payload={**payload, "selection_audit": picker_audit} if picker_audit else payload,
                    error="duplicate_recent_song",
                    retry_safe=False,
                )
            valid = bool(
                payload
                and payload.get("fingerprint")
                and payload.get("title")
                and payload.get("artist")
                and str(payload.get("artist")).casefold() not in {"未知", "unknown", "?"}
                and payload.get("netease_song_id")
            )
            if not valid:
                audit_error = str(picker_audit.get("error") or "").strip()
                audit_payload = picker_audit.get("payload") if isinstance(picker_audit, dict) else {}
                failed_payload = dict(payload or {})
                if picker_audit:
                    failed_payload["selection_audit"] = picker_audit
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary=(f"听歌材料失败：{audit_error}" if audit_error else "未取得可真实播放的歌曲材料"),
                    payload=failed_payload,
                    # Keep the historical fallback only for a legacy handler
                    # that exposes no picker audit at all; real ListenMusic
                    # failures now carry their specific audit error above.
                    error=audit_error or "missing_reliable_song_material",
                    retry_safe=False,
                )
            payload.setdefault("selected_by", "K_autonomous")
            payload.setdefault("selection_source", "wander_llm")
            playback_requested = self._music_playback_is_enabled()
            playback_target = self._music_playback_target() if playback_requested else "none"
            payload["playback_requested"] = playback_requested
            payload["playback_target"] = playback_target
            payload["analysis_source"] = (
                "cached_melody_lyrics" if payload.get("from_cache") else "netease_melody_lyrics"
            )
            # Analysis is useful even when the optional physical playback
            # ritual is disabled. This path never touches a player.
            if not playback_requested:
                payload["played"] = False
                return NodeExecutionResult(
                    NodeExecutionStatus.SUCCEEDED,
                    completion_signal="analysis_complete",
                    summary=f"完成《{payload.get('title', '')}》的旋律与歌词分析（未实际外放）",
                    payload=payload,
                    retry_safe=False,
                )
            if playback_target not in {"computer", "mobile"}:
                payload["played"] = False
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary=f"《{payload.get('title', '')}》外放目标设置无效",
                    payload=payload,
                    error="invalid_playback_target",
                    retry_safe=False,
                )
            try:
                duration_sec = float(payload.get("duration_sec") or 0) if payload else 0.0
            except (TypeError, ValueError):
                duration_sec = 0.0
            title = payload.get("title", "")
            artist = f" - {payload.get('artist')}" if payload.get("artist") else ""
            try:
                playback = handler.start_runtime_playback
                kwargs: dict[str, Any] = {}
                try:
                    parameters = inspect.signature(playback).parameters
                    accepts_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                    if accepts_kwargs or "target" in parameters:
                        kwargs["target"] = playback_target
                    if accepts_kwargs or "song_data" in parameters:
                        kwargs["song_data"] = payload
                except (TypeError, ValueError):
                    kwargs = {}
                started = await playback(payload["netease_song_id"], **kwargs)
            except Exception:
                started = False
            if not started:
                payload["played"] = False
                playback_state = getattr(handler, "_last_runtime_playback", None)
                playback_error = (
                    str(playback_state.get("error") or "").strip()
                    if isinstance(playback_state, dict) else ""
                ) or "playback_start_failed"
                payload["playback_error"] = playback_error[:200]
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary=(
                        f"《{title}》手机外放失败：{playback_error}"
                        if playback_target == "mobile"
                        else f"《{title}》真实播放启动失败"
                    ),
                    payload=payload,
                    error=playback_error,
                    retry_safe=False,
                )
            if duration_sec <= 0:
                # Cached metadata historically did not persist duration. The
                # real playback boundary returns the authoritative track
                # detail, so use it when available instead of guessing a
                # timer; otherwise close the node explicitly.
                playback_result = getattr(handler, "_last_runtime_playback", None)
                playback_info = playback_result.get("playback", {}) if isinstance(playback_result, dict) else {}
                try:
                    duration_sec = float(
                        playback_info.get("duration_sec")
                        or playback_info.get("duration")
                        or playback_info.get("durationMs", 0)
                        or 0
                    )
                    if duration_sec > 10000:
                        duration_sec /= 1000.0
                except (TypeError, ValueError):
                    duration_sec = 0.0
                if duration_sec <= 0:
                    return NodeExecutionResult(
                        NodeExecutionStatus.FAILED,
                        summary=f"《{title}》已启动但未取得可靠播放时长",
                        payload=payload,
                        error="playback_duration_unavailable",
                        retry_safe=False,
                    )
            payload["played"] = True
            expected_completion_at = (self.clock() + timedelta(seconds=duration_sec)).isoformat()
            return NodeExecutionResult(
                NodeExecutionStatus.WAITING_EXTERNAL,
                completion_signal="track_prepared",
                summary=f"已选定并确认开始播放《{title}》{artist}，等待播放完成",
                payload=payload,
                side_effect_refs={
                    "playback_started": True,
                    "expected_completion_at": expected_completion_at,
                    "netease_song_id": str(payload["netease_song_id"]),
                },
                retry_safe=False,
            )

        if event_type in {EventType.USER_TRACKING, EventType.HOST_GROUP_ACTIVITY}:
            # These handlers represent one bounded real-world action. Recovery
            # never replays this call. Host group handlers are optional and must
            # return explicit completion evidence; no room or transport exists
            # in this distribution.
            idle_seconds = 0.0
            if self.get_idle_seconds is not None:
                try:
                    idle_seconds = max(0.0, float(self.get_idle_seconds()))
                except Exception:
                    idle_seconds = 0.0
            event = WanderEvent(
                event_type=event_type,
                event_id=activity_id,
                details={
                    "idle_seconds": idle_seconds,
                    "_runtime_observation_only": event_type == EventType.USER_TRACKING,
                },
            )
            handled = await handler.handle(event)
            payload = dict(handled.details or {})
            if payload.get("error"):
                return NodeExecutionResult(
                    NodeExecutionStatus.FAILED,
                    summary=handled.process_log or f"{event_type.value} 执行失败",
                    payload=payload,
                    error=str(payload.get("error"))[:200],
                )
            if event_type == EventType.USER_TRACKING:
                valid = bool(payload.get("tracking_result"))
                return NodeExecutionResult(
                    NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED,
                    completion_signal="observation_recorded" if valid else "",
                    summary=handled.process_log or "完成一次用户状态感知",
                    payload=payload,
                    side_effect_refs={
                        "runtime_activity_id": activity_id,
                        "gaming_rejudge_scheduled": bool(payload.get("gaming_rejudge_scheduled")),
                    },
                    error="" if valid else "missing_tracking_result",
                )

            completed = payload.get("host_activity_completed") is True
            return NodeExecutionResult(
                NodeExecutionStatus.SUCCEEDED if completed else NodeExecutionStatus.FAILED,
                completion_signal="host_activity_completed" if completed else "",
                summary=handled.process_log or ("宿主群组活动已完成" if completed else "宿主群组活动未提供完成证据"),
                payload=payload,
                side_effect_refs={"runtime_activity_id": activity_id},
                error="" if completed else "missing_host_activity_completion",
                retry_safe=False,
            )

        return NodeExecutionResult(
            NodeExecutionStatus.SKIPPED,
            summary="当前没有该事件的安全单节点执行器",
            error="unsupported_event",
        )

    def _music_playback_is_enabled(self) -> bool:
        """Read the live ritual setting without caching it between nodes."""
        if self.music_playback_enabled is not None:
            try:
                return bool(self.music_playback_enabled())
            except Exception:
                return False
        return bool(self._read_setting("listen_music_playback", False))

    def _music_playback_target(self) -> str:
        value = self._read_setting("listen_music_playback_device", "computer")
        return str(value or "computer").strip().lower()

    def _read_setting(self, key: str, default: Any = None) -> Any:
        if self.get_setting is not None:
            try:
                value = self.get_setting(key)
                return default if value is None else value
            except Exception:
                return default
        try:
            from mirrow_core.settings_manager import get_setting
            value = get_setting(key)
            return default if value is None else value
        except Exception:
            return default

    async def confirm_external_completion(
        self,
        node_id: str,
        signal: str,
    ) -> NodeExecutionResult:
        if signal not in {"playback_finished", "duration_elapsed"}:
            raise ValueError("unsupported completion signal")
        node = self._required_node(node_id)
        activity = self._required_activity(node.activity_id)
        if EventType(activity["activity_type"]) != EventType.LISTEN_MUSIC:
            raise ValueError("external completion is only supported for music nodes")
        if node.state != NodeState.RUNNING:
            raise ValueError("node must still be running")
        if node.execution_status != NodeExecutionStatus.WAITING_EXTERNAL.value:
            raise ValueError("node is not waiting for external completion")

        run = self._required_run(activity["run_id"])
        title = node.source_payload.get("title", "未命名")
        return self._finish(
            run,
            activity,
            node,
            NodeExecutionResult(
                NodeExecutionStatus.SUCCEEDED,
                completion_signal=signal,
                summary=f"《{title}》播放已完成",
                payload=node.source_payload,
                side_effect_refs=node.side_effect_refs,
                retry_safe=False,
            ),
        )

    @staticmethod
    def _material_result(
        valid: bool,
        payload: Any,
        summary: str,
    ) -> NodeExecutionResult:
        return NodeExecutionResult(
            NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED,
            completion_signal="node_material_ready" if valid else "",
            summary=summary,
            payload=payload if isinstance(payload, dict) else {},
            error="" if valid else "missing_required_material",
            retry_safe=False,
        )

    def _finish(
        self,
        run: dict[str, Any],
        activity: dict[str, Any],
        node: WanderNode,
        result: NodeExecutionResult,
    ) -> NodeExecutionResult:
        node.execution_status = result.status.value
        node.completion_signal = result.completion_signal
        node.source_summary = result.summary
        node.source_payload = result.payload
        node.side_effect_refs = result.side_effect_refs
        node.execution_error = result.error
        node.retry_safe = result.retry_safe
        self.store.save_node(node)
        self.store.record_decision(
            run_id=run["run_id"],
            activity_id=activity["activity_id"],
            node_id=node.node_id,
            phase=DecisionPhase.NODE_EXECUTION,
            model=result.audit_model,
            recipe="node_execution_adapter",
            input_context=result.audit_input or {
                "event_type": activity["activity_type"],
                "audit_gap": "legacy handler internals do not expose raw LLM metadata",
            },
            raw_output=result.audit_raw_output,
            reasoning=result.audit_reasoning,
            parsed_output={
                "status": result.status.value,
                "completion_signal": result.completion_signal,
                "summary": result.summary,
                "payload": result.payload,
                "side_effect_refs": result.side_effect_refs,
                "error": result.error,
                "failure_stage": (
                    result.payload.get("failure_stage", "")
                    if isinstance(result.payload, dict) else ""
                ),
                "public_error": (
                    result.payload.get("public_error", "")
                    if isinstance(result.payload, dict) else ""
                ),
            },
            status=result.status.value,
            error=result.error,
            prompt_tokens=self._usage_total(result.audit_usage, "prompt_tokens", "input_tokens"),
            completion_tokens=self._usage_total(result.audit_usage, "completion_tokens", "output_tokens"),
            cache_tokens=self._usage_total(result.audit_usage, "cached_tokens", "cache_read_input_tokens"),
        )
        return result

    @staticmethod
    def _usage_total(usage: dict[str, Any], *names: str) -> int:
        groups = usage.get("calls") if isinstance(usage, dict) else None
        groups = groups if isinstance(groups, list) else [usage]
        total = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            for name in names:
                if name in group:
                    try:
                        total += int(group[name] or 0)
                    except (TypeError, ValueError):
                        pass
                    break
        return total

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
