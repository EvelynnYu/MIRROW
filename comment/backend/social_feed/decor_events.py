"""Durable gift facts share the existing event/day-summary consumption path."""
import asyncio
import sys

_delivery_lock = asyncio.Lock()


async def publish_pending(store=None):
    from .decor_store import get_decor_store
    from shared_state import get_active_session_id
    store = store or get_decor_store()
    main = sys.modules.get('main') or sys.modules.get('__main__')
    sender = getattr(main,'_emit_ui_notification',None)
    session = get_active_session_id()
    if not sender or not session:
        return 0
    from lounge_visits import focus
    if focus.busy():
        return 0
    count = 0
    async with _delivery_lock:
        with store.connect() as db:
            rows = db.execute('SELECT id,fact FROM events WHERE delivered=0 ORDER BY created LIMIT 20').fetchall()
        for row in rows:
            result = await sender(session,row['id'],None,event_type='social_gift',content=row['fact'])
            if not isinstance(result,dict) or not (result.get('mutated') or result.get('reason')=='already_persisted'):
                break
            with store.connect() as db:
                db.execute('UPDATE events SET delivered=1 WHERE id=?',(row['id'],))
            count += 1
    return count


async def draft_exhibit(body):
    from shared_state import get_active_session_id, get_latest_persona_prompt
    from context_builder.builder import ContextBuilder
    from context_builder.assembly import assemble_context_result
    from llm_client import call_llm_api_profile
    from behavior_scheduler import get_global_scheduler
    from wander_manager.flash_structured import parse_json_object
    from .decor_models import Exhibit
    from .decor_store import DecorError
    persona, session = get_latest_persona_prompt(), get_active_session_id()
    if not persona or not session:
        raise DecorError('k_context_unavailable')
    # FULL_CHAT already provides the real active day, persona and memory chain.
    context = await ContextBuilder.build('FULL_CHAT',persona=persona,session_id=session,
                                        user_message=body.facts,context_query=body.facts)
    messages = assemble_context_result(context)
    messages.append({'role':'user','content':'站主请你为共域的奇物架拟稿。以下是站主提供的物品事实：'+body.facts+
        '\n当前资料：'+body.current.model_dump_json()+
        '\n请结合已知人格和记忆写物品名、描述，以及你给访客的一两句留言。内容将给共域访客看；涉及站主的现实身份、住址、具体行踪或工作隐私时使用不暴露细节的表达。'+
        '缺乏事实的来历保留未知。只输出 JSON：{"name":"最多40字","description":"最多500字","ai_note":"最多200字"}。草稿由站主审阅后发布。'})
    scheduler = await get_global_scheduler()
    result = await call_llm_api_profile(messages,scheduler.main_model_profile_id)
    parsed = parse_json_object(result.get('content',''))
    if not isinstance(parsed,dict):
        raise DecorError('draft_unavailable')
    merged = body.current.model_dump() | {key:parsed.get(key,body.current.model_dump()[key]) for key in ('name','description','ai_note')}
    try:
        return Exhibit.model_validate(merged).model_dump()
    except ValueError:
        raise DecorError('draft_unavailable') from None
