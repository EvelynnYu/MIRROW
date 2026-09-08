"""模式检测引擎 — 步数停滞 / GPS 偏离 / 深夜活跃 / 作息矛盾

纯 SQL 聚合 + 简单统计，零 ML。所有检测按需运行，
结果写入 sentinel_events 表供上下文注入。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class PatternResult:
    """模式检测结果"""
    pattern_type: str       # step_stagnation / gps_deviation / late_night_screen / schedule_mismatch
    summary: str            # 人类可读描述
    detail: Dict[str, Any] = field(default_factory=dict)


class PatternDetector:
    """模式检测器 — 从 DataSnapshot 和 HealthStore 检测生活节奏信号"""

    def __init__(self, health_store):
        self._store = health_store
        # GPS 偏离计数器（需要连续 N 次才触发）
        self._gps_deviation_count: int = 0
        self._gps_deviation_category: Optional[str] = None

    # ── 步数停滞 ──────────────────────────────────────

    def detect_step_stagnation(self, snapshot) -> Optional[PatternResult]:
        """检测步数是否长时间未变化。基于时间戳对比而非计次。"""
        stagnant = snapshot.steps_stagnant_minutes
        if not stagnant or stagnant <= 0:
            return None

        cfg = self._get_config("step_stagnation", {})
        threshold = self._pick_stagnation_threshold(snapshot, cfg)

        if stagnant < threshold:
            return None

        # 确定关心梯度
        if snapshot.location_category == "work" and stagnant >= cfg.get("at_work_min", 60):
            summary = f"步数停滞{stagnant}分钟（在上班，可能是久坐）"
        elif snapshot.location_category not in ("home", "work") and stagnant >= cfg.get("outdoor_min", 30):
            loc = snapshot.location_address or "某处"
            summary = f"步数停滞{stagnant}分钟（外出中，位置: {loc}）"
        elif snapshot.location_category == "home" and stagnant >= cfg.get("at_home_min", 120):
            summary = f"步数停滞{stagnant}分钟（在家）"
        else:
            return None

        return PatternResult(
            pattern_type="step_stagnation",
            summary=summary,
            detail={"stagnant_minutes": stagnant, "location": snapshot.location_category},
        )

    def _pick_stagnation_threshold(self, snapshot, cfg) -> int:
        """根据 GPS 和用户状态选择步数停滞阈值"""
        if snapshot.location_category == "work":
            return cfg.get("at_work_min", 60)
        if snapshot.location_category not in ("home", "work", None):
            return cfg.get("outdoor_min", 30)
        return cfg.get("at_home_min", 120)

    # ── GPS 锚点偏离 ──────────────────────────────────

    def detect_gps_deviation(self, snapshot) -> Optional[PatternResult]:
        """连续 N 次 GPS 偏离锚点才触发，防 GPS 漂移误报。"""
        if snapshot.location_lat is None or snapshot.location_lng is None:
            self._gps_deviation_count = 0
            return None

        # 只在工作日上班时段检测
        if not self._is_work_time():
            self._gps_deviation_count = 0
            return None

        cfg = self._get_config("gps", {})
        work_lat, work_lng = cfg.get("work_lat"), cfg.get("work_lng")
        if not work_lat or not work_lng:
            return None

        from silicon_perception.collection.gps import GpsSource
        dist = GpsSource._haversine(
            snapshot.location_lat, snapshot.location_lng,
            work_lat, work_lng,
        )
        work_radius = cfg.get("work_radius_m", 200)

        if dist <= work_radius:
            # 在锚点范围内，清零
            self._gps_deviation_count = 0
            return None

        self._gps_deviation_count += 1
        if self._gps_deviation_count < 3:
            return None  # 需要连续 3 次（30min）

        return PatternResult(
            pattern_type="gps_deviation",
            summary=f"GPS定位偏离公司锚点（{dist:.0f}m），已持续{self._gps_deviation_count * 10}分钟",
            detail={"distance_m": dist, "consecutive_count": self._gps_deviation_count},
        )

    # ── 深夜屏幕/键鼠活跃 ─────────────────────────────

    def detect_late_night_activity(self, snapshot) -> Optional[PatternResult]:
        """深夜时段 + 屏幕活跃/键鼠活跃 → 熬夜信号。
        手表 24h 有心率数据不能说明熬夜，屏幕/键鼠活动才是真正的熬夜信号。"""
        hour = datetime.now().hour
        cfg = self._get_config("screen", {})
        night_start = cfg.get("late_night_start_hour", 0)
        night_end = cfg.get("late_night_end_hour", 6)

        if not (night_start <= hour < night_end):
            return None

        if not snapshot.screen_active:
            return None

        # 键鼠空闲 < 2min → 人在操作 → 确定熬夜
        idle = snapshot.input_idle_seconds
        if idle is not None and idle < 120:
            return PatternResult(
                pattern_type="late_night_screen",
                summary=f"凌晨{hour}点了还在用电脑",
                detail={"hour": hour, "input_idle": idle},
            )

        # 屏幕活跃但键鼠空闲 >2min → 可能在放视频
        if snapshot.screen_active:
            return PatternResult(
                pattern_type="late_night_screen",
                summary=f"凌晨{hour}点屏幕还在活跃（可能在放视频）",
                detail={"hour": hour},
            )

        return None

    # ── 作息矛盾（需要 user_schedule 表数据）───────────

    def detect_schedule_mismatch(self, snapshot) -> Optional[PatternResult]:
        """对照作息表检测矛盾：上班未出门、午休超时、下班未归。"""
        schedule = self._get_user_schedule()
        if not schedule:
            return None

        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute

        # 仅在休息日豁免
        if self._is_rest_day():
            return None

        # 上班未出门
        work_start = self._parse_time(schedule.get("work_start"))
        if work_start is not None:
            grace = work_start + 30  # 上班时间 + 30min 宽限
            if work_start <= now_minutes < grace:
                if snapshot.location_category != "work" and snapshot.user_status != "out":
                    return PatternResult(
                        pattern_type="schedule_mismatch",
                        summary="上班时间到了但人还在家",
                        detail={"schedule": "work_start", "expected": schedule["work_start"]},
                    )

        # 午休超时
        lunch_end = self._parse_time(schedule.get("lunch_end"))
        if lunch_end is not None:
            grace = lunch_end + 15
            if lunch_end <= now_minutes < grace:
                if snapshot.user_status == "napping":
                    return PatternResult(
                        pattern_type="schedule_mismatch",
                        summary="午休时间已过还在睡觉",
                        detail={"schedule": "lunch_end", "expected": schedule["lunch_end"]},
                    )

        # 下班未归
        work_end = self._parse_time(schedule.get("work_end"))
        if work_end is not None:
            overtime = work_end + 120  # 下班后 2h 才触发
            if work_end <= now_minutes < overtime:
                if snapshot.location_category == "work":
                    return PatternResult(
                        pattern_type="schedule_mismatch",
                        summary="已经过了下班时间，还在公司",
                        detail={"schedule": "work_end", "expected": schedule["work_end"]},
                    )

        return None

    # ── 辅助 ──────────────────────────────────────────

    def _get_config(self, section: str, default: dict = None) -> dict:
        try:
            from mirrow_core.settings_manager import get_setting
            ss = get_setting("silicon_perception") or {}
            return ss.get(section, default or {})
        except Exception:
            return default or {}

    @staticmethod
    def _get_user_schedule() -> Optional[dict]:
        try:
            from silicon_perception.recording.health_store import get_store
            conn = get_store()._get_conn()
            row = conn.execute("SELECT * FROM user_schedule WHERE id=1").fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    @staticmethod
    def _is_rest_day() -> bool:
        try:
            from calendar_manager.database import is_rest_day_today
            return is_rest_day_today()
        except Exception:
            return False

    @staticmethod
    def _is_work_time() -> bool:
        """判断当前是否在工作日的工作时段内（含午休）。"""
        if PatternDetector._is_rest_day():
            return False
        schedule = PatternDetector._get_user_schedule()
        if not schedule:
            return False
        now_minutes = datetime.now().hour * 60 + datetime.now().minute
        work_start = PatternDetector._parse_time(schedule.get("work_start"))
        work_end = PatternDetector._parse_time(schedule.get("work_end"))
        if work_start is None or work_end is None:
            return False
        # 允许 1h 前后缓冲
        return (work_start - 60) <= now_minutes <= (work_end + 120)

    @staticmethod
    def _parse_time(hhmm: Optional[str]) -> Optional[int]:
        """HH:MM → 分钟数。"""
        if not hhmm:
            return None
        try:
            parts = hhmm.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return None

    # ── 批量评估 ──────────────────────────────────────

    def evaluate_all(self, snapshot) -> List[PatternResult]:
        """运行所有检测器，返回触发的模式列表。"""
        results = []
        detectors = [
            self.detect_step_stagnation,
            self.detect_gps_deviation,
            self.detect_late_night_activity,
            self.detect_schedule_mismatch,
        ]
        for detector in detectors:
            try:
                result = detector(snapshot)
                if result:
                    results.append(result)
            except Exception as e:
                logger.debug(f"PatternDetector {detector.__name__} 异常: {e}")

        return results
