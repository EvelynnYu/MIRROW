"""Recipe-based context with explicit source packets and stable identity."""
from datetime import datetime
import json
from context_builder.builder import ContextBuilder
from context_builder.recipes import Recipe, RecipeSection


async def identity_context(persona):
    # A host-specific recipe is intentionally restricted to configured sources.
    # No production singleton, implicit diary or device fallback is consulted.
    builder = ContextBuilder()
    result = await builder._build_from_recipe(Recipe('公开宿主身份', '', [RecipeSection('身份', 'persona', guide='')]), persona=persona)
    return result


async def runtime_context(recipe, *, persona='', wander_runtime_text='', **kwargs):
    builder = ContextBuilder(kwargs.get('session_id', ''))
    return await builder._build_from_recipe(Recipe(recipe, '', [
        RecipeSection('身份', 'persona', guide=''), RecipeSection('运行态', 'wander_runtime', guide='')]),
        persona=persona, wander_runtime_text=wander_runtime_text)


async def chat_context(store, memory, user_text):
    if getattr(memory, 'context_sources', None):
        from .context_sources import history_messages
        from context_builder.assembly import assemble_context_result
        rows, diaries = memory.context_sources.select_history(exclude_current=True)
        active_date = store.messages()[-1]['active_date']
        facts = await memory.context_sources.facts(user_text, active_date=active_date)
        facts.update(memory_sources=await memory.search(user_text), earlier_diaries=diaries,
            current_time=datetime.now().astimezone().isoformat(), active_date=active_date)
        result = await ContextBuilder.build('FULL_CHAT', host_history=history_messages(rows),
            host_materials=facts, host_turn=[{'role': 'user', 'content': user_text}])
        return assemble_context_result(result)
    profile = store.profile()
    messages = []
    rows = store.messages()
    for item in rows[:-1]:
        # Timestamps and event provenance are frozen on insertion, not rebuilt as "now".
        provenance = f"时间：{item['timestamp']}"
        if item['event_type']:
            provenance += f"；事件来源：{item['event_type']}"
        messages.append({'role': item['role'], 'content': f"[{provenance}]\n{item['content']}"})
    recalled = await memory.search(user_text)
    facts = {'recorded_at': datetime.now().astimezone().isoformat(),
        'human_display_name': profile['human_name'], 'agent_display_name': profile['agent_name'],
        'memory_sources': recalled}
    if profile.get('relationship_started_at'):
        facts['configured_relationship_started_at'] = profile['relationship_started_at']
    facts['current_message'] = {key: rows[-1][key] for key in ('timestamp', 'message_id', 'role')}
    facts['cognition'] = memory.core_context()
    messages.append({'role': 'system', 'name': 'mirrow_sources', 'content': json.dumps(facts, ensure_ascii=False)})
    messages.append({'role': 'user', 'content': user_text})
    return messages
