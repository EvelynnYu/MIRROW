"""Read-only projections of execution, subjective experience and activity scope.

These views share existing runtime identities. They never turn generated
reflections into execution receipts or create a second experience store.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .runtime_store import redact_sensitive


TERMINAL_ACTIVITIES = frozenset({"completed", "aborted", "interrupted"})


def _source_description(node):
    payload = node.get('source_payload') or {}
    if payload.get('bookshelf_snapshot') or payload.get('reading_snapshot'):
        from memory_v2.experience_material import node_material
        from .bookshelf_actions import material_description, with_finished_picture
        description = material_description(node_material(payload) or {})
        if description:
            return with_finished_picture(description, node)
    if payload.get('interaction_version') == 1:
        from xhs_interactions.context import brief
        return brief(payload, include_feeling=False, reference=False)
    return node.get('source_summary') or ''


def execution_receipt(node: dict[str, Any], *, activity_type: str = "") -> dict[str, Any]:
    payload = node.get("source_payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    receipt = {key: node.get(key) for key in (
        "node_id", "state", "execution_status", "completion_signal", "completed_at"
    )}
    if payload.get("game"):
        from .game_results import completed, result_fact
        before, outcome, params = (payload.get(key) or {} for key in ("before", "outcome", "action_params"))
        before, outcome, params = (value if isinstance(value, dict) else {} for value in (before, outcome, params))
        receipt.update(
            game=payload.get("game"), playthrough_key=payload.get("playthrough_key"),
            action=payload.get("action"),
            chapter_before=(before.get("chapter") or {}).get("number"),
            chapter_after=(outcome.get("chapter") or {}).get("number"),
            chosen_label=params.get("label") if payload.get("action") == "play_step" else None,
            game_complete=(completed(payload)
                           if node.get("execution_status") == "succeeded" else None),
        )
        if node.get('execution_status') == 'succeeded' and (game_result := result_fact(payload)):
            receipt['game_result'] = game_result
    elif payload.get("netease_song_id") or (activity_type == "listen_music" and payload.get("fingerprint")):
        from .music_receipt import music_execution
        receipt.update(song_id=payload.get("netease_song_id"), title=payload.get("title"),
                       artist=payload.get("artist"), fingerprint=payload.get("fingerprint"),
                       music_execution=music_execution(node))
    elif payload.get('checkup_version') == 1:
        receipt.update(checkup_version=1, conclusion=payload.get('conclusion'),
                       **{key: payload[key] for key in ('source_status', 'goal_assessment') if key in payload},
                       checkup_items=payload.get('checkup_items') or [],
                       round_index=node.get('round_index'), started_at=node.get('started_at'),
                       observed_at=payload.get('observed_at'),
                       intent=payload.get('intent') or '', selection_errors=payload.get('selection_errors') or [])
    return receipt


def execution_overview(activity: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Small, durable event-card facts; scene text stays in the node store."""
    receipts = [execution_receipt(node, activity_type=activity.get("activity_type", "")) for node in nodes]
    confirmed = [row for row in receipts if row.get("execution_status") == "succeeded"]
    result = {
        "activity_id": activity.get("activity_id"), "run_id": activity.get("run_id"),
        "state": activity.get("state"),
        "execution_status_counts": dict(Counter(str(row.get("execution_status") or "unknown") for row in receipts)),
        "completion_signals": dict(Counter(row["completion_signal"] for row in confirmed if row.get("completion_signal"))),
    }
    from .stop_reason import stop_reason_text
    from .node_retry import receipt_text
    retries = [text for node in nodes if (text := receipt_text(node.get('source_payload') or {}))]
    if retries:
        result['immediate_retries'] = retries
    if stop_reason := stop_reason_text(activity, nodes):
        result['stop_reason'] = stop_reason
    games = [row for row in confirmed if row.get("game")]
    if games:
        last = games[-1]
        result["game_progress"] = {
            "game": last["game"], "playthrough_key": last.get("playthrough_key"),
            "actions": dict(Counter(row["action"] for row in games if row.get("action"))),
            "latest_chapter": last.get("chapter_after") or last.get("chapter_before"),
            "game_complete": any(row.get("game_complete") is True for row in games),
            "results": [row['game_result'] for row in games if row.get('game_result')],
        }
    tracks = [row for row in receipts if (row.get('song_id') or row.get('fingerprint')) and (
        row.get('execution_status') == 'succeeded' or (row.get('music_execution') or {}).get('started_at'))]
    if tracks:
        result["music_materials"] = [{key: row.get(key) for key in (
            "node_id", "song_id", "fingerprint", "title", "artist", "completion_signal", "completed_at", "music_execution"
        )} for row in tracks]
    observations = [row for row in receipts if row.get('checkup_version') == 1]
    if observations:
        # Unknown/partial observations are facts about the attempt too. Keep
        # the no-image source receipts even when the node naturally skipped.
        result['checkup_observations'] = observations
    from .xhs_request import activity_progress
    phone_task = activity_progress(activity, nodes)
    if phone_task:
        result['phone_task'] = phone_task
    from .memory_fetch_result import description
    attempts = [description(node['source_payload']) for node in nodes
                if (node.get('source_payload') or {}).get('memory_fetch_version') == 1
                and node['source_payload'].get('outcome') != 'found']
    if attempts:
        result['memory_lookup_attempts'] = attempts
    return redact_sensitive(result)


def activity_experience(activity: dict[str, Any], nodes: list[dict[str, Any]],
                        retained: dict[str, Any] | None = None, *,
                        include_settlement: bool = False,
                        include_node_reflections: bool = False) -> dict[str, Any]:
    """Project an activity without promoting its generated summary to a fact."""
    retained = retained or {}
    successful = [node for node in nodes if node.get("execution_status") == "succeeded"]
    accounts = [node for node in nodes if node.get('reflection') and (
        node.get('execution_status') == 'succeeded'
        or (node.get('side_effect_refs') or {}).get('reflection_source') == 'k_node_review')]
    from .checkup_experience import material as checkup_material
    def independent_reflection(node):
        checkup = checkup_material(node)
        return not checkup or not checkup['reflection_is_fact_projection']
    result = {
        "activity": {key: activity.get(key) for key in (
            "activity_id", "run_id", "activity_type", "state", "created_at", "ended_at"
        )},
        "execution_overview": execution_overview(activity, nodes),
        "subjective_experience": {
            "retained_impression": str(retained.get("impression") or ""),
            "source_node_ids": list(retained.get("source_node_ids") or []),
        },
    }
    # Other domains have useful material descriptions (e.g. bookmarks/news).
    # Game scene prose stays in the game window; music identity is above.
    if activity.get("activity_type") not in {"play_game", "listen_music"}:
        result["source_material_descriptions"] = [
            {"node_id": node.get("node_id"), "description": _source_description(node)}
            for node in successful if (node.get('source_payload') or {}).get('checkup_version') != 1
        ]
    subjective = result["subjective_experience"]
    if retained.get('purpose_outcome') is not None:
        subjective['purpose_outcome'] = retained['purpose_outcome']
    if activity.get("next_inclination_note"):
        subjective["continuation_thought_at_settlement"] = {
            "note": activity["next_inclination_note"],
            "recorded_at": activity.get("ended_at"),
            "continue_next": activity.get("continue_next"),
        }
    if include_node_reflections:
        subjective["node_reflections"] = [
            {"node_id": node.get("node_id"), "reflection": node.get("reflection")}
            for node in accounts if independent_reflection(node)
        ]
    elif not retained.get("recorded", bool(retained.get("impression"))):
        # A legacy record has no chosen-impression contract. Keep a sourced
        # historical feeling, rather than silently inventing or deleting one.
        subjective["legacy_node_reflections"] = [
            {"node_id": node.get("node_id"), "reflection": node.get("reflection")}
            for node in accounts if independent_reflection(node)
        ][-1:]
    if include_settlement:
        receipts = [execution_receipt(node, activity_type=activity.get("activity_type", "")) for node in nodes]
        # The complete checkup observation is already above, with this exact
        # node identity. Keep the execution index, not a second copy of its
        # source prose, in the final-share input.
        result["execution_receipts"] = [
            ({**{key: row[key] for key in ('node_id', 'state', 'execution_status', 'completion_signal', 'completed_at')},
              'observation_ref': row['node_id']} if row.get('checkup_version') == 1 else row)
            for row in receipts]
        settlement = activity.get("summary") or ""
        if activity.get('activity_type') == 'user_tracking' and any(checkup_material(node) for node in nodes):
            from .checkup_experience import independent_settlement
            settlement = independent_settlement(activity, nodes)
        if activity.get("activity_type") == "play_game":
            from .game_experience import game_fallback_summary, is_game_fallback_summary
            if is_game_fallback_summary(settlement):
                settlement = game_fallback_summary(activity, nodes)
        subjective["settlement_reflection"] = settlement
    return redact_sensitive(result)


def activity_history_scope(store: Any, activity: dict[str, Any]) -> dict[str, Any]:
    """Pair every excluded predecessor card with its runtime experience view."""
    current_id = str(activity["activity_id"])
    previous = [item for item in store.list_activities(activity["run_id"])
                if int(item.get("order_index") or 0) < int(activity.get("order_index") or 0)
                and item.get("state") in TERMINAL_ACTIVITIES]
    previous.sort(key=lambda item: int(item.get("order_index") or 0))
    experiences = [activity_experience(
        item, store.list_nodes(item["activity_id"]), store.get_settlement_impression(item["activity_id"]),
        include_node_reflections=True,
    ) for item in previous]
    for item, experience in zip(previous, experiences):
        attach_continuation_disposition(store, item, experience)
    from .experience_presentation import runtime_text
    text = ("本事件段中先于当前活动发生的经历；执行回执与 AI 当时的主观感受分别记录：\n"
            + '\n\n'.join('[' + str(item.get('ended_at') or item.get('created_at') or '时间未记录')
                + '｜' + str(item.get('activity_type')) + '｜来源=' + str(item.get('activity_id')) + ']\n' + runtime_text(experience)
                for item, experience in zip(previous, experiences))) if experiences else "本事件段尚无先前活动。"
    return {
        "exclude_wander_activity_ids": [item["activity_id"] for item in previous] + [current_id],
        "wander_predecessor_text": text,
        "provided_wander_node_reflections": {
            node["node_id"]: node["reflection"]
            for experience in experiences
            for node in experience["subjective_experience"].get("node_reflections", ())
        },
    }


def attach_continuation_disposition(store, activity, experience):
    """Record what followed a thought, without deciding it was fulfilled."""
    thought = experience.get('subjective_experience', {}).get('continuation_thought_at_settlement')
    if not thought:
        return
    later = [item for item in store.list_activities(activity['run_id'])
             if int(item.get('order_index') or 0) > int(activity.get('order_index') or 0)]
    if later:
        item = min(later, key=lambda row: int(row.get('order_index') or 0))
        thought['disposition'] = {'kind': 'same_run_successor', 'activity_type': item['activity_type'],
                                   'state': item['state'], 'due': item.get('next_node_at'),
                                   'meaning': '真实后继记录，不表示旧感受已消失或意图已满足'}
    else:
        note = store.get_pending_inclination(activity['activity_id'])
        thought['disposition'] = {'kind': 'planning_note', 'status': note['status'] if note else 'not_handed_off',
                                   'meaning': 'consumed仅表示规划已考虑，不代表愿望已完成'}
