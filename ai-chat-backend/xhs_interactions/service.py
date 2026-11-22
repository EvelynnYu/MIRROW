"""Shared app reads and exact, durably claimed comment replies."""
import hashlib

from xhs_publish.store import account_config
from .store import InteractionStore, material, reply_receipt, validate_target


class InteractionBlocked(RuntimeError):
    pass


def adapter_default():
    from .native import NativeInbox
    return NativeInbox()


async def scan(*, session_id, store=None, adapter=None):
    if not session_id:
        raise ValueError('session_id_required')
    store, adapter = store or InteractionStore(), adapter or adapter_default()
    account = account_config()
    with await adapter.device.lease_async('xhs_inbox_scan'):
        observed = await adapter.scan(account)
    if observed.get('account') != account or observed.get('account_verified') is not True:
        raise ValueError('interaction_account_unverified')
    items = [store.ingest(session_id, account, item) for item in observed.get('items', [])]
    return {'status': 'observed', 'account': account,
            'coverage': observed.get('coverage', 'visible_notifications_only'),
            'observed_at': observed.get('observed_at', ''),
            'items': [store.project(item) for item in items]}


async def read(*, session_id, interaction_id, store=None, adapter=None):
    store, adapter = store or InteractionStore(), adapter or adapter_default()
    item = store.get(interaction_id, session_id)
    if not item:
        raise ValueError('interaction_not_found')
    if item['account'] != account_config():
        raise ValueError('bound_account_changed')
    from . import images
    captured = None
    with await adapter.device.lease_async('xhs_interaction_read'):
        target = await adapter.open(item)
        if item.get('target'):
            if not same_target(target, item['target']):
                raise InteractionBlocked('read_target_changed')
            # Thread visibility can grow after replies; old acquisition is frozen.
            target = item['target']
        item = store.bind_read(interaction_id, session_id, target)
        previous = images.existing(store, item, target)
        if previous is None and target.get('image_count', 0):
            if hasattr(adapter, 'capture_images'):
                captured = await adapter.capture_images(target)
            else:
                captured = {'status':'image_capture_unavailable','frames':[]}
    reading = (images.project(previous) if previous is not None else
               await images.observe(store, item, target, captured) if captured is not None else None)
    value = store.project(item)
    if reading is not None:
        value['image_reading'] = reading
    return {'status': 'read', 'interaction': value}


def same_target(observed, frozen):
    fields = ('identity', 'identity_basis', 'source_ref', 'actor_name', 'comment_text',
              'note_title', 'note_author', 'note_body', 'note_body_scope', 'actor_account_id')
    return all(observed.get(k) == frozen.get(k) for k in fields)


def verified_reply(evidence, item):
    return (isinstance(evidence, dict) and evidence.get('verified') is True
            and evidence.get('target_identity') == item['target']['identity']
            and evidence.get('account_id') == item['account']['account_id']
            and evidence.get('text_sha256') == hashlib.sha256(item['text'].encode()).hexdigest())


async def request(*, session_id, source_key='', action, interaction_id='', text='',
                  reply_id='', store=None, adapter=None):
    if not session_id:
        raise ValueError('session_id_required')
    if action not in {'scan', 'read', 'reply', 'preview', 'status', 'reconcile'}:
        raise ValueError('invalid_interaction_action')
    store = store or InteractionStore()
    if action == 'scan':
        return await scan(session_id=session_id, store=store, adapter=adapter)
    if action == 'read':
        return await read(session_id=session_id, interaction_id=interaction_id, store=store, adapter=adapter)
    if reply_id:
        item = store.reply(reply_id, session_id)
        if not item:
            raise ValueError('reply_not_found')
        if text and text != item['text']:
            raise ValueError('frozen_reply_changed')
        if interaction_id and interaction_id != item['interaction_id']:
            raise ValueError('reply_interaction_changed')
    else:
        if action in {'status', 'reconcile'}:
            raise ValueError('reply_id_required')
        item = store.draft_reply(interaction_id, session_id, text, source_key)
    if action == 'status':
        return {'status': item['state'], 'xhs_reply': reply_receipt(item)}
    if item['account'] != account_config():
        raise ValueError('bound_account_changed')
    if item['state'] == 'published':
        return {'status': item['state'], 'xhs_reply': reply_receipt(item)}
    adapter = adapter or adapter_default()
    if action == 'reconcile':
        if item['state'] in {'submitting', 'submitted_unverified'}:
            with await adapter.device.lease_async('xhs_reply_reconcile'):
                evidence = await adapter.verify_reply(item)
                if verified_reply(evidence, item):
                    item = store.transition(item['id'], session_id, {item['state']}, 'published',
                                            receipt=evidence, error='')
        return {'status': item['state'], 'xhs_reply': reply_receipt(item)}
    # A cancelled prepare or an unknown submit cannot be replayed by a fresh turn.
    if item['state'] in {'preparing', 'submitting', 'submitted_unverified'}:
        return {'status': item['state'], 'xhs_reply': reply_receipt(item)}
    with await adapter.device.lease_async('xhs_comment_reply'):
        item = store.transition(item['id'], session_id, {item['state']}, 'preparing', error='')
        try:
            prepared = await adapter.prepare_reply(item)
            incoming = store.get(item['interaction_id'], session_id)
            target = validate_target(prepared.get('target'),
                                     {**incoming['incoming'], 'source_ref': incoming['source_ref']})
            if not same_target(target, item['target']):
                raise InteractionBlocked('reply_target_changed')
            if (prepared.get('account') != item['account'] or prepared.get('text_verified') is not True
                    or prepared.get('text_sha256') != hashlib.sha256(item['text'].encode()).hexdigest()
                    or prepared.get('existing_reply') is not False):
                raise InteractionBlocked('reply_preview_unverified')
            item = store.transition(item['id'], session_id, {'preparing'}, 'previewed',
                                    receipt={'target_verified': True, 'text_verified': True})
            if action == 'preview':
                await adapter.leave_reply_preview(item)
            else:
                item = store.transition(item['id'], session_id, {'previewed'}, 'submitting')
                await adapter.submit_reply(item)
                item = store.transition(item['id'], session_id, {'submitting'}, 'submitted_unverified')
                evidence = await adapter.verify_reply(item)
                if verified_reply(evidence, item):
                    item = store.transition(item['id'], session_id, {'submitted_unverified'},
                                            'published', receipt=evidence)
        except Exception as exc:
            current = store.reply(item['id'], session_id)
            state = ('submitted_unverified' if current['state'] in {'submitting', 'submitted_unverified'}
                     else 'blocked')
            # Only fixed adapter rejection codes are public, never raw shell/UI errors.
            code = str(exc) if isinstance(exc, InteractionBlocked) else 'interaction_execution_error'
            item = store.transition(item['id'], session_id, {current['state']}, state, error=code)
    return {'status': item['state'], 'xhs_reply': reply_receipt(item)}


def experience(value):
    if not isinstance(value, dict):
        return ''
    reply = value.get('xhs_reply')
    if isinstance(reply, dict):
        state = reply.get('state', 'unknown')
        mehuman = {'published': '回复已在平台核验', 'previewed': '只完成回复预览',
                   'submitting': '回复提交结果未知', 'submitted_unverified': '已尝试回复，平台结果未核验',
                   'blocked': '回复准备受阻', 'preparing': '回复准备尚未确认', 'draft': '回复草稿'}
        target = reply.get('target', {})
        return ('小红书互动记录：' + mehuman.get(state, '回复状态未知')
                + '\n回复任务：' + str(reply.get('id', ''))
                + '\n原帖：' + str(target.get('note_title', ''))
                + '\n原评论作者：' + str(target.get('actor_name', ''))
                + '\n原评论：' + str(target.get('comment_text', ''))
                + '\nAI写的回复：' + str(reply.get('text', '')))
    item = value.get('interaction')
    if isinstance(item, dict) and item.get('target'):
        import json
        return '小红书互动实际读取材料：\n' + json.dumps(item, ensure_ascii=False)
    return ''
