"""Current Affect and device-idle readings for Wander planning and execution."""

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class RuntimeStateReadings:
    """Read live state without turning unavailable sensors into made-up facts."""

    def __init__(self, get_idle_seconds: Optional[Callable[[], float]] = None):
        self._get_idle_seconds = get_idle_seconds

    def mood(self) -> str:
        """Return Affect's self projection when it is available."""
        try:
            from affect import get_affect_service
            return get_affect_service().render_self(cap=700)
        except Exception:
            logger.warning("Wander Affect projection unavailable")
            return ""

    def physical_idle_seconds(self) -> Optional[float]:
        """Return device-idle seconds, or ``None`` when no reading exists."""
        if not self._get_idle_seconds:
            return None
        try:
            value = float(self._get_idle_seconds())
            return value if value >= 0 else None
        except Exception:
            return None

    def idle_seconds(self) -> float:
        """Numeric device-idle value for the node executor."""
        return self.physical_idle_seconds() or 0.0

    def away_context(self) -> str:
        """Describe a sufficiently long physical idle interval as a fact."""
        idle = self.idle_seconds()
        if idle < 60:
            return ""
        total_min = int(idle / 60)
        hours, minutes = divmod(total_min, 60)
        if hours and minutes:
            return f"用户已经离开 {hours} 小时 {minutes} 分钟。"
        if hours:
            return f"用户已经离开 {hours} 小时。"
        return f"用户已经离开 {minutes} 分钟。"
