"""Execution-bound settlement accounts. No prose is evidence of an action."""
from .runtime_models import SettlementReason


def unexecuted(activity, nodes, reason):
    # Sleep has a real timer instead of nodes. An external-signal wait by
    # itself is not proof that an action completed.
    if (activity.get('activity_type') == 'sleep'
            and reason == SettlementReason.TIMER_ENDED
            and activity.get('timer_ends_at')):
        return False
    # A planned/running/aborted row alone is not an action. Use the same
    # admitted-material boundary as the frozen experience consumers. Failed
    # observations remain materials, including their unavailable-source facts.
    from memory_v2.experience_contract import snapshot
    return snapshot(dict(activity, retained_impression='', retained_node_ids=[]), nodes)['operational_only']


def constrain_unexecuted(decision, activity, nodes, reason):
    """Keep scheduling intact, but an empty activity acquires no experience."""
    if not unexecuted(activity, nodes, reason):
        return decision
    from .runtime_decision_adapters import EmotionEffect
    no_new = (activity.get('activity_type') == 'check_xiaohongshu_interactions' and bool(nodes)
              and all(n.get('execution_status') == 'skipped'
                      and (n.get('source_payload') or {}).get('status') == 'no_pending_interactions'
                      for n in nodes))
    decision.summary = ('没有新评论或艾特，本次查看已结束。' if no_new
                        else '本次尚未执行就结束了，没有新增经历。')
    decision.retained_impression = ''
    decision.retained_node_ids = []
    decision.purpose_outcome = None
    decision.emotion_effect = EmotionEffect()
    decision.share = False
    return decision
