"""Use the existing AI context to resolve a chosen browsing target, once."""
from context_builder.assembly import assemble_context_result
from context_builder.channel_projection import backend_turn


async def task_messages(instruction, contract, *, context=None, builder=None, history_scope=None, references_only=False):
    if context is None:
        return [{'role': 'system', 'content': contract}, {'role': 'user', 'content': instruction}]
    if not context.persona or not context.session_id:
        raise ValueError('xhs_context_unavailable')
    if builder is None:
        from context_builder.builder import ContextBuilder
        builder = ContextBuilder.build
    try:
        built = await builder('WANDER_ACTIVITY', persona=context.persona, session_id=context.session_id,
            context_query=instruction, wander_runtime_text='[本次已选择的小红书行动]\n' + instruction,
            **(history_scope or {}))
        if references_only:
            from context_builder.assembly import context_result_zones
            history = [m for m in context_result_zones(built)['history'] if m.get('role') in ('user','assistant')
                       and not m.get('name') and isinstance(m.get('content'), str)]
            return [{'role':'system','content':contract}, *history[-6:], backend_turn(instruction)]
        messages = assemble_context_result(built)
    except Exception as exc:
        raise ValueError('xhs_context_unavailable') from exc
    if not messages:
        raise ValueError('xhs_context_unavailable')
    return messages + [backend_turn(contract)] if contract else messages


async def chat_context(runner, session_id):
    import inspect
    try:
        value = runner.context_provider()
        if inspect.isawaitable(value):
            value = await value
    except Exception as exc:
        raise ValueError('xhs_context_unavailable') from exc
    context = getattr(value, 'decision', None)
    if context is None or context.session_id != session_id or not context.persona:
        raise ValueError('xhs_context_unavailable')
    return context
