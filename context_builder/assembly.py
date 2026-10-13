"""Small, side-effect-free helpers for final context assembly.

Recipe and ContextBuilder still decide which material exists.  This module
only owns the last cross-zone merge so callers do not each reimplement the
``prefix -> history -> context -> turn`` order.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


ZONE_PREFIX = "prefix"
ZONE_HISTORY = "history"
ZONE_CONTEXT = "context"
ZONE_TURN = "turn"
VALID_ZONES = frozenset({ZONE_PREFIX, ZONE_HISTORY, ZONE_CONTEXT, ZONE_TURN})


def _copy_message(message: Any) -> dict:
    """Return an owned message copy without flattening multimodal content."""

    if isinstance(message, Mapping):
        value = dict(message)
    elif hasattr(message, "model_dump"):
        value = message.model_dump()
    elif hasattr(message, "dict") and callable(message.dict):
        value = message.dict()
    else:
        value = {
            "role": getattr(message, "role", ""),
            "content": getattr(message, "content", ""),
        }
    return deepcopy(value)


def assemble_messages(
    *,
    prefix: Iterable[Any] = (),
    history: Iterable[Any] = (),
    context: Iterable[Any] = (),
    turn: Iterable[Any] = (),
) -> list[dict]:
    """Merge the four shared zones and never mutate caller-owned messages."""

    result: list[dict] = []
    for zone in (prefix, history, context, turn):
        for message in zone:
            item = _copy_message(message)
            if item.get("role") == "event":
                from .event_history_adapter import project_event_for_context

                item = project_event_for_context(item)
                if item is None:
                    continue
                item = {"role": "system", "content": item["content"]}
            if item.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError(f"unsupported provider message role: {item.get('role')!r}")
            result.append(item)
    return result


def context_result_zones(
    result: Any,
    *,
    formatted_messages: Iterable[Any] | None = None,
    include_turn: bool = True,
) -> dict[str, list[dict]]:
    """Project a ContextResult into the shared four-zone skeleton.

    ``formatted_messages`` can be supplied when a caller has added a
    request-local history projection after Builder returned.  It is copied,
    just like the ContextResult-owned messages.
    """

    formatted = [
        _copy_message(message)
        for message in (
            formatted_messages
            if formatted_messages is not None
            else (getattr(result, "formatted_messages", None) or [])
        )
    ]
    if not formatted:
        system = str(getattr(result, 'system_content', '') or '')
        if system:
            formatted = [{'role': 'system', 'content': system}]
    prefix: list[dict] = []
    history = formatted
    if history and history[0].get("role") == "system":
        prefix = [history.pop(0)]

    context: list[dict] = []
    dynamic = str(getattr(result, "dynamic_content", "") or "").strip()
    dynamic_messages = getattr(result, 'dynamic_messages', None) or []
    if dynamic_messages:
        context.extend(_copy_message(message) for message in dynamic_messages)
    elif dynamic:
        context.append({"role": "system", "content": dynamic})
    context.extend(
        _copy_message(message)
        for message in (getattr(result, "context_messages", None) or [])
    )

    turn = (
        [
            _copy_message(message)
            for message in (getattr(result, "tail_messages", None) or [])
        ]
        if include_turn
        else []
    )
    return {
        ZONE_PREFIX: prefix,
        ZONE_HISTORY: history,
        ZONE_CONTEXT: context,
        ZONE_TURN: turn,
    }


def assemble_context_result(
    result: Any,
    *,
    formatted_messages: Iterable[Any] | None = None,
    include_turn: bool = True,
) -> list[dict]:
    """Assemble one ContextResult in the single shared zone order."""

    zones = context_result_zones(
        result,
        formatted_messages=formatted_messages,
        include_turn=include_turn,
    )
    return assemble_messages(**zones)


def assemble_context_text(result: Any) -> str:
    """Losslessly render ordered text material for single-system consumers.

    Native history roles remain explicit source labels. These are historical
    records, not the current user turn; multimodal material must use messages.
    """
    formatted = getattr(result, "formatted_messages", None)
    if not formatted:
        system = str(getattr(result, "system_content", "") or "")
        formatted = [{"role": "system", "content": system}] if system else []
    messages = assemble_context_result(result, formatted_messages=formatted)
    parts = []
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError("single-system context requires text material")
        role = message.get("role")
        STATUS_MATERIAL_NAME = "mirrow_status_event"
        if message.get("name") == STATUS_MATERIAL_NAME:
            content = f"[历史材料·MIRROW 状态记录]\n{content}"
        elif message.get('name') in {'mirrow_inner_record', 'mirrow_inner_snapshot'}:
            content = f'[MIRROW 内在记录，非聊天原话]\n{content}'
        elif role != "system":
            label = {"user": "人类伙伴", "assistant": "AI", "tool": "工具"}.get(role, role)
            content = f"[历史消息·{label}]\n{content}"
        if content.strip():
            parts.append(content)
    return "\n\n".join(parts)


def split_context_result_for_scheduler(
    result: Any,
    *,
    formatted_messages: Iterable[Any] | None = None,
) -> tuple[str, list[dict]]:
    """Return the stable system prefix and the remaining ordered messages."""

    zones = context_result_zones(result, formatted_messages=formatted_messages)
    prefix = zones.pop(ZONE_PREFIX)
    system_prompt = str(prefix[0].get("content") or "") if prefix else ""
    return system_prompt, assemble_messages(**zones)


def assemble_provider_messages(
    *,
    system_content: str,
    history: Iterable[Any] = (),
    context: Iterable[Any] = (),
    turn: Iterable[Any] = (),
) -> list[dict]:
    """Build the final provider request without content-based deduplication."""

    prefix = (
        [{"role": "system", "content": system_content}]
        if str(system_content or "").strip()
        else []
    )
    return assemble_messages(prefix=prefix, history=history, context=context, turn=turn)
