"""One bounded textual view of a settled Wander timeline event."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


RETENTION_STRONG = "strong"
RETENTION_NORMAL = "normal"
RETENTION_WEAK = "weak"
NORMAL_FULL_SECONDS = 2 * 60 * 60


_DAILY_STATE_LABELS = {
    "completed": "完成", "settled": "本次已结束", "failed": "失败",
    "interrupted": "中断", "stopped": "停止", "canceled": "取消",
}
_DAILY_STATUS_LABELS = {
    "succeeded": "成功", "failed": "失败", "skipped": "跳过",
    "rejected": "拒绝", "blocked": "阻止", "unknown": "未知",
}


def _daily_stamp(timestamp: str) -> str:
    stamp = str(timestamp or "")
    if len(stamp) >= 16:
        return f"{stamp[:10]} {stamp[11:16]}"
    return stamp[:10]


def _daily_progress(overview: Any, *, include_counts: bool = True) -> str:
    """Surface progress only when the activity did not simply succeed."""
    if not isinstance(overview, dict):
        return ""
    parts = []
    state = str(overview.get("state") or "").strip()
    if state and state not in {"completed", "settled"}:
        parts.append("活动状态：" + _DAILY_STATE_LABELS.get(state, state))
    if overview.get('stop_reason'):
        parts.append(str(overview['stop_reason']))
    parts.extend(str(item) for item in overview.get('memory_lookup_attempts') or [])
    counts = overview.get("execution_status_counts")
    if include_counts and isinstance(counts, dict):
        abnormal = {key: value for key, value in counts.items()
                    if value and str(key) != "succeeded"}
        if abnormal:
            parts.append("执行：" + "、".join(
                f"{_DAILY_STATUS_LABELS.get(str(key), str(key))} {value} 次"
                for key, value in abnormal.items()))
    return "；".join(parts)


def _same_text(left: Any, right: Any) -> bool:
    left = "".join(str(left or "").split())
    right = "".join(str(right or "").split())
    return bool(left and right and left == right)


def _first_call(tool_calls: Any) -> dict:
    calls = tool_calls
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (TypeError, ValueError):
            calls = []
    return calls[0] if isinstance(calls, list) and calls and isinstance(calls[0], dict) else {}


def _emotion_effect(extra: Any) -> dict:
    if not isinstance(extra, dict):
        return {}
    effect = extra.get("emotion_effect")
    if isinstance(effect, str):
        try:
            effect = json.loads(effect)
        except (TypeError, ValueError):
            return {}
    return effect if isinstance(effect, dict) else {}


def retention_tier(tool_calls: Any) -> str:
    """How much of one settled event stays in the immediate chat context.

    New rows carry the settlement emotion effect. Old rows have no such field;
    they keep their existing full projection instead of being reclassified
    retroactively.
    """
    first = _first_call(tool_calls)
    extra = first.get("extra_data") if isinstance(first.get("extra_data"), dict) else {}
    effect = _emotion_effect(extra)
    if effect:
        if effect.get("changed") and str(effect.get("persistence") or "") == "lingering":
            return RETENTION_STRONG
        if effect.get("changed"):
            return RETENTION_NORMAL
        return RETENTION_WEAK
    # Legacy rows have no emotion effect. Preserve their existing projection
    # rather than retroactively reclassifying an already indexed experience.
    return RETENTION_NORMAL


def seconds_since(timestamp: str, now: datetime | None = None) -> float | None:
    """Seconds since the event timestamp; unknown or malformed stays unknown."""
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    current = now or datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=current.tzinfo)
    elif current.tzinfo is None:
        current = current.replace(tzinfo=stamp.tzinfo)
    try:
        return max(0.0, (current - stamp).total_seconds())
    except (TypeError, ValueError):
        return None


def format_wander_activity_for_chat(summary: str, tool_calls: Any, message_id: str = "",
                                    *, timestamp: str = "", now: datetime | None = None,
                                    strong_full: bool = True) -> tuple[str, str]:
    """Salience-tiered projection for the live main-chat context."""
    tier = retention_tier(tool_calls)
    if tier == RETENTION_STRONG:
        level = "daily" if strong_full else "brief"
    elif tier == RETENTION_NORMAL:
        age = seconds_since(timestamp, now)
        level = "daily" if age is not None and age <= NORMAL_FULL_SECONDS else "brief"
    else:
        level = "action"
    text = format_wander_activity_event(
        summary, tool_calls, message_id, level=level, timestamp=timestamp,
        include_completed_review=True, live_reading=True)
    if tier == RETENTION_STRONG:
        text = "（强烈感想）\n" + text
    return text, tier


def _daily_material_lines(value: Any, timestamp: str, *, retained_impression='', reading_in_action=False) -> list[str]:
    """References only; no material body is ever expanded or cut here."""
    if not isinstance(value, dict):
        return []
    stamp = _daily_stamp(timestamp)
    lines: list[str] = []
    music_run: list[str] = []
    def flush_music():
        if music_run:
            lines.append('音乐：' + '、'.join(music_run) + '。')
            music_run.clear()
    for item in value.get("materials") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind != 'music':
            flush_music()
        if kind == 'xhs_interactions':
            from xhs_interactions.context import brief, capture
            view = capture(item)
            lines.append(brief(view, timestamp=timestamp,
                               include_feeling=view.get('reflection') != retained_impression))
        elif kind == "checkup" and item.get("version") == 1:
            from wander_manager.experience_presentation import state_text
            state = item.get('source_status') or item.get('conclusion') or 'unknown'
            goal = (item.get('goal_assessment') or {}).get('status') or 'not_assessed'
            gap = '；关心的问题仍未确认' if goal != 'confirmed' else '；关心的问题已有依据'
            lines.append('这次观察' + state_text(state) + gap + '。逐项观察可在查岗记录中查看。')
        elif kind == "game":
            from wander_manager.game_results import result_text
            lines.extend(result_text(fact, titles_only=True) for fact in item.get('results') or [])
            chapters = item.get('chapters') or []
            labels = ['第' + str(c.get('number')) + '章' + ('《' + str(c['title']) + '》' if c.get('title') else '')
                      for c in chapters if isinstance(c, dict) and c.get('number') is not None]
            lines.append('游戏经历（虚构）：' + ('、'.join(labels) or str(item.get('game_name') or '本次进展'))
                         + '；选择、结局与章末感想在游戏记录中。')
        elif kind == "music":
            title = str(item.get("title") or "").strip()
            artist = str(item.get("artist") or "").strip()
            entry = f'《{title}》' + (f'（{artist}）' if artist else '') if title else '一首曲目'
            if item.get('completion_signal') == 'analysis_complete':
                entry += '（材料分析）'
            music_run.append(entry)
        elif kind == "music_listening_attempt":
            title = str(item.get("title") or "").strip()
            receipt = item.get("music_execution") or {}
            lines.append("听歌尝试：" + (f"《{title}》" if title else "一首曲目")
                         + "；" + str(receipt.get("fact") or "完成状态未知") + "。")
        elif kind == "reading" and not reading_in_action:
            count = str(item.get("displayed_message_count") or "").strip()
            lines.append("可回溯：回看历史对话摘录"
                         + (f" {count} 条" if count else "")
                         + "（原文在历史对话）")
        elif kind == "workshop":
            # Old material stores the whole room/work text in ``description``;
            # its title cannot be inferred reliably.  Give the verifiable
            # timestamp instead of guessing an artifact.
            lines.append("可回溯：工作坊材料（作品柜中可查；本次 "
                         + (stamp or "时间见事件") + "）")
        elif kind == "xhs":
            title = str(item.get("title") or "").strip()
            lines.append("可回溯：小红书帖子" + (f"《{title}》" if title else "")
                         + "（来源记录中可查）")
        elif kind == "xhs_publication":
            title = str(item.get("title") or "").strip()
            lines.append("可回溯：小红书发布" + (f"《{title}》" if title else "")
                         + "（发布记录保留在来源库）")
    flush_music()
    return lines


def _daily_projection(summary: str, extra: Any, result: str,
                      timestamp: str = "", *, reading_in_action=False) -> str:
    """Daily-entry view: event, progress, feeling and references."""
    lines = [result]
    progress = _daily_progress(extra.get("execution_overview"))
    if progress:
        lines.append(progress)
    thought = str(summary or "").strip()
    frozen = extra.get("memory_experience")
    interactions = (isinstance(frozen, dict) and any(
        item.get('kind') == 'xhs_interactions' for item in frozen.get('materials') or []))
    if thought and not interactions:
        lines.append(thought)
    if isinstance(frozen, dict) and frozen.get("version") == 2:
        from memory_v2.experience_contract import impression_text
        retained = impression_text(frozen.get("retained_impression"),
                                   node_ids=frozen.get("retained_node_ids") or [])
        settlement = str(frozen.get("settlement_reflection") or "").strip()
        if retained and not _same_text(retained, thought):
            lines.append(retained)
        if settlement and not _same_text(settlement, retained) and not _same_text(settlement, thought):
            lines.append(settlement)
        lines.extend(_daily_material_lines(frozen, timestamp, retained_impression=retained, reading_in_action=reading_in_action))
        purpose = frozen.get('purpose_outcome') or {}
        for key, label in (('obtained', '得到的'), ('remaining', '还想接着')):
            text = str(purpose.get(key) or '').strip()
            if text and not any(_same_text(text, paragraph) for paragraph in (thought, retained, settlement)):
                lines.append(label + '：' + text)
    else:
        retained = extra.get("retained_experience")
        impression = str(retained.get("impression") or "").strip() if isinstance(retained, dict) else ""
        if impression and not _same_text(impression, thought):
            lines.append(impression)
    return "\n".join(line for line in lines if line)


def _brief_projection(summary: str, extra: dict, result: str) -> str:
    """Action, abnormal progress and one short feeling; no material references."""
    lines = [result]
    # New frozen result facts are stable across reads; legacy events are not
    # retroactively rewritten or enriched by querying mutable runtime state.
    frozen = extra.get('memory_experience') or {}
    from wander_manager.game_results import result_text
    for material in frozen.get('materials') or []:
        if material.get('kind') == 'game':
            lines.extend(result_text(fact, titles_only=True) for fact in material.get('results') or [])
    progress = _daily_progress(extra.get("execution_overview"))
    if progress:
        lines.append(progress)
    feeling = str(summary or "").strip()
    if not feeling:
        frozen = extra.get("memory_experience") if isinstance(extra.get("memory_experience"), dict) else {}
        retained = extra.get("retained_experience") if isinstance(extra.get("retained_experience"), dict) else {}
        feeling = str(frozen.get("settlement_reflection") or frozen.get("retained_impression")
                      or retained.get("impression") or "").strip()
    if feeling:
        first = feeling.split("。", 1)[0].strip()
        if len(first) > 80:
            first = first[:80].rstrip() + "…"
        elif first:
            first += "。"
        lines.append(first)
    return "\n".join(line for line in lines if line)


def format_wander_activity_event(summary: str, tool_calls: Any, message_id: str = "",
                                 *, level: str = "detail", timestamp: str = "",
                                 include_interrupted_review: bool = True,
                                 include_completed_review: bool = False, live_reading: bool = False) -> str:
    if level not in {"fact", "experience", "daily", "detail", "action", "brief"}:
        raise ValueError("unknown wander event projection level")
    calls = tool_calls
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (TypeError, ValueError):
            calls = []
    first = calls[0] if isinstance(calls, list) and calls else {}
    description = (
        str(first.get("description") or "").strip()[:60]
        if isinstance(first, dict) else ""
    ) or "一次漫想活动已结算"
    thought = str(summary or "").strip()
    game = isinstance(first, dict) and str(first.get("tool") or "").strip().endswith("玩游戏")
    source_label = "AI 的虚构游戏漫想事件" if game else "AI 的漫想事件"
    result = f"{source_label}：{description}。"
    extra = (first.get("extra_data") or {}) if isinstance(first, dict) else {}
    if not isinstance(extra, dict):
        extra = {}
    reading_action = ''
    if live_reading:
        from wander_manager.bookshelf_actions import activity_description
        reading_action = activity_description(extra.get('memory_experience'))
        if reading_action:
            result = f'{source_label}：{reading_action}。'
    if 'essay_index' in extra:
        from wander_manager.essays import index_text
        text = index_text(extra['essay_index']) or result
        overview = extra.get('execution_overview') or {}
        return '\n'.join(part for part in (text, overview.get('stop_reason', '')) if part)
    if level == "action":
        overview = extra.get("execution_overview")
        state = overview.get('state') if isinstance(overview, dict) else ''
        progress = _daily_progress(overview)
        review = ''
        if ((include_interrupted_review and state == 'interrupted')
                or (include_completed_review and state in {'completed', 'settled'})):
            frozen = extra.get('memory_experience')
            review = thought or (str(frozen.get('settlement_reflection') or '').strip()
                                 if isinstance(frozen, dict) else '')
            if review and state == 'interrupted':
                progress = _daily_progress(overview, include_counts=False)
        if _same_text(review, description) or _same_text(review, result):
            review = ''
        from wander_manager.game_results import result_text
        frozen = extra.get('memory_experience') or {}
        game_results = [result_text(fact, titles_only=True)
                        for material in frozen.get('materials') or [] if material.get('kind') == 'game'
                        for fact in material.get('results') or []]
        return "\n".join(line for line in (result, progress, *game_results, review) if line)
    if level == "brief":
        return _brief_projection(summary, extra, result)
    if level == "daily":
        return _daily_projection(summary, extra, result, timestamp, reading_in_action=bool(reading_action))
    frozen = extra.get('memory_experience')
    if isinstance(frozen, dict) and frozen.get('version') == 2 and level != 'fact':
        from wander_manager.experience_presentation import frozen_text
        return frozen_text(frozen) or result
    overview = extra.get("execution_overview") if isinstance(extra, dict) else None
    if isinstance(overview, dict):
        result += "\n执行回执概况：" + json.dumps(overview, ensure_ascii=False)
    if level == "fact":
        return result
    retained = extra.get("retained_experience") if isinstance(extra, dict) else None
    impression = str(retained.get("impression") or "").strip() if isinstance(retained, dict) else ""
    if level == "experience":
        if impression:
            return result + f"\nAI 当时选择留下的感受：{impression}"
        # Legacy non-game events had only a settlement thought. Preserve its
        # continuity; old game recaps are often whole chapter transcripts.
        if retained is None and not game and not str(first.get("tool") or "").strip().endswith("自己听歌") and thought:
            return result + f"\n当时的结算感受：{thought}"
        return result
    if game:
        from wander_manager.game_experience import (
            compact_chapter_facts, game_fallback_from_chapters,
            is_game_fallback_summary, read_activity_chapter_facts,
        )

        game_summary = (extra.get("game_summary") or {}) if isinstance(extra, dict) else {}
        game_summary = game_summary if isinstance(game_summary, dict) else {}
        chapters = game_summary.get("chapters") if isinstance(game_summary, dict) else None
        if not isinstance(chapters, list) and message_id.startswith("activity_event_"):
            chapters = read_activity_chapter_facts(message_id[len("activity_event_"):])
        if chapters:
            result += "\n已确认的游戏章节事实（虚构作品中的 AI 选择）：\n" + compact_chapter_facts(chapters)
        if is_game_fallback_summary(thought):
            thought = game_fallback_from_chapters(
                str(game_summary.get("game_name") or "游戏"), chapters or [],
                completed=game_summary.get("completed") is True,
            )
    if thought:
        result += ("\n" if game else "") + f"当时的结算感受：{thought}"
    if impression:
        result += f"\nAI 当时选择留下的感受：{impression}"
    return result


def format_wander_communication(message: dict[str, Any]) -> str:
    """A sent utterance is evidence of communication, separate from execution."""
    return "历史通信记录（AI 当时发给人类伙伴的原话；行动状态由对应活动回执记录）：\n" + json.dumps({
        "message_id": message.get("_message_id") or "",
        "sent_at": message.get("_ts") or "",
        "speaker": "AI",
        "sent_wording": message.get("_spoken_content", message.get("content", "")),
    }, ensure_ascii=False)


def wander_activity_summary_text(summary: str, tool_calls: Any, message_id: str = "",
                                 *, timestamp: str = "") -> str:
    """Day-summary view: weak keeps the action and any saved interruption account."""
    tier = retention_tier(tool_calls)
    level = 'experience' if tier in {RETENTION_STRONG, RETENTION_NORMAL} else 'action'
    return format_wander_activity_event(summary, tool_calls, message_id,
        level=level, timestamp=timestamp)


def inner_event_text(row, *, frozen_only=False, include_completed_review=False):
    """Daily reading view; internal boxes keep their admitted source version."""
    summary = str(row.get('content') or '')
    if frozen_only and summary:
        from memory_v2.source_projection import memory_source_content
        # Old snapshots did not freeze every SQL summary. Do not import that
        # separate current text into a box whose source only binds the snapshot.
        if summary not in (memory_source_content(row) or ''):
            summary = ''
    tier = retention_tier(row.get('tool_calls'))
    level = 'daily' if tier in {RETENTION_STRONG, RETENTION_NORMAL} else 'action'
    extra = _first_call(row.get('tool_calls')).get('extra_data') or {}
    include_review = not frozen_only or (isinstance(extra, dict) and extra.get('interrupted_review_version') == 1)
    return format_wander_activity_event(summary, row.get('tool_calls'),
        str(row.get('message_id') or ''), level=level, timestamp=str(row.get('timestamp') or ''),
        include_interrupted_review=include_review, include_completed_review=include_completed_review, live_reading=True)


__all__ = ["format_wander_activity_event", "format_wander_communication"]
