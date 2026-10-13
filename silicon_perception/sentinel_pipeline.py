"""Grounded second-stage and receipt handling for the optional sensor host.

The host supplies a structured-decision message generator and a durable sink.
No device accounts, private context or action handlers are discovered here.
"""
from datetime import datetime
import time
from .analysis.location_episode import deferred_key
from .analysis.sentinel_grounding import build_sentinel_fact_contract, natural_care_motivation
from .delivery import deliver_push, new_chat_since


def cancelled(host, started):
    snapshot = host._last_snapshot
    age = host._get_last_message_seconds()
    return host._should_suppress_push(snapshot) or new_chat_since(started, age)


async def generate_and_deliver(host, decision, *, started):
    if cancelled(host, started):
        return False
    current = build_sentinel_fact_contract(host._last_snapshot)
    from .analysis.sentinel_grounding import SentinelFactContract
    prior = SentinelFactContract.from_dict(decision.get('fact_contract'))
    packet = dict(decision)
    if current.location_detail_changed_from(prior):
        packet['observation'] = ''
        packet['push_motivation'] = natural_care_motivation(should_push=True)
    packet['fact_contract'] = current.to_dict()
    result = await host._call_pro_llm(packet)
    text = result.get('content', '') if isinstance(result, dict) else str(result or '')
    newest = build_sentinel_fact_contract(host._last_snapshot)
    if not text.strip() or cancelled(host, started) or newest.location_detail_changed_from(current) or not newest.validate_text(text).accepted:
        return False
    return await deliver_push(host._on_push, {'message': text, 'rule_id': 'flash_push',
        'priority': 'medium', 'timestamp': datetime.now().isoformat()}, store=host._store,
        event={'rule_id': 'flash_push', 'priority': 'pattern', 'message': packet.get('observation', ''),
            'event_type': 'pattern'}, context=host._last_flash_context)


async def process_decision(host, snapshot, triggers):
    decision = host._summarizer.get_decision().to_dict()
    decision['fact_contract'] = host._summarizer.get_fact_contract().to_dict()
    host._trigger_detector.record_flash_decision(snapshot, triggers, decision['should_push'])
    host._last_flash_context = {'timestamp': datetime.now().isoformat(), 'triggers': triggers,
        'should_push': decision['should_push'], 'pushed': False}
    if not decision['should_push'] or not host._on_push or not host._call_pro_llm:
        return False
    started = time.time()
    if host._should_suppress_push(snapshot):
        host._deferred_push[deferred_key(triggers)] = {'decision': decision, 'queued_at': started,
            'at': time.monotonic()}
        return False
    return await generate_and_deliver(host, decision, started=started)


async def check_deferred(host):
    if not host._call_pro_llm or not host._on_push:
        return
    for key, item in list(host._deferred_push.items()):
        if time.monotonic() - item['at'] < host._push_defer_seconds:
            continue
        del host._deferred_push[key]
        if 'decision' in item:
            await generate_and_deliver(host, item['decision'], started=item['queued_at'])
