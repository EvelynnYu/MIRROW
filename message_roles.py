"""Message role semantics shared by persistence, context and UI adapters.

``notification`` is a durable, UI-only message.  It is intentionally not a
``divider``: notifications must never close a topic or become a conversation
boundary.  Keep the role contract here so a new UI-only event cannot
accidentally leak into an LLM consumer that only knows about divider messages.
"""

from __future__ import annotations

from typing import Any, Mapping


NOTIFICATION_ROLE = "notification"
TOPIC_DIVIDER_ROLE = "divider"

# Roles that are persisted for the interface but must not be turned into
# OpenAI-style conversation messages.  ``system`` remains context-bearing in
# this project and therefore is deliberately not included.
LLM_EXCLUDED_ROLES = frozenset({TOPIC_DIVIDER_ROLE, NOTIFICATION_ROLE})


def is_ui_only_role(role: Any) -> bool:
    return str(role or "") in LLM_EXCLUDED_ROLES


def is_topic_boundary_role(role: Any, event_type: Any = "") -> bool:
    """Whether a persisted message starts/ends a topic or scene.

    Legacy divider messages are real topic boundaries.  A notification is
    always false, even when it has an event type such as
    ``wish_board_update``.
    """

    return str(role or "") == TOPIC_DIVIDER_ROLE


def is_context_message(message: Mapping[str, Any] | Any) -> bool:
    """Return whether a message is eligible for LLM context consumers."""

    if isinstance(message, Mapping):
        role = message.get("role")
    else:
        role = getattr(message, "role", "")
    return not is_ui_only_role(role)

