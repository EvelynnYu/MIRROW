"""Bind a previously acquired post to the existing comment submission ledger."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from xhs_publish.store import ROOT, account_config, now
from .store import digest


def source_locator(node_id, session_id, *, path=None):
    path=Path(path) if path else ROOT/'events'/'wander_runtime.db'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT n.source_payload,n.execution_status,a.activity_type FROM wander_nodes n '
            'JOIN wander_activities a ON a.activity_id=n.activity_id JOIN wander_runs r ON r.run_id=a.run_id '
            'WHERE n.node_id=? AND r.session_id=?',(node_id,session_id)).fetchone()
    if not row or row[1]!='succeeded' or row[2]!='browse_xiaohongshu':
        raise ValueError('comment_requires_read_post')
    payload=json.loads(row[0] or '{}')
    if payload.get('status')!='success':raise ValueError('comment_requires_read_post')
    locator=payload.get('comment_locator')
    if not locator and payload.get('profile_id') and (payload.get('task_spec') or {}).get('operation')=='profile':
        locator={'version':1,'profile_id':payload['profile_id'],'note_title':payload.get('title'),
                 'note_body':payload.get('article_text'),'note_author':payload['task_spec'].get('query')}
    if not isinstance(locator,dict) or any(not isinstance(locator.get(k),str) or not locator[k].strip()
            for k in ('profile_id','note_title','note_body','note_author')):
        raise ValueError('post_reopen_identity_unavailable')
    return locator


async def bind_post(*,node_id,session_id,store,adapter,source_key='',path=None):
    locator=source_locator(node_id,session_id,path=path)
    account=account_config()
    from .post_identity import source_ref,author_name
    ref=source_ref(locator)
    incoming={'kind':'post','source_ref':ref,'actor_name':author_name(locator['note_author']),
              'notification_text':locator['note_title'],'observed_at':now(),
              'post_locator':locator,'source_node_id':node_id}
    item=store.ingest(session_id,account,incoming)
    previous=store.reply(digest([session_id,item['id'],source_key]),session_id) if source_key else None
    if not previous:
        claimed=store.project(item).get('existing_reply')
        previous=store.reply(claimed['id'],session_id) if claimed else None
    # Preserve unknown/publication evidence even when the phone is now offline,
    # the editor remains open, or the original post was removed/changed.
    if previous:return item,previous['id']
    with await adapter.device.lease_async('xhs_post_comment_read'):
        target=await adapter.open(item)
        return store.bind_read(item['id'],session_id,target),''


async def apply_review(runtime_store,node_id,review,activity):
    """The persisted AI review owns wording; the shared ledger owns submission."""
    wording=getattr(review,'comment_text','')
    if not wording or not activity or activity.get('activity_type')!='browse_xiaohongshu':return
    current=runtime_store.get_activity(activity['activity_id'])
    if not current or current.get('interrupt_requested'):return
    node=runtime_store.get_node(node_id)
    if (node.get('side_effect_refs') or {}).get('xhs_comment_result'):return
    run=runtime_store.get_run(activity['run_id'])
    source_key='wander-comment:'+node_id
    runtime_store.merge_node_side_effect_refs(node_id,{'xhs_comment_source_key':source_key})
    from .service import request
    from .context import capture
    try:
        value=await request(session_id=run['session_id'],source_key=source_key,action='comment',
                            node_id=node_id,text=wording)
        value={'status':value['status'],'xhs_reply':value.get('xhs_reply'),
               'context':capture(value)}
    except Exception as exc:
        value={'status':'blocked','error':str(exc) if isinstance(exc,ValueError) else 'comment_unavailable'}
    runtime_store.merge_node_side_effect_refs(node_id,{'xhs_comment_result':value})
