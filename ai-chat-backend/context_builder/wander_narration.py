"""Adjacent narration only; occurrence IDs, clocks and storage stay separate."""
import json


def calls(row):
    value = row.get('tool_calls') or row.get('toolCalls') or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []



def identity(row):
    if row.get('event_type') != 'wander_activity':
        return None
    first = next(iter(calls(row)), {})
    extra = first.get('extra_data') or {}
    frozen = extra.get('memory_experience') or {}
    overview = extra.get('execution_overview') or {}
    kind = frozen.get('activity_type')
    return {'kind': kind, 'run_id': overview.get('run_id') or ''} if kind else None


def connection(previous, current):
    if not previous or not current:
        return ''
    if previous['kind'] == current['kind']:
        return '接着，又有了一段同类经历：'
    if previous.get('run_id') and previous['run_id'] == current.get('run_id'):
        return '同一段漫想里，接着换了件事：'
    return ''


def narrate(messages):
    """Use after status/chat/event interleaving. Appending never rewrites a prefix."""
    result, previous = [], None
    for message in messages:
        current = message.get('_wander_identity')
        prefix = connection(previous, current)
        item = dict(message)
        if prefix:
            item['content'] = prefix + '\n' + item['content']
        result.append(item)
        previous = current
    return result
