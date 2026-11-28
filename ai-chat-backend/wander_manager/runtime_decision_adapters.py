"""Auditable node-review and settlement decisions for the Wander runtime.

These adapters interpret persisted facts. They never advance the controller,
send a message, or update the emotion system.
"""

from __future__ import annotations

import json
from affect.feeling_contract import normalize_wander_feelings
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .event_catalog import strategy_for, render_event_options
from .event_types import EventType
from .flash_structured import FlashJsonResult, call_flash_json_detailed
from .runtime_models import ActivityState, DecisionPhase, NodeState, SettlementReason
from .runtime_store import WanderRuntimeStore, redact_sensitive
from .timing_policy import normalize_delay_seconds
from .goal_policy import normalize_extension, get_activity_goal_policy, is_autonomous_run
from .context_projection import activity_history_scope, execution_receipt
from .music_receipt import MUSIC_EXPRESSION_CONTRACT
from .activity_purpose import normalize_outcome, current_progress, recall_query


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
    today_wander: str = ""


@dataclass
class EmotionEffect:
    operation: str = "add"
    previous_feeling: str = ""
    changed: bool = False
    from_emotion: str = ""
    to_emotion: str = ""
    delta: str = ""
    confidence: float = 0.0
    family: str = "复杂/未归类"
    intensity: float = 0.6
    persistence: str = "situational"
    target: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            'changed': self.changed, 'from': self.from_emotion, 'to': self.to_emotion,
            'operation': self.operation, 'previous_feeling': self.previous_feeling,
            'delta': self.delta, 'confidence': self.confidence, 'family': self.family,
            'intensity': self.intensity, 'persistence': self.persistence, 'target': self.target,
        }


@dataclass
class NodeReviewDecision:
    reflection: str = ""
    purpose_outcome: Optional[dict[str, str]] = None
    collect_music: bool = False
    music_library_action: Optional[dict[str, Any]] = None
    keep_post: bool = False
    keep_post_reason: str = ""
    emotion_effect: EmotionEffect = field(default_factory=EmotionEffect)
    continue_activity: bool = True
    abort_reason: str = ""
    switch_to: Optional[tuple[EventType, str]] = None
    next_node_delay_seconds: Optional[float] = None
    timing_reason: str = ""
    timing_source: str = "fallback"
    next_node_at: str = ""
    extend_count: int = 0
    goal_reason: str = ""
    switch_goal: Optional[dict[str, Any]] = None
    goal_assessment: Optional[dict[str, Any]] = None
    closeout: Optional[SettlementDecision] = None


@dataclass
class SettlementDecision:
    summary: str = ""
    purpose_outcome: Optional[dict[str, str]] = None
    retained_impression: str = ""
    retained_node_ids: list[str] = field(default_factory=list)
    share: bool = False
    continue_next: bool = False
    next_inclination_note: str = ""
    emotion_effect: EmotionEffect = field(default_factory=EmotionEffect)
    next_run_delay_seconds: Optional[float] = None
    timing_reason: str = ""
    timing_source: str = "fallback"
    next_plan_at: str = ""
    xhs_share_node_id: str = ""
    essay_share_node_id: str = ""
    reading_picture_node_id: str = ""
    xhs_comment_draft: str = ""

@dataclass
class HorizonReviewDecision:
    continue_activity: bool = False
    extend_minutes: int = 0
    next_node_delay_seconds: float = 900.0
    reason: str = ""
    new_horizon: str = ""
    next_node_at: str = ""
    closeout: Optional[SettlementDecision] = None


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
    normalized = normalize_wander_feelings(raw)
    return EmotionEffect(changed=True, **normalized) if normalized else EmotionEffect()


def _normalize_music_library_action(raw: Any) -> Optional[dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip()
    if action not in {
        "create_playlist", "rename_playlist", "delete_playlist",
        "add_current_song", "remove_song", "set_default_playlist",
    }:
        return None
    result = {"action": action}
    playlist_id = str(raw.get("playlist_id") or "").strip()
    song_id = str(raw.get("song_id") or "").strip()
    name = str(raw.get("name") or "").strip()[:100]
    if action != "create_playlist":
        if not playlist_id.isdigit() or len(playlist_id) > 20:
            return None
        result["playlist_id"] = playlist_id
    if action in {"create_playlist", "rename_playlist"}:
        if not name:
            return None
        result["name"] = name
    if action == "remove_song":
        if not song_id.isdigit() or len(song_id) > 20:
            return None
        result["song_id"] = song_id
    reason = str(raw.get("reason") or "").strip()[:300]
    if reason:
        result["reason"] = reason
    return result


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
        checkup = (node.get('source_payload') or {}).get('checkup_version') == 1
        started_music = bool(((node.get('source_payload') or {}).get('listening_experience') or {}).get('started_at'))
        if node.get("state") != NodeState.COMPLETED.value and not (node.get('state') == 'aborted' and (checkup or started_music)):
            continue
        from .checkup_experience import material as checkup_material, project as project_checkup
        observed = checkup_material(node)
        if observed:
            lines.append(project_checkup(observed))
            continue
        reflection = node.get("reflection") or ""
        feeling_text = f"AI 当时原述感想：{reflection}" if reflection else "未取得节点感想"
        library_receipt = (node.get("side_effect_refs") or {}).get("music_library_action")
        payload = node.get("source_payload") or {}
        music_node = isinstance(payload, dict) and bool(payload.get("netease_song_id"))
        if music_node:
            from .music_receipt import music_execution
            feeling_text = ("音乐执行事实：" + json.dumps(music_execution(node), ensure_ascii=False)
                            + "；" + feeling_text)
        receipt_text = (
            f"；歌单行动回执：{_safe_excerpt(library_receipt, 500)}"
            if isinstance(library_receipt, dict) else "；歌单行动回执：无，本节点未确认歌单变更" if music_node else ""
        )
        xhs_receipt = (node.get("side_effect_refs") or {}).get("xhs_post_action")
        if isinstance(xhs_receipt, dict):
            receipt_text += f"；MIRROW 本地留帖回执：{_safe_excerpt(xhs_receipt, 300)}"
        comment_receipt = (node.get('side_effect_refs') or {}).get('xhs_comment_result')
        if isinstance(comment_receipt, dict):
            from xhs_interactions.context import brief
            receipt_text += '；公开评论回执：' + brief(comment_receipt.get('context') or comment_receipt)
        lines.append(
            f"- 第{node['round_index']}节点（node_id={node.get('node_id') or ''}）："
            f"执行摘要：{node.get('source_summary') or '无摘要'}；"
            f"执行状态：{node.get('execution_status') or 'unknown'}；"
            f"收尾原因：{node.get('abort_reason') or '无'}；{feeling_text}{receipt_text}"
        )
    return "\n".join(lines) or "尚无已完成节点"


def _accumulated_emotion(nodes: list[dict[str, Any]]) -> str:
    for node in reversed(nodes):
        effect = node.get("emotion_effect") or {}
        if (isinstance(effect, dict) and effect.get("changed")
                and effect.get("to") and "、" not in str(effect["to"])
                and effect.get("delta")):
            return _safe_excerpt({"previous_node": node.get("round_index"),
                                  "feeling_then": effect.get("to"),
                                  "change_then": effect.get("delta") or ""}, 500)
    return "暂无已确认的节点心情变化"


def _prior_game_progress(history: list[dict[str, Any]], nodes: list[dict[str, Any]],
                         activity_id: str) -> str:
    """Give settlement the earlier activities of this save, not only this activity's nodes."""
    current = next((n.get("source_payload") for n in reversed(nodes)
                    if isinstance(n.get("source_payload"), dict)
                    and n["source_payload"].get("playthrough_key")), None)
    if not current:
        return "当前游戏存档身份尚未确认"
    key = current["playthrough_key"]
    from .game_experience import chapter_facts, compact_chapter_facts

    earlier_rows = [row for row in history if row.get("activity_id") != activity_id
                    and isinstance(row.get("source_payload"), dict)
                    and row["source_payload"].get("playthrough_key") == key]
    earlier: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    for row in history:
        payload = row.get("source_payload") or {}
        if not isinstance(payload, dict):
            continue
        row_key = payload.get("playthrough_key")
        if row_key == key and row.get("activity_id") != activity_id:
            group = earlier.setdefault(str(row.get("activity_id") or ""), {
                "at": row.get("activity_created_at"), "node_reflections": [], "steps": [],
            })
            reflection = str(row.get("reflection") or "").strip()
            if reflection and not any(item["text"] == reflection for item in group["node_reflections"]):
                group["node_reflections"].append({
                    "node_id": row.get("node_id"), "text": reflection[:240],
                })
                group["node_reflections"] = group["node_reflections"][-4:]
            if payload.get("game") != "detroit":
                group["steps"].append({"at": row.get("completed_at"),
                                       "fact": str(row.get("source_summary") or "")[:240],
                                       "feeling": str(row.get("reflection") or "")[:240]})
        elif row_key and row_key != key and payload.get("game_complete"):
            completed[str(row_key)] = {
                "playthrough": row_key, "summary": row.get("activity_summary") or "",
                "at": row.get("completed_at"),
            }
    return _safe_excerpt({"current_playthrough": key,
                          "confirmed_chapters_this_playthrough": compact_chapter_facts(chapter_facts(earlier_rows)),
                          "earlier_activities_this_playthrough": list(earlier.values()),
                          "completed_other_playthroughs": list(completed.values())}, 18000)


def _game_execution_facts(nodes: list[dict[str, Any]]) -> str:
    """Expose confirmed game actions independently of generated reasons/reflections."""
    facts = [execution_receipt(node) for node in nodes if node.get("execution_status") == "succeeded"]
    return json.dumps(redact_sensitive(facts), ensure_ascii=False)


def _runtime_block(
    context: DecisionContext,
    activity: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    phase: str,
    current_node: Optional[dict[str, Any]] = None,
    settlement_reason: str = "",
    run: Optional[dict[str, Any]] = None,
    purpose_progress: Optional[dict[str, Any]] = None,
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
        f"[人类伙伴状态]\n{context.user_status or '未知'}",
        *([f"[状态持续时间]\n{context.status_duration}"] if context.status_duration else []),
        *([f"[距最后私聊]\n{context.private_message_idle}"] if context.private_message_idle else []),
        *([f"[设备本地物理空闲]\n{context.physical_idle}"] if context.physical_idle else []),
        *([f"[离开时长]\n{context.away_duration}"] if not any((context.status_duration, context.private_message_idle, context.physical_idle)) and context.away_duration else []),
        f"[今日漫想]\n{context.today_wander or '暂无'}",
        f"[当前活动合同]\n{contract}",
        f"[本次活动起意]\n{activity.get('reason') or '未记录'}",
        f"[活动时间事实]\ncreated_at={activity.get('created_at') or '未知'}；next_node_at={activity.get('next_node_at') or '无'}",
        f"[本段此前已完成的节点]\n{_completed_progress(nodes)}",
    ]
    if purpose_progress:
        parts.append('[本次活动最近复核的主观进展与续作起意]\n'
                     + json.dumps(purpose_progress, ensure_ascii=False))
    if run:
        timing = (run.get("context_snapshot") or {}).get("timing") or {}
        parts.append(f"[计划时间事实]\nhorizon_mode={timing.get('horizon_mode', 'deadline')}；horizon_at={run.get('next_wake_at') or '无'}")
        policy = get_activity_goal_policy(run, activity)
        parts.append(f"[目标事实]\nautonomy_version={1 if is_autonomous_run(run) else 0}；completion={policy.get('completion', 'fixed')}；initial_goal_value={policy.get('initial_goal_value', activity.get('goal_value'))}；extensions_total={policy.get('extensions_total', 0)}")
    parts.append(f"[累积情绪候选]\n{_accumulated_emotion(nodes)}")
    if event_type == EventType.BROWSE_BOOKMARKS:
        from .reading_illustrations import choices
        pictures = choices(nodes)
        if pictures:
            parts.append('[已画好并夹在书里的插画]\n' + pictures)
        failures = {'unmatched': '原句没能定位，未出图', 'failed': '未取得成图', 'interrupted': '配图中断', 'requested': '配图尚无完成回执'}
        for row in nodes:
            status = (row.get('side_effect_refs') or {}).get('reading_illustration', {}).get('status')
            if status in failures:
                parts.append('本次翻阅配图：' + failures[status])
        selections = [{
            'node_id': node['node_id'], 'execution_status': node.get('execution_status'),
            'selection': node['source_payload']['material_selection'],
            'reading_received': bool(node['source_payload'].get('reading_received')),
            'result': node.get('source_summary') or '',
        } for node in nodes if (node.get('source_payload') or {}).get('material_selection')]
        if selections:
            parts.append('[本次翻阅的选材与读取回执；可用索引不代表已经阅读]\n'
                         + json.dumps(selections, ensure_ascii=False))
    if event_type == EventType.BROWSE_XIAOHONGSHU and run:
        from .xhs_request import latest_cursor, progress_for_ai
        record = (run.get('context_snapshot') or {}).get('phone_tasks', {}).get(activity.get('activity_id'), {})
        progress = progress_for_ai(activity.get('activity_id'), record.get('spec'),
                                  latest_cursor(nodes, record.get('initial_cursor')))
        if progress:
            if any((node.get('source_payload') or {}).get('status') == 'profile_end_observed' for node in nodes):
                progress['listing_coverage'] = 'explicit_end'
            parts.append('[手机阅读任务事实]\n' + json.dumps(progress, ensure_ascii=False))
    if current_node is not None:
        payload = current_node.get("source_payload") or {}
        if event_type == EventType.LISTEN_MUSIC and isinstance(payload, dict):
            from .music_receipt import music_execution
            parts.append("[当前音乐执行事实]\n" + json.dumps(music_execution(current_node), ensure_ascii=False))
            from music_system.selection_catalog import memberships
            known_memberships = memberships(payload)[:8]
            editable_memberships = [item for item in known_memberships
                                    if item["subject"] in {"k", "shared"}]
            k_memberships = [item for item in editable_memberships if item["subject"] == "k"]
            origin = str(payload.get("selection_origin") or "") or (
                "cache_candidate" if payload.get("from_cache") is True
                else "search_result" if payload.get("from_cache") is False else "unknown"
            )
            parts.append("[当前曲目选取与歌单成员事实]\n" + json.dumps({
                "song_id": str(payload.get("netease_song_id") or ""),
                "title": str(payload.get("title") or "")[:120],
                "selection_origin": origin,
                "source_playlist_id": str(payload.get("source_playlist_id") or ""),
                "known_playlist_memberships": known_memberships,
                "editable_playlist_ids": [item["id"] for item in editable_memberships],
                "k_writable_playlist_ids": [item["id"] for item in k_memberships],
            }, ensure_ascii=False))
        current_source = (
            "[当前节点真实材料]\n"
            f"node_id={current_node.get('node_id')}\n"
            f"当前节点序号={current_node.get('round_index')}（本次复核中的真实节点）\n"
        )
        if event_type == EventType.BROWSE_XIAOHONGSHU and payload.get('material_version') == 1:
            from .xhs_request import material_for_ai
            current_source += json.dumps(material_for_ai(payload), ensure_ascii=False)
            parts.append(current_source)
            return "\n".join(parts)
        if event_type in {EventType.PUBLISH_XIAOHONGSHU, EventType.XHS_DIRECT_MESSAGES, EventType.CHECK_XIAOHONGSHU_INTERACTIONS}:
            current_source += json.dumps(payload, ensure_ascii=False)
            parts.append(current_source)
            return "\n".join(parts)
        if event_type == EventType.USER_TRACKING and payload.get('checkup_version') == 1:
            from .checkup_experience import material as checkup_material, project as project_checkup
            current_source += project_checkup(checkup_material(current_node))
            parts.append(current_source)
            return "\n".join(parts)
        if event_type == EventType.MEMORY_FETCH and payload.get('memory_fetch_version') == 1 and payload.get('outcome') != 'found':
            from .memory_fetch_result import description
            parts.append(current_source + description(payload))
            return '\n'.join(parts)
        if isinstance(payload, dict) and payload.get("content_summary"):
            current_source += f"本次来源内容摘要={_safe_excerpt(payload['content_summary'], 2000)}\n"
        from .node_account import account_material
        payload, account = account_material(event_type.value, payload)
        # Current music and news reading retain all received material; their
        # later personal accounts are separate from the source text.
        if event_type == EventType.LISTEN_MUSIC:
            from .music_listening import material_for_k
            payload = material_for_k(payload)
        if event_type == EventType.PLAY_GAME:
            from .game_results import review_material
            payload = review_material(payload)
        material = (json.dumps(redact_sensitive(payload), ensure_ascii=False, default=str)
                    if event_type in {EventType.LISTEN_MUSIC, EventType.BROWSE_NEWS, EventType.PLAY_GAME}
                    else _safe_excerpt(payload, 2500))
        source_summary = current_node.get('source_summary') or ''
        if account and source_summary.strip() == account['text']:
            source_summary = ''
        current_source += (source_summary + '\n' if source_summary else '') + material
        parts.append(current_source)
        if account:
            parts.append('[' + account['kind'] + ']\n' + str(redact_sensitive(account['text'])))
    if settlement_reason:
        parts.append(f"[结算原因]\n{settlement_reason}")
    return "\n".join(parts)


from .decision_contracts import (
    NODE_REVIEW as _NODE_REVIEW_INSTRUCTION, SETTLEMENT as _SETTLEMENT_INSTRUCTION,
    MUSIC_REVIEW, XHS_REVIEW, XHS_CLOSEOUT, WORKSHOP_FACTS, GAME_FACTS,
    CLOSEOUT_FIELDS,
    WANDER_FEELING_INSTRUCTION,
)

_HORIZON_REVIEW_INSTRUCTION = """只输出 JSON 对象，包含 continue_activity（布尔值）、extend_minutes（非负整数）、next_node_delay_seconds（非负数字）和 reason（字符串）。
这是原先预计时间到达后的复核：对照本次活动起意、已完成的经历和现在的兴趣，决定继续或结束这次活动。继续时填写真实想继续的原因、正整数 extend_minutes 和下一节点等待秒数；停止时不要假装已经执行新节点。"""
_HORIZON_REVIEW_INSTRUCTION += ('\n继续时 closeout=null；停下时在同一回答中填写 closeout。\n'
                              + CLOSEOUT_FIELDS + '\n' + WANDER_FEELING_INSTRUCTION)


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
        activity: dict[str, Any],
    ) -> list[dict[str, str]]:
        if not context.persona.strip():
            raise DecisionContextError("persona_is_required")
        if context.session_id != run["session_id"]:
            raise DecisionContextError("session_id_mismatch")
        try:
            nodes = self.store.list_nodes(activity['activity_id'])
            context_query = recall_query(activity['activity_type'], activity.get('reason') or '',
                                         current_progress(self.store, activity, run), nodes)
            recipe_kwargs = dict(
                wander_runtime_text=runtime_block,
                persona=context.persona,
                session_id=context.session_id,
                context_query=context_query,
                **activity_history_scope(self.store, activity),
            )
            if self.context_builder is not None:
                built = await self.context_builder("WANDER_ACTIVITY", **recipe_kwargs)
            else:
                from context_builder.builder import ContextBuilder

                built = await ContextBuilder.build("WANDER_ACTIVITY", **recipe_kwargs)
        except DecisionContextError:
            raise
        except Exception as exc:
            raise DecisionContextError(f"recipe_build:{type(exc).__name__}") from exc

        from context_builder.assembly import assemble_context_text, assemble_context_result
        from context_builder.channel_projection import backend_turn
        system_content = assemble_context_text(built)
        sections = getattr(built, "sections", {}) or {}
        runtime_section = sections.get("wander_runtime", {})
        if (
            not system_content
            or not runtime_section.get("text")
            or not system_content.rstrip().endswith(runtime_block.rstrip())
        ):
            raise DecisionContextError("runtime_block_not_final_or_missing")
        return assemble_context_result(built) + [backend_turn(instruction)]

    @staticmethod
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


class NodeReviewAdapter(_DecisionAdapterBase):
    async def review(
        self,
        node_id: str,
        context: DecisionContext,
        *,
        allowed_event_types: Optional[frozenset[EventType]] = None,
        gallery_pages=None,
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
        from .memory_fetch_result import empty_lookup
        if (
            node["state"] != NodeState.RUNNING.value
            or (node["execution_status"] != "succeeded" and not empty_lookup(node))
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
            purpose_progress=current_progress(self.store, activity, run),
        )
        if allowed_event_types is not None:
            try:
                last_seen = self.store.completed_last_seen_by_type()
            except Exception:
                last_seen = {}
            runtime_block += "\n[当前可续作事件]\n" + (
                render_event_options(allowed_event_types, last_seen=last_seen) or "无"
            )
        instruction = _NODE_REVIEW_INSTRUCTION
        from .reading_illustrations import share_option
        instruction += share_option(nodes)
        if empty_lookup(node):
            instruction += '\n这次没有找到新记忆。你可以换条线索继续回想、换件事，或停下；感想可留空。'
        if (node.get('source_payload') or {}).get('essay_version') == 1:
            from .essays import REVIEW_INSTRUCTION
            instruction += REVIEW_INSTRUCTION
        if activity['activity_type'] == EventType.LISTEN_MUSIC.value:
            instruction += '\n' + MUSIC_REVIEW
        if activity['activity_type'] == EventType.BROWSE_XIAOHONGSHU.value:
            instruction += '\n' + XHS_REVIEW + '\ncloseout 中：' + XHS_CLOSEOUT
            labels = (node.get('source_payload') or {}).get('gallery_labels')
            if labels and labels != [label for label, _ in gallery_pages or ()]:
                self._record_review_failure(run, activity, node, runtime_block,
                                            'context_error', 'captured_gallery_no_longer_available')
                return None
            if gallery_pages:
                from .xhs_gallery_review import reading_contract
                instruction += reading_contract(gallery_pages)
        if activity.get('activity_type') == EventType.USER_TRACKING.value:
            instruction += ('\n查岗的 checkup_items 是本次感官回执；reflection 记录你自己的感受与猜想。'
                            '未知的室内活动仍是未知，猜想按猜想表达；工具查询成功只说明拿到了该来源的回执。'
                            '\n在同一 JSON 中补充 goal_assessment：'
                            '{"status":"confirmed 或 uncertain","reason":"本次起意的问题是否已得到回答及依据",'
                            '"evidence_sources":["实际使用的 checkup_items.tool"]}。'
                            '该评估针对本次起意的信息缺口，不是工具是否调用成功；'
                            'confirmed 引用已取得且可理解的来源，uncertain 保留仍不知道的部分。')
        if activity.get("activity_type") in {"visit_workshop", "workshop_create", "workshop_history", "workshop_continue"}:
            instruction += '\n' + WORKSHOP_FACTS
        if activity.get("activity_type") == EventType.PLAY_GAME.value:
            runtime_block += "\n[本次游戏已执行动作]\n" + _game_execution_facts([node])
            instruction += '\n' + GAME_FACTS
            runtime_block += '\n[本周目此前进展]\n' + _prior_game_progress(
                self.store.list_game_history(), nodes, activity['activity_id'])
        try:
            messages = await self._messages(
                context,
                run,
                runtime_block,
                instruction,
                activity,
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
                route_label="wander_node_review",
                **({'gallery_images': [image for _, image in gallery_pages]} if gallery_pages else {}),
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
        if detailed.status == 'ok' and gallery_pages:
            from .xhs_gallery_review import observations
            try:
                values = observations(raw, gallery_pages)
            except ValueError:
                detailed.status = 'validation_error'
                detailed.error = 'gallery_observations_incomplete'
            else:
                self.store.record_xhs_page_reading(node_id, values)
        if detailed.status == 'ok' and (node.get('source_payload') or {}).get('material_version') == 1:
            self.store.merge_node_side_effect_refs(node_id, {'xhs_material_received': True})
        decision = self._normalize_review(raw, allowed_event_types) if raw is not None and detailed.status == "ok" else None
        if decision is not None and raw.get('reuse_account') is True:
            from .node_account import account_material
            _, account = account_material(activity['activity_type'], node.get('source_payload') or {})
            if account and account['kind'] == '本节点已有自述':
                decision.reflection = account['text']
        if decision is not None and activity.get('activity_type') == EventType.USER_TRACKING.value:
            from checkup_assessment import normalize_goal_assessment
            decision.goal_assessment = normalize_goal_assessment(
                raw.get('goal_assessment'), (node.get('source_payload') or {}).get('checkup_items'))
        if decision is not None and (node.get('source_payload') or {}).get('material_version') == 1:
            # Receipt of material and an attributable, valid AI review are distinct facts.
            self.store.merge_node_side_effect_refs(node_id, {'xhs_review_succeeded': True})
            decision.reflection = raw['reflection'].strip() if isinstance(raw.get('reflection'), str) else ''
        if (decision is not None and activity.get("activity_type") == EventType.PLAY_GAME.value
                and (node.get("source_payload") or {}).get("game_complete")):
            # A completed remote playthrough cannot produce another move in
            # the same save. AI may still switch to a different activity.
            decision.continue_activity = False
            if decision.switch_to is None:
                decision.abort_reason = "游戏周目已完结"
        if decision is not None:
            anchor = self.clock()
            from .review_closeout import normalize_closeout
            decision.closeout = normalize_closeout(raw.get('closeout'),
                reflection=decision.reflection, effect=decision.emotion_effect,
                activity=activity, nodes=nodes, anchor=anchor)
            if decision.closeout and not decision.closeout.purpose_outcome:
                decision.closeout.purpose_outcome = decision.purpose_outcome
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
                "user": instruction,
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
        reflection = str(raw.get("reflection") or "").strip()
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
        extend_count = normalize_extension(raw.get("extend_count"))
        switch_goal = raw.get("switch_goal") if isinstance(raw.get("switch_goal"), dict) else None
        return NodeReviewDecision(
            reflection=reflection,
            purpose_outcome=normalize_outcome(raw.get('purpose_outcome')),
            collect_music=raw.get("collect_music") is True,
            music_library_action=_normalize_music_library_action(raw.get("music_library_action")),
            keep_post=raw.get("keep_post") is True,
            keep_post_reason=str(raw.get("keep_post_reason") or "").strip()[:300],
            emotion_effect=_normalize_effect(raw.get("emotion_effect")),
            continue_activity=continue_activity,
            abort_reason=abort_reason,
            switch_to=switch_to,
            next_node_delay_seconds=delay,
            timing_reason=str(raw.get("timing_reason") or "").strip()[:300],
            timing_source=source,
            extend_count=extend_count,
            goal_reason=str(raw.get("goal_reason") or "").strip(),
            switch_goal=switch_goal,
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
        from .settlement_outcome import constrain_unexecuted
        constrain_unexecuted(fallback, activity, nodes, reason)
        runtime_block = _runtime_block(
            context,
            activity,
            nodes,
            phase="settlement",
            run=run,
            settlement_reason=reason.value,
            purpose_progress=current_progress(self.store, activity, run),
        )
        instruction = _SETTLEMENT_INSTRUCTION
        from .reading_illustrations import share_option
        instruction += share_option(nodes)
        if any((n.get('source_payload') or {}).get('essay_version') == 1 for n in nodes):
            instruction += ('\n写成的随笔保存在书柜，summary 简记篇目与写作感受。'
                            '想分享一篇原文卡片时，填 essay_share_node_id 为该篇 node_id，同时 share=true；不分享则留空。')
        successors = [item for item in self.store.list_activities(activity["run_id"])
                      if int(item.get("order_index") or 0) > int(activity.get("order_index") or 0)
                      and item.get("state") == ActivityState.PLANNED.value]
        runtime_block += "\n[本段已规划后继]\n" + json.dumps([
            {key: item.get(key) for key in ("activity_id", "activity_type", "reason", "next_node_at")}
            for item in successors
        ], ensure_ascii=False)
        if successors:
            instruction += '\n本段后继已确定；本次仅收尾当前活动，next_run_delay_seconds、timing_reason 留空。'
        if activity.get('activity_type') == EventType.USER_TRACKING.value:
            instruction += '\n查岗回执支持已观察到的部分；自己的猜想和仍不知道的事按原来的把握表达。'
        if activity.get('activity_type') in {'visit_workshop', 'workshop_create', 'workshop_history', 'workshop_continue'}:
            instruction += '\n' + WORKSHOP_FACTS
        if activity.get("activity_type") == EventType.LISTEN_MUSIC.value:
            from .music_receipt import music_execution
            runtime_block += "\n[本次听歌执行回执]\n" + json.dumps([
                music_execution(node)
                for node in nodes if node.get("state") == NodeState.COMPLETED.value
                or ((node.get('source_payload') or {}).get('listening_experience') or {}).get('started_at')
            ], ensure_ascii=False)
            instruction += ("\n本次是听歌活动。summary 以本次歌曲材料和节点感受为主体；"
                            "先前游戏等经历若确实形成联想，只作为此前经历说明。")
            instruction += MUSIC_EXPRESSION_CONTRACT
        if activity.get("activity_type") == EventType.PLAY_GAME.value:
            game_history = self.store.list_game_history()
            runtime_block += "\n[本次游戏已执行动作]\n" + _game_execution_facts(nodes)
            runtime_block += "\n[本次游戏完成事实]\n" + json.dumps({
                "confirmed_complete_this_activity": any(
                    isinstance(node.get("source_payload"), dict)
                    and node["source_payload"].get("game_complete") is True
                    for node in nodes if node.get("execution_status") == "succeeded"
                ),
            }, ensure_ascii=False)
            runtime_block += "\n[本周目先前已确认的章节与感想、已完结其他周目]\n" + _prior_game_progress(
                game_history, nodes, activity_id)
            instruction += ("\n本次为真实推进的虚构游戏。summary 用自然的一小段话串联本次最有分量的选择、结果和 AI 的感受；"
                            "游戏站原文、字段名、JSON 和逐步流水留在游戏记录里，不搬进这段感想。"
                            "retained_impression 由你选择真正留下的感受及少数印象深刻的选择，不需复述全部章节路径；"
                            "只有本次节点确认 game_complete 时，才结合本周目先前活动写这一独立周目的完结总结。"
                            "以前的周目是可比较的经历，不要把其结果当成本周目发生，也不要覆盖或复写旧总结。"
                            "未确认通关时写本次推进的阶段心得与下次可接续的检查点；此前结算文字不是本次结果证据。"
                            "start_next_chapter 只进入新章场景，只有 play_step 才表示已选场景中的具体选项。")
            if reason == SettlementReason.GAME_STOP:
                instruction += "\n人类伙伴从机生活结束了本次游玩；按已确认的步骤记录阶段心得和存档位置。"
        valid_xhs_nodes = {
            str(node.get("node_id") or "") for node in nodes
            if node.get("state") == NodeState.COMPLETED.value
            and (node.get("source_payload") or {}).get("status") == "success"
        } if activity.get("activity_type") == EventType.BROWSE_XIAOHONGSHU.value else set()
        if valid_xhs_nodes:
            runtime_block += "\n[本次实际已读帖子节点]\n" + "\n".join(
                f"node_id={node['node_id']}；{str(node.get('source_summary') or '')[:180]}"
                for node in nodes if str(node.get("node_id") or "") in valid_xhs_nodes
            )
            instruction += ("\n这次若想把其中一篇帖做成卡片寄给人类伙伴，在 xhs_share_node_id 填上面已读节点的准确 node_id；"
                            "若还想请人类伙伴亲自评论，可填一句 xhs_comment_draft。只想谈感想而不寄卡片时两个字段留空。"
                            "卡片是否有原帖链接由本地真实链接证据决定；没有链接时评论草稿不会作为邀请寄出。")

        try:
            messages = await self._messages(
                context,
                run,
                runtime_block,
                instruction,
                activity,
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
                route_label="wander_settlement",
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
        decision = self._normalize_settlement(raw, fallback, valid_xhs_nodes) if raw is not None else fallback
        completed_ids = {str(node.get("node_id") or "") for node in nodes
                         if node.get("state") == NodeState.COMPLETED.value}
        from memory_v2.experience_contract import impression_text
        decision.retained_impression = impression_text(
            decision.retained_impression,
            node_ids={str(node.get("node_id") or "") for node in nodes},
        )
        if (decision.retained_node_ids and not set(decision.retained_node_ids) <= completed_ids) or (
                decision.retained_impression and not completed_ids):
            decision.retained_impression = ""
            decision.retained_node_ids = []
        if (activity.get("activity_type") == EventType.PLAY_GAME.value
                and reason not in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR)
                and any(node.get("execution_status") == "succeeded" for node in nodes)):
            # 人类伙伴明确想听 AI 玩完回来的感想；分享内容仍由推送模型
            # 结合真实步骤撰写，只有已确认的游玩进展才触发这一约定。
            decision.share = True
        elif activity.get("activity_type") == EventType.PLAY_GAME.value and reason == SettlementReason.GAME_STOP:
            decision.share = False
        from .settlement_outcome import constrain_unexecuted, unexecuted
        constrain_unexecuted(decision, activity, nodes, reason)
        anchor = self.clock()
        if successors:
            decision.next_run_delay_seconds, decision.next_plan_at = None, ''
            decision.timing_source, decision.timing_reason = 'successor', ''
        else:
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
                "user": instruction,
            },
            raw_output=detailed.raw_content,
            reasoning=detailed.reasoning,
            parsed_output={"raw": raw or {}, "normalized": {**asdict(decision),
                'unexecuted': unexecuted(activity, nodes, reason)}},
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
        valid_xhs_nodes: set[str] | None = None,
    ) -> SettlementDecision:
        delay, source = normalize_delay_seconds(raw.get("next_run_delay_seconds"))
        selected = str(raw.get("xhs_share_node_id") or "").strip()
        if selected not in (valid_xhs_nodes or set()) or not _as_bool(raw.get("share")):
            selected = ""
        return SettlementDecision(
            summary=str(raw.get("summary") or fallback.summary).strip(),
            purpose_outcome=normalize_outcome(raw.get('purpose_outcome')),
            retained_impression=str(raw.get("retained_impression") or "").strip(),
            retained_node_ids=[str(item) for item in raw.get("retained_node_ids", [])
                               if isinstance(item, str)] if isinstance(raw.get("retained_node_ids"), list) else [],
            share=_as_bool(raw.get("share")),
            continue_next=_as_bool(raw.get("continue_next")),
            next_inclination_note=str(raw.get("next_inclination_note") or "").strip(),
            emotion_effect=_normalize_effect(raw.get("emotion_effect")),
            next_run_delay_seconds=delay,
            timing_reason=str(raw.get("timing_reason") or "").strip()[:300],
            timing_source=source,
            xhs_share_node_id=selected,
            essay_share_node_id=str(raw.get('essay_share_node_id') or '').strip(),
            reading_picture_node_id=str(raw.get('reading_picture_node_id') or '').strip() if _as_bool(raw.get('share')) else '',
            xhs_comment_draft=str(raw.get("xhs_comment_draft") or "").strip()[:1000] if selected else "",
        )

    @staticmethod
    def _fallback(
        activity: dict[str, Any],
        nodes: list[dict[str, Any]],
        reason: SettlementReason,
    ) -> SettlementDecision:
        if activity.get('activity_type') == EventType.USER_TRACKING.value:
            from checkup_orchestrator import grounded_tracking_summary
            # No valid model account was returned. Use the exact receipt
            # fallback, which downstream recognizes as fact, not a feeling.
            return SettlementDecision(summary=grounded_tracking_summary(nodes))
        if activity.get('activity_type') == EventType.LISTEN_MUSIC.value and any(
                ((node.get('source_payload') or {}).get('listening_experience') or {}).get('started_at')
                and node.get('execution_status') != 'succeeded' for node in nodes):
            from memory_v2.experience_contract import snapshot, project
            return SettlementDecision(summary=project(snapshot(activity, nodes)) or '这次听歌尚未完整结束。')
        label = strategy_for(EventType(activity["activity_type"])).display_name
        completed = [node for node in nodes if node.get("state") == NodeState.COMPLETED.value]
        if activity.get("activity_type") == EventType.PLAY_GAME.value and completed:
            from .game_experience import game_fallback_summary
            return SettlementDecision(summary=game_fallback_summary(activity, completed))
        if completed:
            facts = "、".join(node.get("source_summary") or "一个节点" for node in completed[-5:])
            summary = f"刚才在{label}，完成了：{facts}。"
        elif reason == SettlementReason.USER_INTERRUPT:
            summary = f"刚才准备{label}，人类伙伴回来后停下了。"
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
                    from .review_closeout import restore_closeout
                    return HorizonReviewDecision(**{k: normalized.get(k) for k in (
                        "continue_activity", "extend_minutes", "next_node_delay_seconds", "reason", "new_horizon", "next_node_at")},
                        closeout=restore_closeout(normalized.get('closeout')))
                return None
        nodes = self.store.list_nodes(activity_id)
        block = _runtime_block(context, activity, nodes, phase="timing_review", run=run,
                               purpose_progress=current_progress(self.store, activity, run)) + (
                f"\n[计划时间事实]\nhorizon_mode=estimate；expected_horizon={old_horizon}；"
                f"next_node_at={activity.get('next_node_at') or '无'}"
            )
        raw, detailed, messages = {}, None, None
        try:
            from .reading_illustrations import share_option
            instruction = _HORIZON_REVIEW_INSTRUCTION + share_option(nodes)
            messages = await self._messages(context, run, block, instruction, activity)
            detailed = await self.llm_caller(messages=messages, temperature=0.0, max_tokens=16384,
                route_label="wander_timing_review")
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
                from .review_closeout import normalize_closeout
                reflection = next((n.get('reflection') for n in reversed(nodes) if n.get('reflection')), '')
                decision.closeout = normalize_closeout(raw.get('closeout'), reflection=reflection,
                    effect=EmotionEffect(), activity=activity, nodes=nodes, anchor=self.clock())
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
            recipe="WANDER_ACTIVITY", input_context={"runtime": block, "user": instruction},
            raw_output=getattr(detailed, "raw_content", ""), reasoning=getattr(detailed, "reasoning", ""),
            duration_ms=getattr(detailed, "duration_ms", None),
            parsed_output={"raw": raw or {}, "normalized": normalized}, status=status, error=error)
        return decision
