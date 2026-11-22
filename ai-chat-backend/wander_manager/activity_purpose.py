"""AI's recorded purpose updates, distinct from execution and scheduling."""


def normalize_outcome(value):
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str)
                                              for key in ('obtained', 'remaining')):
        return None
    return {key: value[key].strip() for key in ('obtained', 'remaining')}


def outcome_text(value):
    outcome = normalize_outcome(value)
    if outcome is None:
        return ''
    from context_builder.text_utils import _redact_tool_fact
    return ('AI 当时对本轮的判断（主观原述）：\n'
            '已经得到：' + _redact_tool_fact(outcome['obtained'] or '未记下具体所得') + '\n'
            '仍想接续：' + _redact_tool_fact(outcome['remaining'] or '当时没有留下接续事项'))


def current_progress(store, activity, run=None):
    """Only applied reviews enter continuity; saved but uncommitted replies do not."""
    from .goal_policy import get_activity_goal_policy
    run = run or store.get_run(activity['run_id']) or {}
    timing = None
    for item in store.list_purpose_reviews(activity['activity_id']):
        value = (item.get('parsed_output') or {}).get('normalized') or {}
        phase = item.get('phase')
        if phase == 'node_review':
            result = dict(source_decision_id=item['decision_id'], source_node_id=item['node_id'],
                        recorded_at=item['created_at'],
                        purpose_outcome=normalize_outcome(value.get('purpose_outcome')),
                        continuation_reason=(value.get('goal_reason') or '')
                        if value.get('continue_activity') else '')
            if timing:
                result.update(continuation_reason=timing['reason'], timing_review=timing)
            return result
        if (phase == 'timing_review' and value.get('continue_activity')
                and value.get('new_horizon') and value['new_horizon'] == run.get('next_wake_at')
                and timing is None):
            timing = dict(source_decision_id=item['decision_id'], recorded_at=item['created_at'],
                          reason=value.get('reason') or '')
    if timing:
        return dict(continuation_reason=timing['reason'], timing_review=timing)
    # Older applied extensions kept the public continuation reason in the run.
    policy = get_activity_goal_policy(run, activity)
    return ({'continuation_reason': policy['reason'], 'source': 'activity_goal_policy'}
            if policy.get('reason') else {})


def recall_query(activity_type, initial_reason, progress, nodes):
    """Current purpose and acquired material guide recall, not initial intent alone."""
    latest = next((node for node in reversed(nodes) if node.get('execution_status') == 'succeeded'), {})
    return '\n'.join(part for part in (
        activity_type, progress.get('continuation_reason') or initial_reason,
        outcome_text(progress.get('purpose_outcome')), latest.get('source_summary') or '',
    ) if part)
