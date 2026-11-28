"""Single backend admission gate for Wander event selection.

Initial planning and review-time ``switch_to`` both ask one question here:
which abilities are really usable right now.  Only real capability boundaries
(service running, device online, feature enabled) belong here.  A failed
attempt is a fact recorded by the runtime; it does not hide the ability again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Callable, Iterable, Optional

from .event_types import EventType

# The dedicated phone is probed at most once per minute so planning and review
# never shell out on every tick.
_XHS_DEVICE_TTL_SECONDS = 60.0
_xhs_device_cache: dict[str, float | bool] = {"at": 0.0, "ready": False}


def _xhs_serial() -> str:
    return str(os.getenv("MIRROW_XHS_ADB_SERIAL", "") or "").strip()


def xhs_device_ready(ttl: float = _XHS_DEVICE_TTL_SECONDS) -> bool:
    """Whether the dedicated ADB phone is reachable right now (cached).

    Only a read-only ``adb devices`` probe; the serial is never logged or
    returned.  A missing/unresponsive adb is a plain "not available", not an
    exception into the planner.
    """
    now = time.monotonic()
    if ttl > 0 and now - float(_xhs_device_cache["at"]) < ttl:
        return bool(_xhs_device_cache["ready"])
    ready = False
    adb = shutil.which("adb")
    if adb:
        serial = _xhs_serial()
        try:
            proc = subprocess.run(
                [adb, "devices"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            online = [parts[0] for line in (proc.stdout or "").splitlines()[1:]
                      if len(parts := line.split()) >= 2 and parts[1] == "device"]
            ready = serial in online if serial else len(online) == 1
        except (OSError, subprocess.SubprocessError):
            ready = False
    _xhs_device_cache.update(at=now, ready=ready)
    return ready


def _safe(check: Optional[Callable[[], bool]]) -> bool:
    if check is None:
        return True
    try:
        return bool(check())
    except Exception:
        return False


def _capability_available(event_type: EventType, availability_checks: dict) -> bool:
    """Real ability check; a missing capability is not a cooldown."""
    if event_type in availability_checks and not _safe(availability_checks.get(event_type)):
        return False
    if event_type in (EventType.XHS_DIRECT_MESSAGES, EventType.CHECK_XIAOHONGSHU_INTERACTIONS):
        from .xhs_interaction_runtime import available as interactions_available
        if not interactions_available() or (event_type == EventType.XHS_DIRECT_MESSAGES and not xhs_device_ready()):
            return False
    elif event_type == EventType.PUBLISH_XIAOHONGSHU:
        from xhs_publish.store import autonomy_available
        try:
            if not autonomy_available():
                return False
        except (ValueError, OSError):
            return False
    elif event_type == EventType.BROWSE_XIAOHONGSHU:
        # Native browser work needs the dedicated phone to be actually online.
        if not xhs_device_ready():
            return False
    return True


def event_available(event_type: EventType, availability_checks: Optional[dict] = None) -> bool:
    return _capability_available(event_type, availability_checks or {})


def filter_allowed(
    allowed: Iterable[EventType],
    availability_checks: Optional[dict] = None,
) -> frozenset[EventType]:
    """One admission answer for both planning and review-time successors."""
    result = set(allowed)
    # Legacy visits remain executable for saved runs and direct invitations;
    # the three concrete workshop entries replace them in planning.
    if result.intersection({EventType.WORKSHOP_CREATE, EventType.WORKSHOP_HISTORY, EventType.WORKSHOP_CONTINUE}):
        result.discard(EventType.VISIT_WORKSHOP)
    return frozenset(
        event_type for event_type in result
        if event_available(event_type, availability_checks)
    )
