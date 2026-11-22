"""Memory lookup receipts: an empty search is not a failed read or a memory."""


def result(outcome, *, mode='', query='', reason='', candidate_count=None):
    return dict(memory_fetch_version=1, outcome=outcome, mode=mode, query=query,
                reason=reason, candidate_count=candidate_count)


def empty_lookup(node):
    payload = node.get('source_payload') or {}
    return (node.get('execution_status') == 'skipped'
            and payload.get('memory_fetch_version') == 1
            and payload.get('outcome') == 'no_result')


def description(payload):
    prefix = ('沿“' + payload['query'] + '”回想：' if payload.get('query') else
              '翻找旧对话：' if payload.get('mode') == 'scene' else '回想：')
    text = {
        'no_unread_match': '这次没有找到尚未读过的匹配记忆。',
        'no_scene': '这次没有找到符合条件的旧对话。',
        'invalid_choice': '回忆方式未能解析。',
        'selection_unavailable': '未取得回忆方式的选择。',
        'retrieval_unavailable': '记忆读取出了问题。',
        'scene_unavailable': '旧对话读取出了问题。',
    }.get(payload.get('reason'), '未取得记忆材料。')
    return prefix + text
