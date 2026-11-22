"""Task-local handoff from a persisted activity to its internal decisions."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json

_current = ContextVar('wander_activity_context', default=None)


def current():
    return _current.get()


@contextmanager
def bind(context):
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def snapshot(store, activity, nodes):
    from .context_projection import activity_history_scope, activity_experience
    from .activity_purpose import current_progress, recall_query
    scope = activity_history_scope(store, activity)
    progress = current_progress(store, activity)
    return {
        'activity_id': activity['activity_id'], 'activity_type': activity['activity_type'],
        'activity_reason': activity.get('reason') or '',
        'activity_created_at': activity.get('created_at') or '',
        'purpose_progress': progress,
        'context_query': recall_query(activity['activity_type'], activity.get('reason') or '', progress, nodes),
        'earlier_experiences': scope['wander_predecessor_text'],
        'exclude_wander_activity_ids': scope['exclude_wander_activity_ids'],
        'provided_node_reflections': scope['provided_wander_node_reflections'],
        'current_experience': activity_experience(activity, nodes, include_node_reflections=True),
    }


def recipe_kwargs(activity):
    current_experience = activity.get('current_experience') or {}
    provided = dict(activity.get('provided_node_reflections') or {})
    provided.update({node['node_id']: node['reflection'] for node in
        (current_experience.get('subjective_experience') or {}).get('node_reflections', ())})
    runtime = {key: activity.get(key) or '' for key in
               ('activity_id', 'activity_type', 'activity_created_at', 'activity_reason')}
    runtime['current_time'] = datetime.now().astimezone().isoformat(timespec='seconds')
    from .experience_presentation import runtime_text
    text = '[本次活动：身份、起意与当前进展]\n' + json.dumps(runtime, ensure_ascii=False)
    if activity.get('purpose_progress'):
        text += '\n[AI 最近复核的主观进展与续作起意]\n' + json.dumps(activity['purpose_progress'], ensure_ascii=False)
    if current_experience:
        text += '\n' + runtime_text(current_experience)
    if activity.get('music_context_scope'):
        text += '\n[额外选歌事实范围]\n' + json.dumps(activity['music_context_scope'], ensure_ascii=False)
    return dict(
        context_query=activity.get('context_query') or '\n'.join(filter(None, (activity.get('activity_type'), activity.get('activity_reason')))),
        wander_runtime_text=text,
        exclude_wander_activity_ids=activity.get('exclude_wander_activity_ids') or [],
        provided_wander_node_reflections=provided,
        wander_predecessor_text=activity.get('earlier_experiences') or '本事件段尚无先前活动。',
    )


def handoff_text():
    activity = current()
    if activity is None:
        return ''
    kwargs = recipe_kwargs(activity)
    return kwargs['wander_runtime_text'] + '\n\n' + kwargs['wander_predecessor_text']


async def build_context(*, persona, session_id, activity_context=None, recipe='WANDER_ACTIVITY',
                        interlocutor_ids=None):
    from context_builder import ContextBuilder
    kwargs = recipe_kwargs(activity_context or current() or {})
    if interlocutor_ids is not None:
        kwargs['interlocutor_ids'] = interlocutor_ids
    return await ContextBuilder.build(recipe, persona=persona, session_id=session_id,
        **kwargs)
