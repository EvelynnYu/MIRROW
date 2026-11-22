"""One semantic compiler and versioned read-only task contract for both brains."""
from __future__ import annotations

from copy import deepcopy
import json


def validate_task(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_xhs_task')
    operation = value.get('operation')
    if operation not in {'recommend', 'search', 'profile', 'continue'}:
        raise ValueError('unsupported_xhs_operation')
    query = value.get('query') or ''
    task_id = value.get('task_id') or ''
    scope = value.get('scope', 'count')
    count = value.get('count', 3)
    goal_count = value.get('goal_count') or count
    if (not isinstance(query, str) or len(query) > 120 or
            not isinstance(task_id, str) or len(task_id) > 80 or
            scope not in {'count', 'all'} or isinstance(count, bool) or
            not isinstance(count, int) or not 1 <= count <= 5 or isinstance(goal_count, bool) or
            not isinstance(goal_count, int) or not 1 <= goal_count <= 500):
        raise ValueError('invalid_xhs_task')
    if operation in {'search', 'profile'} and not query.strip():
        raise ValueError('target_required')
    if scope == 'all' and operation not in {'profile', 'continue'}:
        raise ValueError('all_requires_profile')
    if operation not in {'profile', 'continue'} and goal_count > count:
        raise ValueError('long_count_requires_profile')
    if operation == 'continue' and not task_id:
        raise ValueError('task_id_required')
    return {'version': 1, 'operation': operation, 'query': query.strip(),
            'scope': scope, 'count': count, 'goal_count': goal_count, 'task_id': task_id}


async def compile_task(instruction, *, compiler=None):
    """Compile AI's chosen action; no keyword intent inference or new decisions."""
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        raise ValueError('instruction_required')
    if compiler is None:
        from .flash_structured import call_flash_json_detailed
        compiler = call_flash_json_detailed
    result = await compiler(messages=[
        {'role': 'system', 'content': (
            '把AI已经决定的小红书只读行动编译为JSON，保持原目标和范围。可用operation：'
            'recommend首页、search搜索主题、profile搜索账号并读主页笔记、continue按已有task_id续读。'
            'query原样保留目标账号/主题（含符号）；scope=count或all（只有明确全部主页才用all）；'
            'count是本批篇数1至5默认3，goal_count保留要求的总篇数（未指定则等于count），超过5篇仅支持主页续读。'
            'continue须原样提取回执的task_id。输出{operation,query,scope,count,goal_count,task_id}。'
            '不支持发布、评论、下单、视频完整播放或指定链接直读；'
            '这些请求输出{"operation":"unsupported"}，不降级成搜索。')},
        {'role': 'user', 'content': instruction}], temperature=0, max_tokens=700,
        enable_thinking=False, route_label='xhs_action_compiler')
    if result.status != 'ok':
        raise ValueError('xhs_compile_failed')
    return validate_task(result.parsed)


def latest_cursor(nodes, initial=None):
    cursor = deepcopy(initial or {})
    for node in nodes:
        payload = node.get('source_payload') or {}
        if node.get('execution_status') == 'succeeded' and isinstance(payload.get('cursor_after'), dict):
            cursor = deepcopy(payload['cursor_after'])
    return cursor


def resolve_profile_resume(store, session_id, task_id):
    """Resolve the same committed task lineage from either execution entrance."""
    activity = store.get_activity(task_id)
    run = store.get_run(activity['run_id']) if activity else None
    def valid(candidate, owner):
        return (owner and owner.get('session_id') == session_id and candidate and
                candidate.get('activity_type') == 'browse_xiaohongshu')
    if not valid(activity, run):
        raise ValueError('task_not_found_in_session')
    for _ in range(200):
        snapshot = run.get('context_snapshot') or {}
        successor = (snapshot.get('phone_task_successors') or {}).get(activity['activity_id'])
        if not successor and len(snapshot.get('phone_tasks') or {}) == 1:
            successor = snapshot.get('phone_task_successor')
        if not successor:
            break
        activity = store.get_activity(successor)
        run = store.get_run(activity['run_id']) if activity else None
        if not valid(activity, run):
            raise ValueError('task_lineage_incomplete')
    else:
        raise ValueError('task_lineage_limit')
    record = (run.get('context_snapshot') or {}).get('phone_tasks', {}).get(activity['activity_id'])
    if not record:
        raise ValueError('task_spec_missing')
    spec = validate_task(record['spec'])
    nodes = store.list_nodes(activity['activity_id'])
    cursor = latest_cursor(nodes, record.get('initial_cursor'))
    end = any((node.get('source_payload') or {}).get('status') == 'profile_end_observed' for node in nodes)
    if (spec['operation'] != 'profile' or end or
            (spec['scope'] == 'count' and cursor.get('read_count', 0) >= spec['goal_count'])):
        raise ValueError('task_not_resumable')
    return run, activity, spec, cursor


def progress_for_k(task_id, spec, cursor):
    """Objective continuation facts, shared by live review and frozen events."""
    if not task_id or not isinstance(spec, dict) or spec.get('operation') != 'profile':
        return None
    cursor = cursor if isinstance(cursor, dict) else {}
    return {'task_id': str(task_id), 'query': str(spec.get('query') or ''),
            'scope': spec.get('scope', 'count'), 'goal_count': spec.get('goal_count'),
            'acquired_count': cursor.get('read_count', 0),
            'listing_coverage': cursor.get('listing', 'partial')}


def activity_progress(activity, nodes):
    if activity.get('activity_type') != 'browse_xiaohongshu':
        return None
    progress = None
    for node in nodes:
        payload = node.get('source_payload') or {}
        if node.get('execution_status') == 'succeeded' and payload.get('material_version') == 1:
            progress = progress_for_k(activity.get('activity_id'), payload.get('task_spec'),
                                      payload.get('cursor_after'))
        elif progress and payload.get('status') == 'profile_end_observed':
            progress['listing_coverage'] = 'explicit_end'
    return progress


def material_for_k(payload):
    """Exactly the same bounded article material at foreground/background delivery."""
    result = {key: deepcopy(payload[key]) for key in (
        'title', 'source_id', 'source_provider', 'retrieved_at', 'article_text',
        'image_observations', 'coverage', 'limitations', 'content_summary') if key in payload}
    if 'image_observations' in result:
        result['image_observations'] = [{key: item[key] for key in ('page', 'observation', 'omitted_chars') if key in item}
                                        for item in result['image_observations'] if isinstance(item, dict)]
    return result


TASK_ERRORS = {
    'target_required': '这次想看的账号或帖子还缺少可定位的线索；需要具体账号名或已有浏览任务编号。',
    'unsupported_xhs_operation': '这项目标不属于浏览入口支持的操作；评论与艾特使用互动入口，发布使用创作入口。',
    'invalid_xhs_task': '浏览动作参数没有解析完整，本次尚未操作手机。',
    'invalid_profile_target': '主页目标没有解析清楚，本次尚未操作手机。',
    'task_id_required': '续读缺少原浏览任务编号，本次尚未操作手机。',
    'all_requires_profile': '读取全部需要明确主页目标，本次尚未操作手机。',
    'long_count_requires_profile': '跨批读取需要明确主页目标，本次尚未操作手机。',
    'instruction_required': '本次缺少浏览目标，尚未操作手机。',
    'xhs_compile_failed': '浏览动作解析没有取得有效结果，本次尚未操作手机。',
    'xhs_context_unavailable': '本次没有取得同一会话的目标上下文，尚未操作手机。',
    'aning_account_not_configured': '人类伙伴的小红书账号还没有配置，本次尚未操作手机。',
}

OPEN_ERRORS = {
    'no_readable_image_posts': '这次遇到的视频帖已跳过，有限查找内没有找到可读取的图文，未计入已读。',
    'video_return_failed': '跳过视频时未能返回原列表，本次浏览停下了。',
    'feed_return_unconfirmed': '跳过视频后尚未确认回到原列表，本次浏览停下了。',
}
