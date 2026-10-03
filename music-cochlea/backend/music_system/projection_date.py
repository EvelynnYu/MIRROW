"""Resolve an event's occurrence day without borrowing today's topic for history."""
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def _local(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ))


def event_active_date(payload, session_id, chronicle, *, now=None):
    anchor = chronicle.get_message_by_msg_id(str(payload.get('event_id') or ''))
    if anchor and anchor.get('session_id') == session_id and anchor.get('active_date'):
        return anchor['active_date'], 'persisted_event'
    occurred = _local(payload['occurred_at'])
    from scene_manager.scene import get_scene_for_ts
    scene = get_scene_for_ts(payload['occurred_at'], session_id=session_id, chronicle=chronicle)
    if scene and scene.get('active_date'):
        return scene['active_date'], 'persisted_scene'
    topic = chronicle.get_topic_state(session_id) or {}
    if topic.get('is_active') and topic.get('start_time'):
        started = _local(topic['start_time'])
        if started <= occurred <= (now or datetime.now(TZ)):
            return started.date().isoformat(), 'active_topic_at_occurrence'
    return occurred.date().isoformat(), 'occurrence_calendar_fallback'
