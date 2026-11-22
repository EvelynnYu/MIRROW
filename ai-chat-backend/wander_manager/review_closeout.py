"""Reuse a closing review as settlement, without another generation.

The controller marks the exact closing node in its existing transaction. This
module only projects its saved decision; execution and Affect still commit via
the normal settlement path.
"""
from dataclasses import asdict
from datetime import timedelta

from .runtime_models import DecisionPhase, NodeState, SettlementReason
from .timing_policy import normalize_delay_seconds


def normalize_closeout(raw, *, reflection, effect, activity, nodes, anchor):
    if not isinstance(raw, dict):
        return None
    from .runtime_decision_adapters import SettlementAdapter, SettlementDecision
    from memory_v2.experience_contract import impression_text
    eligible = {str(n['node_id']) for n in nodes if n.get('execution_status') == 'succeeded'}
    xhs = {str(n['node_id']) for n in nodes if str(n['node_id']) in eligible
           and (n.get('source_payload') or {}).get('status') == 'success'} if activity['activity_type'] == 'browse_xiaohongshu' else set()
    value = SettlementAdapter._normalize_settlement(raw, SettlementDecision(summary=reflection), xhs)
    if 'emotion_effect' not in raw:
        value.emotion_effect = effect
    value.retained_impression = impression_text(value.retained_impression, node_ids=eligible)
    if not set(value.retained_node_ids) <= eligible or (value.retained_impression and not eligible):
        value.retained_impression, value.retained_node_ids = '', []
    delay, source = normalize_delay_seconds(value.next_run_delay_seconds, now=anchor)
    if source == 'fallback':
        value.timing_source, value.next_run_delay_seconds = source, delay
    value.next_plan_at = (anchor + timedelta(seconds=delay)).isoformat()
    return value


def restore_closeout(raw):
    if not isinstance(raw, dict) or not raw:
        return None
    from .runtime_decision_adapters import SettlementDecision, _normalize_effect
    fields = dict(raw)
    fields['emotion_effect'] = _normalize_effect(fields.get('emotion_effect'))
    return SettlementDecision(**{k: v for k, v in fields.items() if k in SettlementDecision.__dataclass_fields__})


def from_closing_review(store, activity_id, reason):
    """A deterministic receipt, not a second model call or a new reflection."""
    if reason in (SettlementReason.USER_INTERRUPT, SettlementReason.EXECUTION_ERROR, SettlementReason.GAME_STOP):
        return None
    activity = store.get_activity(activity_id)
    nodes = store.list_nodes(activity_id)
    last = nodes[-1] if nodes else {}
    run = store.get_run(activity['run_id']) or {}
    for record in reversed(store.list_decisions(activity['run_id'])):
        if record.get('activity_id') != activity_id or record.get('status') != 'ok':
            continue
        saved = (record.get('parsed_output') or {}).get('normalized') or {}
        if record.get('phase') == DecisionPhase.TIMING_REVIEW.value:
            if (reason != SettlementReason.PLAN_HORIZON or saved.get('continue_activity') is not False
                    or not saved.get('expected_horizon') or saved['expected_horizon'] != run.get('next_wake_at')):
                continue
        elif record.get('phase') in (DecisionPhase.NODE_REVIEW.value, DecisionPhase.SELF_REFLECTION.value):
            if (record.get('node_id') != last.get('node_id') or last.get('state') != NodeState.COMPLETED.value
                    or not (last.get('side_effect_refs') or {}).get('closeout_from_review')):
                continue
        else:
            continue
        decision = restore_closeout(saved.get('closeout'))
        if decision is None:
            continue
        if activity['activity_type'] == 'play_game' and any(n.get('execution_status') == 'succeeded' for n in nodes):
            decision.share = True  # Existing agreement: share confirmed game progress.
        from .settlement_outcome import constrain_unexecuted, unexecuted
        constrain_unexecuted(decision, activity, nodes, reason)
        if any(a['order_index'] > activity['order_index'] and a['state'] == 'planned'
               for a in store.list_activities(activity['run_id'])):
            decision.next_run_delay_seconds, decision.next_plan_at = None, ''
            decision.timing_source, decision.timing_reason = 'successor', ''
        store.record_decision(run_id=activity['run_id'], activity_id=activity_id,
            phase=DecisionPhase.SETTLEMENT, model='', recipe='WANDER_ACTIVITY',
            status='ok_from_review', input_context={'source_decision_id': record['decision_id']},
            parsed_output={'normalized': {**asdict(decision), 'closeout_version': 1,
                                          'unexecuted': unexecuted(activity, nodes, reason)}, 'source_decision_id': record['decision_id'],
                           'source_phase': record['phase']})
        return decision
    return None
