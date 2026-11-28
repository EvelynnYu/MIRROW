"""One semantic compiler and versioned read-only task contract for both brains."""
from __future__ import annotations

from copy import deepcopy
import json


TASK_ERRORS = {
    'task_interrupted': '活动已中断，未继续操作手机。',
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
}


def validate_task(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_xhs_task')
    operation = value.get('operation')
    if operation == 'needs_target':
        raise ValueError('target_required')
    if operation not in ('recommend', 'search', 'favorites', 'profile', 'continue'):
        raise ValueError('unsupported_xhs_operation')
    profile_target = value.get('profile_target', 'user')
    if operation in {'recommend','favorites'} and (
            profile_target is None or isinstance(profile_target, str) and not profile_target.strip()):
        profile_target = 'user'
    if profile_target not in ('user', 'self') or (
            profile_target != 'user' and operation != 'profile'):
        raise ValueError('invalid_profile_target')
    query = value.get('query') or ''
    task_id = value.get('task_id') or ''
    scope = value.get('scope', 'count')
    count = value.get('count', 3)
    goal_count = value.get('goal_count') or count
    if (not isinstance(query, str) or len(query) > 120 or
            not isinstance(task_id, str) or len(task_id) > 80 or
            scope not in ('count', 'all') or isinstance(count, bool) or
            not isinstance(count, int) or not 1 <= count <= 5 or isinstance(goal_count, bool) or
            not isinstance(goal_count, int) or not 1 <= goal_count <= 500):
        raise ValueError('invalid_xhs_task')
    if operation in {'search', 'profile'} and not query.strip() and profile_target == 'user':
        raise ValueError('target_required')
    if scope == 'all' and operation not in {'profile', 'continue'}:
        raise ValueError('all_requires_profile')
    if operation not in {'profile', 'continue'} and goal_count > count:
        raise ValueError('long_count_requires_profile')
    if operation == 'continue' and not task_id:
        raise ValueError('task_id_required')
    return {'version': 1, 'operation': operation, 'query': query.strip(),
            'scope': scope, 'count': count, 'goal_count': goal_count, 'task_id': task_id,
            **({'profile_target': profile_target} if profile_target != 'user' else {})}


COMPILE_CONTRACT = (
        '把 AI 已经决定的小红书只读行动编译为JSON，保持原目标和范围。可用operation：'
        'recommend首页、search搜索公开主题、favorites浏览或在 AI 的账号收藏中搜索并读原帖（query可为空）、profile搜索账号并读主页笔记、continue按已有task_id续读。'
        '看 AI 自己的主页用profile并带profile_target=self，由执行器核验绑定账号；'
        '其他账号用profile且query指定账号。query原样保留目标账号/主题（含符号）；'
        'scope=count或all（只有明确全部主页才用all）；'
        'count是本批篇数1至5默认3，goal_count保留要求的总篇数（未指定则等于count），超过5篇仅支持主页续读。'
        'continue须原样提取回执的task_id。输出{operation,query,scope,count,goal_count,task_id}；'
        'profile_target仅在选择自己的主页时附加，首页推荐省略该字段。'
        '结合当前会话中已经明确的账号、帖子和先前浏览任务解析“她”“刚才那篇”等指代，保留 AI 已选择的阅读对象。'
        '一句话里有多个目标时只编译最先要做的那一件，其余留到下一轮；'
        '含读后动作时只编译其阅读目标，包内读完当篇后接着处理。'
        '想看某人的帖子属于profile阅读；她准备分享、发布的背景不等于本次要替她发布。'
        '指定对象缺少可确认的账号或任务线索时输出{"operation":"needs_target"}；不改成首页或话题搜索，也不猜账号。'
        '只是随便逛逛、未指定对象时用recommend。'
        '查收到的艾特或回复属于独立的小红书互动工具，不是关键词搜索。'
        '只有请求本身不含任何浏览目标、且属于明确不支持的请求时（查自己的艾特或回复、发布、下单、视频完整播放、指定链接直读），'
        '才输出{"operation":"unsupported"}，不降级成搜索。')


async def compile_task(instruction, *, compiler=None, context=None, builder=None, history_scope=None,
                       route_inbox=False, correction=''):
    """Compile the AI's chosen action; no keyword intent inference or new decisions."""
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        raise ValueError('instruction_required')
    if compiler is None:
        from .flash_structured import call_flash_json_detailed
        compiler = call_flash_json_detailed
    contract = COMPILE_CONTRACT
    if route_inbox:
        contract += ('\n这次来自漫想，小红书包也可以从收到的通知进入原帖。目标由某条评论或别人给 AI 的 @ 指定时，'
                     '直接输出 {"operation":"received_interaction"}，由互动入口核对原帖、评论与既有回复。'
                     '作者的称呼、帖子的主题都只是辨认通知的线索。只有明确要逛账号主页时才选 profile。')
    from .xhs_task_context import task_messages
    if correction:
        contract += '\n上次的浏览选择：' + correction
    messages = await task_messages(instruction, contract)
    result = await compiler(messages=messages, temperature=0, max_tokens=700,
        enable_thinking=False, route_label='xhs_action_compiler')
    if result.status != 'ok':
        raise ValueError('xhs_compile_failed')
    # A deictic target can genuinely depend on earlier conversation.
    # Resolve only that case in context instead of attaching a life history to navigation.
    if context is not None and isinstance(result.parsed, dict) and result.parsed.get('operation') == 'needs_target':
        messages = await task_messages(instruction, contract, context=context, builder=builder,
                                       history_scope=history_scope, references_only=True)
        result = await compiler(messages=messages, temperature=0, max_tokens=700,
                                enable_thinking=False, route_label='xhs_action_compiler')
        if result.status != 'ok':
            raise ValueError('xhs_compile_failed')
    from . import node_retry
    while True:
        if node_retry.cancelled():
            raise ValueError('task_interrupted')
        if route_inbox and isinstance(result.parsed, dict) and result.parsed.get('operation') == 'received_interaction':
            return {'operation':'received_interaction'}
        try:
            return validate_task(result.parsed)
        except ValueError as exc:
            note = TASK_ERRORS[str(exc)]
            if correction:
                node_retry.unresolved(note)
                raise
            if not node_retry.take('选择小红书浏览目标', note):
                raise
            correction = note
        result = await compiler(messages=[*messages, {'role':'user', 'content':'【浏览选择回执】' + correction}],
            temperature=0, max_tokens=700, enable_thinking=False, route_label='xhs_action_compiler')
        if result.status != 'ok':
            raise ValueError('xhs_compile_failed')


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


def progress_for_ai(task_id, spec, cursor):
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
            progress = progress_for_ai(activity.get('activity_id'), payload.get('task_spec'),
                                      payload.get('cursor_after'))
        elif progress and payload.get('status') == 'profile_end_observed':
            progress['listing_coverage'] = 'explicit_end'
    return progress


def material_for_ai(payload):
    """Exactly the same bounded article material at foreground/background delivery."""
    result = {key: deepcopy(payload[key]) for key in (
        'title', 'source_id', 'source_provider', 'retrieved_at', 'article_text',
        'image_observations', 'coverage', 'limitations', 'content_summary', 'unread_notice') if key in payload}
    if 'image_observations' in result:
        gallery = payload.get('gallery_capture', payload.get('gallery'))
        if isinstance(gallery, dict):
            result['gallery'] = {k: deepcopy(gallery[k]) for k in ('declared_total','captured_pages','capture_complete') if k in gallery}
        result['image_observations'] = [{key: item[key] for key in ('page', 'observation', 'omitted_chars') if key in item}
                                        for item in result['image_observations'] if isinstance(item, dict)]
    return result
