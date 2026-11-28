"""Bounded foreground batches over the same persistent nodes as Wander."""
from copy import deepcopy
import hashlib

from .event_types import EventType
from .runtime_models import (WanderRun, WanderActivity, RunState, ActivityState,
                             GoalMode, SettlementReason)
from .node_execution_adapter import NodeExecutionStatus
from .xhs_request import compile_task, latest_cursor, material_for_ai, resolve_profile_resume


def run_model(row):
    return WanderRun(**{**row, 'state': RunState(row['state'])})


def recover_receipt(runner, run):
    """Recover committed screen material, closing unknown work without replay."""
    snapshot = run.get('context_snapshot') or {}
    receipt = snapshot.get('chat_receipt')
    if receipt:
        return deepcopy(receipt)
    tasks = snapshot.get('phone_tasks') or {}
    if not tasks:
        return {'status': 'interrupted', 'error': 'task_spec_missing', 'resumable': False}
    task_id, record = next(iter(tasks.items()))
    nodes = runner.store.list_nodes(task_id)
    posts = []
    for node in nodes:
        if node.get('execution_status') == 'succeeded':
            payload = node.get('source_payload') or {}
            posts.append(post_receipt(payload, node['node_id'], len(posts)+1))
    activity = runner.store.get_activity(task_id)
    if activity['state'] not in {'completed', 'aborted', 'interrupted'}:
        runner.controller.settle_activity(task_id, SettlementReason.EXECUTION_ERROR,
            summary='读取批次中断；保留已提交屏幕材料，未重放未知节点。', share_decision=False)
    cursor = latest_cursor(nodes, record.get('initial_cursor'))
    spec = record['spec']
    total_read = cursor.get('read_count', len(posts))
    end_observed = any((node.get('source_payload') or {}).get('status') == 'profile_end_observed'
                       for node in nodes)
    complete = (end_observed if spec['operation'] == 'profile' and spec['scope'] == 'all'
                else total_read >= spec['goal_count'])
    resumable = spec['operation'] == 'profile' and not complete and not end_observed
    receipt = {'status': 'success' if complete else ('partial_success' if posts else 'failed'),
        **({} if complete else {'barrier': 'batch_interrupted'}),
        'run_id': run['run_id'], 'activity_id': task_id, 'task_id': task_id,
        'node_ids': [node['node_id'] for node in nodes], 'posts': posts,
        'success_count': len(posts), 'target_count': record['spec']['count'],
        'resumable': resumable, 'can_continue_now': False,
        'progress': {'observed_articles': total_read},
        'coverage': {'listing': 'explicit_end' if end_observed else 'unproven_end', 'scope': spec['scope']},
        'source': 'chat_tool', 'delivery_status': 'not_requested'}
    current = run_model(runner.store.get_run(run['run_id']))
    current.context_snapshot['chat_receipt'] = deepcopy(receipt)
    runner.store.save_run(current)
    return receipt


def post_receipt(payload, node_id, index):
    return {'index': index, 'node_id': node_id,
        'source_id': payload.get('source_id', ''), 'title': payload.get('title', ''),
        'summary': payload.get('content_summary', ''), 'evidence_excerpt': payload.get('evidence_excerpt', ''),
        'source_provider': payload.get('source_provider', ''), 'source_url': payload.get('source_url', ''),
        'material': material_for_ai(payload)}


async def execute_chat_task(runner, *, session_id, instruction, source_turn_id='', compiler=None):
    if not session_id:
        return {'status': 'invalid_request', 'error': 'session_id_required'}
    # Same delivered action is never replayed after a client retry/restart.
    key = hashlib.sha256((session_id+'\n'+source_turn_id+'\n'+instruction).encode()).hexdigest()
    run_id = 'xhs_chat_'+key if source_turn_id else None
    async with runner._tick_lock:
        previous = runner.store.get_run(run_id) if run_id else None
        if previous:
            return recover_receipt(runner, previous)
        try:
            task = await compile_task(instruction, compiler=compiler)
        except ValueError as exc:
            return {'status': 'invalid_request', 'error': str(exc)}
        cursor = {}
        parent_run = None
        parent_activity_id = ''
        if task['operation'] == 'continue':
            try:
                old_run, activity, task, cursor = resolve_profile_resume(runner.store, session_id, task['task_id'])
            except ValueError as exc:
                return {'status': 'invalid_request', 'error': str(exc)}
            if old_run['state'] not in {'completed', 'aborted', 'interrupted'}:
                if old_run.get('trigger_reason') != 'chat_tool':
                    return {'status': 'invalid_request', 'error': 'task_in_progress'}
                recover_receipt(runner, old_run)
            parent_run = old_run
            parent_activity_id = activity['activity_id']
        target = task['count']
        if task['operation'] == 'profile' and task['scope'] == 'count':
            remaining = task['goal_count']-cursor.get('read_count', 0)
            if remaining <= 0:
                return {'status': 'invalid_request', 'error': 'task_already_complete', 'resumable': False}
            target = min(target, remaining)
        run = WanderRun(trigger_reason='chat_tool', session_id=session_id)
        if run_id:
            run.run_id = run_id
        run.transition_to(RunState.ACTIVE_WAIT)
        activity = WanderActivity(activity_type=EventType.BROWSE_XIAOHONGSHU.value,
            run_id=run.run_id, goal_mode=GoalMode.COUNT, goal_value=target, reason=instruction)
        activity.transition_to(ActivityState.ACTIVE_WAIT)
        activity.next_node_at = runner.clock().isoformat()
        run.context_snapshot['phone_tasks'] = {activity.activity_id: {
            'version': 1, 'spec': task, 'initial_cursor': cursor}}
        runner.store.save_plan(run, [activity])
        runner.store.record_delivery(run_id=run.run_id, activity_id=activity.activity_id,
                                     delivery_type='chat_tool', status='not_requested')
        if parent_run:
            parent = run_model(runner.store.get_run(parent_run['run_id']))
            parent.context_snapshot.setdefault('phone_task_successors', {})[parent_activity_id] = activity.activity_id
            runner.store.save_run(parent)
        posts, node_ids, barrier = [], [], ''
        end_observed = False
        for index in range(target):
            try:
                node = runner.controller.begin_node(activity.activity_id)
                node_ids.append(node.node_id)
                execution = await runner.executor.execute(node.node_id)
            except Exception:
                return recover_receipt(runner, runner.store.get_run(run.run_id))
            payload = execution.payload or {}
            success = execution.status == NodeExecutionStatus.SUCCEEDED
            barrier = '' if success else str(payload.get('status') or execution.error or 'failed')
            end_observed = barrier == 'profile_end_observed'
            # This bookkeeping is not AI's subjective review. His reply follows the actual tool material.
            action = runner.controller.complete_node_review(node.node_id, reflection='',
                continue_activity=success and index+1 < target, abort_reason=barrier,
                execution_failed=not success)
            if not success:
                break
            posts.append(post_receipt(payload, node.node_id, index+1))
            if runner.store.get_activity(activity.activity_id).get('interrupt_requested'):
                barrier = 'user_interrupt'
                break
        runner.controller.settle_activity(activity.activity_id,
            SettlementReason.GOAL_REACHED if len(posts) == target or end_observed else (
                SettlementReason.USER_INTERRUPT if barrier == 'user_interrupt' else SettlementReason.EXECUTION_ERROR),
            summary=f'专用手机本批取得{len(posts)}篇；'+(barrier or '批次结束'), share_decision=False)
        final_cursor = latest_cursor(runner.store.list_nodes(activity.activity_id), cursor)
        profile_all = task['operation'] == 'profile' and task['scope'] == 'all'
        total_read = final_cursor.get('read_count', len(posts))
        complete = end_observed if profile_all else total_read >= task['goal_count']
        resumable = task['operation'] == 'profile' and not complete and not end_observed
        receipt = {'status': 'success' if complete else ('partial_success' if posts else 'failed'),
            'run_id': run.run_id, 'activity_id': activity.activity_id, 'task_id': activity.activity_id,
            'node_ids': node_ids, 'posts': posts, 'target_count': target, 'success_count': len(posts),
            'resumable': resumable, 'source': 'chat_tool', 'delivery_status': 'not_requested',
            'can_continue_now': resumable and not barrier,
            'coverage': {'listing': 'explicit_end' if end_observed else 'unproven_end',
                         'scope': task['scope'], 'articles': 'see_each_post'},
            'progress': {'observed_articles': final_cursor.get('read_count', len(posts)),
                         'identity_kind': 'profile_and_listing_title_not_platform_ids'},
            **({'barrier': barrier} if barrier else {})}
        # The immutable assistant/tool event contains this same material, one experience per entry.
        current = run_model(runner.store.get_run(run.run_id))
        current.context_snapshot['chat_receipt'] = deepcopy(receipt)
        runner.store.save_run(current)
        return receipt
