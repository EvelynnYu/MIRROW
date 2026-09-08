"""异常检测规则引擎

纯 Python 阈值规则，零延迟零 token 消耗。
每条规则有独立冷却，防止同一异常反复推送。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

# 心率时效门控阈值（秒）：>15min 无新鲜读数视为陈旧，规则不再用它报警。
# 手环没戴/断连不吐数据（0x2A37 实时 notify），在线读数写入时刻≈采集时刻。
HR_MAX_AGE = 900


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class DataSnapshot:
    """一次监控周期采集的所有数据聚合"""
    timestamp: str = ""
    heart_rate: Optional[int] = None
    screen_active: Optional[bool] = None
    foreground_app: Optional[str] = None
    foreground_exe: Optional[str] = None
    mirrow_visible: Optional[bool] = None
    user_status: Optional[str] = None
    is_period: bool = False
    period_day: Optional[int] = None

    # 派生上下文（由 anomaly_detector 填充）
    hr_avg_5min: Optional[int] = None
    hr_avg_prev_5min: Optional[int] = None  # 5-10分钟前的均值（用于骤升检测）
    hr_count_3min: int = 0
    hr_count_5min: int = 0
    last_message_seconds: float = 999999.0
    hr_sustained_high_count: int = 0  # 连续 >threshold 的采样次数

    # 🆕 键鼠空闲
    input_idle_seconds: Optional[float] = None

    # 🆕 步数
    cumulative_steps: Optional[int] = None
    steps_today: Optional[int] = None
    steps_stagnant_minutes: int = 0

    # 🆕 GPS
    location_lat: Optional[float] = None
    location_lng: Optional[float] = None
    location_address: Optional[str] = None
    location_category: Optional[str] = None

    # 🆕 屏幕使用时长
    screen_time_minutes: Optional[int] = None
    top_app_category: Optional[str] = None

    # 🆕 当前手机前台App + 屏幕状态 + 系统分类（2min 轮询）
    mobile_app_package: Optional[str] = None
    mobile_screen_on: Optional[bool] = None
    mobile_app_name: Optional[str] = None

    # 🆕 行为状态
    behavior_state: Optional[str] = None
    active_app_session: Optional[str] = None

    # 🆕 字段时效：field_group → captured_at ISO 时间戳。用于 is_stale() 判断。
    field_ts: Dict[str, str] = field(default_factory=dict)

    def age_of(self, key: str) -> Optional[float]:
        """字段距采集的秒数。无时间戳返回 None。"""
        ts = self.field_ts.get(key)
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return None

    def is_stale(self, key: str, max_age: float) -> bool:
        """字段是否陈旧（超过 max_age 秒，或从无数据）。
        无时间戳视为陈旧（保守：宁可当不可用，不可当正常）。"""
        age = self.age_of(key)
        if age is None:
            return True
        return age > max_age


@dataclass
class AnomalyRule:
    """异常检测规则"""
    id: str
    name: str
    priority: Priority
    cooldown_seconds: int
    condition: Callable[["AnomalyRule", DataSnapshot], bool]
    message_template: str
    message_prompt_extra: str = ""  # LLM 润色时附加的上下文

    def evaluate(self, snapshot: DataSnapshot) -> bool:
        try:
            return self.condition(self, snapshot)
        except Exception as e:
            logger.warning(f"规则 {self.id} 评估异常: {e}")
            return False


class AnomalyDetector:
    """异常检测规则引擎"""

    def __init__(self, health_store=None):
        self._rules: List[AnomalyRule] = []
        self._health_store = health_store  # 用于查询冷却
        self._baseline_vars: Dict[str, Any] = {}
        self._register_rules()

    def set_baseline_context(self, baseline_vars: Dict[str, Any]):
        """注入行为基线变量，供规则 eval 命名空间使用。
        Phase 2: sentinel._tick() 调用 check() 后设置。
        """
        self._baseline_vars = baseline_vars

    # ── 规则注册 ──────────────────────────────────────

    def _register_rules(self):
        self._rules = self._load_rules_from_json()

    # ── 从 JSON 加载规则 ──────────────────────────────

    def _load_rules_from_json(self) -> List[AnomalyRule]:
        import json, os
        config_path = os.path.join(os.path.dirname(__file__), "..", "rules.json")
        if not os.path.exists(config_path):
            logger.warning("rules.json 未找到，使用空规则集")
            return []

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        rules = []
        for r in config.get("rules", []):
            cond_str = r["condition"]
            # 编译 condition 字符串为可调用对象
            def make_condition(expr: str):
                detector = self  # closure
                def _eval(_rule, snapshot: DataSnapshot) -> bool:
                    try:
                        # 基线变量（Phase 2: behavior_profile.check() 注入）
                        bv = detector._baseline_vars if hasattr(detector, '_baseline_vars') else {}
                        # 心率时效门控：陈旧(>15min无新鲜读数)时置 None/0，规则短路不误报
                        hr_stale = snapshot.is_stale("heart_rate", HR_MAX_AGE)
                        ns = {
                            "heart_rate": (None if hr_stale else snapshot.heart_rate),
                            "screen_active": snapshot.screen_active,
                            "mirrow_visible": snapshot.mirrow_visible,
                            "user_status": snapshot.user_status,
                            "is_period": snapshot.is_period,
                            "period_day": snapshot.period_day,
                            "hr_avg_5min": (None if hr_stale else snapshot.hr_avg_5min),
                            "hr_avg_prev_5min": (None if hr_stale else snapshot.hr_avg_prev_5min),
                            "hr_count_3min": snapshot.hr_count_3min,
                            "hr_count_5min": snapshot.hr_count_5min,
                            "last_message_seconds": snapshot.last_message_seconds,
                            "hr_sustained_high_count": (0 if hr_stale else snapshot.hr_sustained_high_count),
                            "steps_today": snapshot.steps_today,
                            "input_idle_seconds": snapshot.input_idle_seconds,
                            "behavior_state": snapshot.behavior_state,
                            # 位置（GPS 陈旧>30min 时置 None，不用陈旧值判异常）
                            "location_category": (None if snapshot.is_stale("gps", 1800) else snapshot.location_category),
                            "__work_hours__": AnomalyDetector._is_work_hours(),
                            "wfh_today": AnomalyDetector._is_wfh_today(),
                            "__late_night__": AnomalyDetector._is_late_night(),
                            "__sleep_hours__": AnomalyDetector._is_sleep_hours(),
                            "__toy_recent_1800__": AnomalyDetector._was_toy_used_recently(1800),
                            # 基线变量
                            "resting_hr": bv.get("resting_hr", 65),
                            "hr_baseline_mean": bv.get("hr_baseline_mean", 0),
                            "hr_baseline_stddev": bv.get("hr_baseline_stddev", 10),
                            "steps_baseline_mean": bv.get("steps_baseline_mean", 0),
                            "is_cold_start": bv.get("is_cold_start", True),
                        }
                        return bool(eval(expr, {"__builtins__": {}}, ns))
                    except Exception:
                        return False
                return _eval

            rules.append(AnomalyRule(
                id=r["id"], name=r["name"],
                priority=Priority(r["priority"]),
                cooldown_seconds=r["cooldown_seconds"],
                condition=make_condition(cond_str),
                message_template=r["message_template"],
            ))

        logger.info(f"AnomalyDetector: 从 rules.json 加载 {len(rules)} 条规则")
        return rules

    # ── 评估入口 ──────────────────────────────────────

    async def evaluate(
        self, snapshot: DataSnapshot
    ) -> List[Dict[str, Any]]:
        """评估所有规则，返回应推送的告警列表"""
        triggered = []
        now = datetime.now()

        for rule in self._rules:
            if not rule.evaluate(snapshot):
                continue

            # Low priority 仅记录不推送（也走冷却，防刷屏）
            if rule.priority == Priority.LOW:
                if not self._cooldown_passed(rule, now):
                    continue
                self._log_only(rule, snapshot)
                continue

            # 冷却检查
            if not self._cooldown_passed(rule, now):
                logger.debug(f"规则 {rule.id} 命中但冷却中")
                continue

            # Medium priority 额外门控：需要用户在屏幕前或 idle
            if rule.priority == Priority.MEDIUM:
                if not (snapshot.mirrow_visible or snapshot.user_status == "idle"):
                    continue

            # 通过
            triggered.append({
                "rule_id": rule.id,
                "name": rule.name,
                "priority": rule.priority.value,
                "message": self._fill_template(rule.message_template, snapshot),
                "message_prompt_extra": rule.message_prompt_extra,
            })
            logger.info(f"Sentinel 规则触发: {rule.id} {rule.name} (priority={rule.priority.value})")

        return triggered

    # ── 冷却 ──────────────────────────────────────────

    def _cooldown_passed(self, rule: AnomalyRule, now: datetime) -> bool:
        if self._health_store is None:
            return True
        last_time = self._health_store.get_last_push_time(rule.id)
        if last_time is None:
            return True
        try:
            last_dt = datetime.fromisoformat(last_time)
            return (now - last_dt).total_seconds() >= rule.cooldown_seconds
        except Exception:
            return True

    def _log_only(self, rule: AnomalyRule, snapshot: DataSnapshot):
        """Low priority 规则仅记录到数据库"""
        if self._health_store:
            self._health_store.insert_event(
                rule_id=rule.id,
                priority=rule.priority.value,
                message=None,
                pushed=False,
            )

    # ── 模板填充 ──────────────────────────────────────

    def _fill_template(self, template: str, snapshot: DataSnapshot) -> str:
        """用快照数据 + 基线变量填充消息模板中的 {vars}"""
        bv = self._baseline_vars if hasattr(self, '_baseline_vars') else {}
        mapping = {
            "heart_rate": str(snapshot.heart_rate) if snapshot.heart_rate else "?",
            "avg_hr": str(snapshot.hr_avg_5min) if snapshot.hr_avg_5min else "?",
            "prev_avg": str(snapshot.hr_avg_prev_5min) if snapshot.hr_avg_prev_5min else "?",
            "period_day": str(snapshot.period_day) if snapshot.period_day else "?",
            "hour": str(datetime.now().hour),
            "steps_today": str(snapshot.steps_today) if snapshot.steps_today else "?",
            "steps_baseline_mean": str(int(bv.get("steps_baseline_mean", 0))) if bv.get("steps_baseline_mean", 0) > 0 else "?",
            "baseline_mean": str(int(bv.get("hr_baseline_mean", 0))) if bv.get("hr_baseline_mean", 0) > 0 else "?",
        }
        result = template
        for key, val in mapping.items():
            result = result.replace("{" + key + "}", val)
        return result

    # ── 严重程度乘数 ──────────────────────────────────

    def _get_severity_multiplier(self, snapshot: DataSnapshot) -> float:
        """
        基于对话间隔计算严重程度乘数。
        同一 HR>100 规则：
        - 最后消息 <5min → 可能刚提到运动 → 0.7 (降级)
        - 最后消息 >1h → 静默+心率高 → 1.0 (保留)
        - 最后消息 >3h + 凌晨 → 1.5 (升级)
        """
        gap = snapshot.last_message_seconds
        multiplier = 1.0

        if gap < 300:  # <5min
            multiplier = 0.7
        elif gap > 10800 and self._is_late_night():  # >3h + 凌晨
            multiplier = 1.5
        elif gap > 3600:  # >1h
            multiplier = 1.0

        return multiplier

    # ── 辅助方法 ──────────────────────────────────────

    @staticmethod
    def _is_late_night() -> bool:
        hour = datetime.now().hour
        return 2 <= hour < 6

    @staticmethod
    def _is_sleep_hours() -> bool:
        hour = datetime.now().hour
        return hour < 7 or hour >= 23

    @staticmethod
    def _is_work_hours() -> bool:
        """工作时段判断：非休息日 + 在 user_schedule 工作窗口内。"""
        try:
            now = datetime.now()
            # 休息日不算工作时段
            try:
                from calendar_manager.database import is_rest_day_today
                if is_rest_day_today():
                    return False
            except Exception:
                pass
            # 读作息表
            from mirrow_core.settings_manager import get_setting
            sched = get_setting("user_schedule") or {}
            ws = sched.get("work_start", "09:00")
            we = sched.get("work_end", "18:00")
            cur = now.strftime("%H:%M")
            return ws <= cur <= we
        except Exception:
            return False

    @staticmethod
    def _is_wfh_today() -> bool:
        """今日是否勾选了居家办公（带日期，次日自动失效）。"""
        try:
            from mirrow_core.settings_manager import get_setting
            sp = get_setting("silicon_perception") or {}
            wfh_date = sp.get("wfh_date")
            return wfh_date == datetime.now().strftime("%Y-%m-%d")
        except Exception:
            return False

    # ── 跨快照状态 ────────────────────────────────────

    _last_toy_use_time: Optional[datetime] = None

    @classmethod
    def mark_toy_used(cls):
        """外部调用：标记玩具刚被使用"""
        cls._last_toy_use_time = datetime.now()

    @classmethod
    def _was_toy_used_recently(cls, seconds: int) -> bool:
        if cls._last_toy_use_time is None:
            return False
        return (datetime.now() - cls._last_toy_use_time).total_seconds() < seconds
