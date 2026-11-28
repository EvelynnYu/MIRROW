"""Opt-in category routing at existing text/JSON transport boundaries."""
from __future__ import annotations
import asyncio
import copy
from .policies import category_for, route_role
from .scope import routing_scope, phase_status


@routing_scope
async def call_managed(route: str, messages: list, *, enable_thinking=False, temperature=None,
                       max_tokens=None, json_mode=False, request_timeout=None,
                       response_format=None, max_attempts=None, result_validator=None, request_projector=None, inspect_provider=False,
                       image_batch=None):
    from .config import load_config, resolve_chain
    from .scope import current_snapshot
    config, _, _ = current_snapshot() or await asyncio.to_thread(load_config)
    if category_for(route, config.assignments) not in config.policies and route not in config.overrides:
        return None
    sources, timeout = await asyncio.to_thread(resolve_chain, route)
    sources = copy.deepcopy(sources)
    # The node owns its thinking contract. Binding an old structural Flash
    # source must not silently disable final expression or schema-review thought.
    for source in sources:
        if enable_thinking is not None:
            source["model"]["thinking"] = "default"
        if response_format and response_format.get("type") == "json_schema" and source["station"]["id"] in {"deepseek_direct", "deepseek_expression"}:
            source["station"]["protocol"] = "openai-responses"
    from .runner import run_structured
    result = await run_structured(messages, temperature=temperature if temperature is not None else 1.0,
        max_tokens=max_tokens, route=route, enable_thinking=enable_thinking, sources=sources,
        timeout_seconds=timeout, request_timeout=request_timeout, json_mode=json_mode,
        response_format=response_format, result_validator=result_validator,
        max_rounds=max_attempts, request_projector=request_projector, inspect_provider=inspect_provider,
        **({'image_batch': image_batch} if image_batch else {}))
    phase_status(route, result.status, getattr(result,"phase_id",None))
    return result


def flash_route(usage_tag: str) -> str:
    from llm_usage_scope import usage_metadata
    if usage_metadata().get('cognition_stage') == 'brief_review':
        return 'cognition_brief_review'
    tag = (usage_tag or "").casefold()
    if "sentinel_activity_summary" in tag:
        return "sentinel_activity_summary"
    if any(word in tag for word in ("group_reply", "self_task_executor", "alarm_closure", "reading_followup")):
        return "workshop_expression"
    if any(word in tag for word in ("cognition", "world_book", "self_book", "other_book", "interest")):
        return "cognition_maintenance"
    if any(word in tag for word in ("diary", "summary", "long_text")):
        return "daily_summary"
    if "intent" in tag or "lightweight" in tag:
        return "intent_analysis"
    if "sentinel_first" in tag:
        return "sentinel_first_layer"
    if "wander" in tag:
        return "wander_message"
    if "lounge" in tag or "visit" in tag:
        return "lounge_expression"
    if "workshop" in tag:
        return "workshop_expression"
    if any(word in tag for word in ("sentinel", "reminder", "push", "music")):
        return "sentinel_push"
    return "structured"
