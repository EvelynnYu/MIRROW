"""Process-local duplicate suppression backed by explicit host commit receipts.

The host's durable message ID remains the restart boundary. This gate only
serializes local callbacks; a failure or cancellation never claims delivery.
"""

import asyncio
from functools import wraps


class ConfirmedDeliveryGate:
    def __init__(self, limit: int = 200):
        self._lock = asyncio.Lock()
        self._confirmed: dict[str, None] = {}
        self._limit = max(1, limit)

    def __call__(self, send):
        @wraps(send)
        async def deliver(payload):
            event_id = str(payload.get("event_id") or "")
            # Reception owns a separate durable dual-write receipt and may
            # retry solely to repair its frontend cache.
            if payload.get("event_type") == "visit_lounge" and payload.get("activity_source") == "visitor":
                event_id = ""
            async with self._lock:
                if event_id and event_id in self._confirmed:
                    return True
                result = await send(payload)
                if result is True and event_id:
                    self._confirmed[event_id] = None
                    while len(self._confirmed) > self._limit:
                        del self._confirmed[next(iter(self._confirmed))]
                return result
        return deliver
