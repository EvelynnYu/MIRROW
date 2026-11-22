"""Short stop facts from persisted runtime causes, never generated accounts."""


def stop_reason_text(activity, nodes):
    reason = activity.get('settlement_reason')
    cause = activity.get('abort_reason')
    if cause == 'not_started_after_user_interrupt':
        return '前面的活动被中断，这项活动还没开始。'
    if cause == 'not_started_after_prior_execution_error':
        return '前面的活动出了问题，这项活动还没开始。'
    if reason == 'user_interrupt':
        if cause == 'user_message':
            return '人类伙伴发来消息，活动中断了。'
        if cause == 'manual_stop':
            return '人类伙伴关闭了漫想，活动中断了。'
        if any(node.get('abort_reason') == 'process_exit' for node in nodes):
            return '后端进程退出，活动中断了。'
        return '活动收到中断请求，停下了。'
    if reason == 'execution_error':
        if activity.get('activity_type') == 'browse_xiaohongshu':
            from .xhs_request import TASK_ERRORS
            from .xhs_request import OPEN_ERRORS
            for node in reversed(nodes):
                payload = node.get('source_payload') or {}
                if message := OPEN_ERRORS.get(payload.get('status')):
                    return message
                if payload.get('failure_stage') == 'task_compilation':
                    message = TASK_ERRORS.get(payload.get('status'))
                    if message:
                        return message
        if any(node.get('abort_reason') == 'node_review_unavailable' for node in nodes):
            return '节点回顾未能完成，活动中断了。'
        return '活动执行时出了问题，中断了。'
    if reason == 'game_stop':
        return '人类伙伴结束了这次游戏邀请，活动停下了。'
    return ''
