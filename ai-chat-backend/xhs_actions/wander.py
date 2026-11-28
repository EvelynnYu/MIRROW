"""Execute the social inbox activities registered by the public Wander planner."""
from wander_manager.event_types import EventType
from wander_manager.node_execution_adapter import NodeExecutionResult, NodeExecutionStatus


async def social_node(event_type, store, activity, node_id, decision_context):
    if event_type == EventType.XHS_DIRECT_MESSAGES:
        from xhs_interactions.native import available
        if not available():
            return NodeExecutionResult(NodeExecutionStatus.SKIPPED, error='dm_account_not_configured',
                                       summary='尚未配置并启用小红书私信。', retry_safe=False)
        from .dm_tool import attend
        report = await attend(decision_context)
        return NodeExecutionResult(NodeExecutionStatus.SUCCEEDED, summary=report,
                                   payload={'summary':report}, retry_safe=False,
                                   completion_signal='dm_checked')
    if event_type == EventType.CHECK_XIAOHONGSHU_INTERACTIONS:
        from wander_manager.xhs_interaction_runtime import execute_node
        return await execute_node(store, activity, node_id, decision_context)
    raise ValueError('unsupported_inbox_activity')
