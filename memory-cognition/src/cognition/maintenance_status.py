"""Read-only view of retired lanes; never rewrite historical outcomes."""


def current_lanes(state):
    result = dict(state)
    lanes = state.get('lanes', {})
    if 'wander' not in lanes:
        return result
    result['historical_status'] = state.get('status')
    result['retired_lanes'] = {'wander': lanes['wander']}
    result['lanes'] = {k: v for k, v in lanes.items() if k != 'wander'}
    result['lane_errors'] = {k: v for k, v in state.get('lane_errors', {}).items() if k != 'wander'}
    current_names = ('world', 'self', 'other', 'scp') if 'self' in lanes or 'other' in lanes else ('world', 'subjective', 'scp')
    if state.get('status') == 'error' and lanes['wander'] == 'error' and all(
        lanes.get(name) in {'completed', 'completed_with_rejections', 'no_evidence', 'not_triggered'}
        for name in current_names
    ):
        result['status'] = ('completed_with_rejections'
                            if any(lanes.get(name) == 'completed_with_rejections'
                                   for name in current_names)
                            else 'completed')
        result.pop('message', None)
    return result
