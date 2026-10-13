"""Freeze completed local activities as day evidence, including unshared ones."""


def capture_activity(wander, activity_id):
    life = wander.lifecycle
    if not life or not life.days.active():
        return
    activity = wander.store.get_activity(activity_id)
    if not activity or activity['state'] not in {'completed', 'interrupted', 'aborted'}:
        return
    day = life.days.active()['date']
    event_id = 'wander_activity_' + activity_id
    if any(e['id'] == event_id for e in life.days.events(day)):
        return
    delivery = wander.store.get_delivery_for_activity(activity_id)
    payload = {'activity_id': activity_id, 'event_type': activity['activity_type'],
        'state': activity['state'], 'summary': activity.get('summary', ''),
        'delivery': delivery['status'] if delivery else 'not_created',
        'identity_basis': '已结算的内部活动；未分享的内容不是聊天发言',
        'nodes': [{k: node.get(k) for k in ('node_id','execution_status','source_summary','reflection','started_at','completed_at')}
                  for node in wander.store.list_nodes(activity_id)]}
    life.days.register_event(event_id, 'wander', day, activity.get('ended_at') or activity['created_at'], payload)


def capture_open_day(wander):
    if not wander.lifecycle or not wander.lifecycle.days.active():
        return
    from datetime import datetime, date, timedelta
    start = date.fromisoformat(wander.lifecycle.days.active()['date'])
    end = datetime.now().date()
    while start <= end:
        for activity in wander.store.list_activities_created_on(start.isoformat()):
            capture_activity(wander, activity['activity_id'])
        start += timedelta(days=1)
