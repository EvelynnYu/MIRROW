"""Record delivery only after the message sink confirms durable acceptance.

None (including focus suppression) is not a receipt. Unknown outcomes are not
automatically retried: the callback may already have performed physical actions.
"""
import logging
import math
import time

logger = logging.getLogger(__name__)


class PushSuperseded(Exception):
    """Generation was overtaken by a human message or changed delivery gate."""


def new_chat_since(started_at, last_message_seconds=None, *, now=None):
    if started_at is None:
        return False  # Critical alerts retain their separate delivery policy.
    try:
        if last_message_seconds is None:
            from silicon_perception.analysis.behavior_profile import get_behavior_profile
            last_message_seconds = get_behavior_profile().get_last_user_message_seconds()
        started = float(started_at)
        age = float(last_message_seconds)
        current = time.time() if now is None else now
        if not all(math.isfinite(v) for v in (started, age, current)):
            return True
        return age <= max(0, current - started)
    except (TypeError, ValueError, OverflowError):
        return True


async def deliver_push(callback, payload, *, store, event, context=None):
    state = "delivery_unavailable"
    pushed = False
    if callback is not None:
        try:
            pushed = (await callback(payload)) is True
            state = "pushed" if pushed else "delivery_unconfirmed"
        except Exception as exc:
            state = "delivery_failed"
            logger.warning("Sentinel delivery failed: %s", type(exc).__name__)
    if context is not None:
        context.update(pushed=pushed, delivery_state=state)
        if pushed:
            context["push_content"] = payload["message"]
    try:
        store.insert_event(**event, pushed=pushed)
    except Exception as exc:
        # An audit failure cannot undo a confirmed message or justify a retry.
        logger.error("Sentinel delivery audit failed: %s", type(exc).__name__)
    return pushed
