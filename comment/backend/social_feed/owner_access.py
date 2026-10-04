"""One-use, short-lived owner handoff from the local MIRROW UI to the public wall."""

from __future__ import annotations

import secrets
import time
from threading import Lock


_lock = Lock()
_tickets: dict[str, float] = {}
_TTL_SECONDS = 90


def issue_owner_ticket() -> str:
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        expired = [value for value, expiry in _tickets.items() if expiry <= now]
        for value in expired:
            _tickets.pop(value, None)
        if len(_tickets) >= 16:
            _tickets.pop(min(_tickets, key=_tickets.get))
        _tickets[ticket] = now + _TTL_SECONDS
    return ticket


def consume_owner_ticket(ticket: str) -> bool:
    if not isinstance(ticket, str) or len(ticket) > 128:
        return False
    with _lock:
        expiry = _tickets.pop(ticket, 0)
    return expiry > time.time()
