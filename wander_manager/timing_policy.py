"""Small, deterministic boundary between model timing intent and real clocks."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

DEFAULT_DELAY_SECONDS = 900.0
MIN_AUTONOMOUS_DELAY_SECONDS = 5.0


def normalize_delay_seconds(value: Any, *, fallback: float = DEFAULT_DELAY_SECONDS,
                            minimum: float = MIN_AUTONOMOUS_DELAY_SECONDS,
                            now: datetime | None = None) -> tuple[float, str]:
    """Accept only finite, non-negative scalar seconds.

    The model never supplies an absolute clock: the caller anchors this delay
    using its trusted clock.  Zero is intentionally a short real wait, rather
    than an in-process busy loop.
    """
    if isinstance(value, bool):
        return fallback, "fallback"
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback, "fallback"
    if not math.isfinite(seconds) or seconds < 0:
        return fallback, "fallback"
    # This is a representability boundary, not a behavioural upper limit.
    seconds = max(float(minimum), seconds)
    try:
        (now or datetime.now()) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return fallback, "fallback"
    return seconds, "model"
