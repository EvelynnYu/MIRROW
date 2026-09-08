"""Flash 触发器（v4 — 2026-07-05 重构）
三级唤醒体系：
  L1: 快照对比 — 对比当前 tick 和上一 tick 的 DataSnapshot，值变化 → 事件
  L2: 救命规则 — C1/C2，在 sentinel._tick_inner 中直接推送，不经过这里
  L3: 保底定时 — 30min 无触发则主动唤醒

零冷却：L1 事件不做冷却，由 Flash 自行判断是否 PUSH。
数据来源：DataSnapshot（与 health_snapshots 同源），零额外 DB 查询。
"""

import logging
import time
from typing import Optional, List
from datetime import datetime

# 设备类别判断从 behavior_state 导入 — 状态字符串格式的唯一权威在那边，
# 本模块不做任何字面量前缀匹配（文案与逻辑解耦，改用户称呼不影响触发器）。
from silicon_perception.analysis.behavior_state import behavior_category as _behavior_category

logger = logging.getLogger(__name__)


class TriggerDetector:
    """L1 快照对比 + L3 保底定时 — 收集唤醒 Flash 的触发事件"""

    def __init__(self):
        self._last: Optional['DataSnapshot'] = None  # 上一 tick 的快照
        self._last_hr_5min: Optional[int] = None  # 5min 前心率（用于骤升骤降检测）
        self._last_flash_time: float = time.monotonic()  # 初始化为当前时间，避免首次就触发 L3
        self._last_fired_triggers: List[dict] = []  # debug: 最近触发记录
        self._last_fire_time: dict = {}  # trigger_name → monotonic timestamp，冷却追踪

    # ── 主入口 ──────────────────────────────────────

    def evaluate(self, snapshot: 'DataSnapshot', hr_stats_5min: dict = None) -> List[str]:
        """每个 tick 调用。返回触发事件列表。"""
        now = time.monotonic()
        triggers: List[str] = []

        # ── L1: 快照对比（首次跳过，无上次数据）──
        # 心率异常已移交给 anomaly_detector C1/C2 直推，这里不再检测
        if self._last is not None:
            self._detect_steps(snapshot, triggers)
            self._detect_gps(snapshot, triggers)
            self._detect_behavior(snapshot, triggers)
            self._detect_app_session(snapshot, triggers)
            self._detect_user_status(snapshot, triggers)

        # ── L3: 保底定时 ──
        if now - self._last_flash_time > 1800:  # 30min
            triggers.append('periodic_check')

        # 保存当前快照供下次对比
        self._last = snapshot

        if triggers:
            self._last_flash_time = now
            self._last_fired_triggers.insert(0, {
                "time": datetime.now().isoformat(),
                "triggers": triggers,
            })
            if len(self._last_fired_triggers) > 10:
                self._last_fired_triggers = self._last_fired_triggers[:10]
            logger.info(f"TriggerDetector: {triggers}")

        return triggers

    def _can_fire(self, name: str, cooldown_sec: float = 600) -> bool:
        """检查冷却：name 类触发在 cooldown_sec 秒内只允许一次。"""
        now = time.monotonic()
        last = self._last_fire_time.get(name, 0)
        if now - last < cooldown_sec:
            return False
        self._last_fire_time[name] = now
        return True

    # ── L1 检测器 ──────────────────────────────────

    # _detect_hr 已删除 — 心率异常完全交给 anomaly_detector 的 C1/C2 救命规则直推。
    # trigger_detector+Flash 专管行为/步数/GPS/状态等软信号，不再碰心率，消除双推重叠。

    def _detect_steps(self, snap, triggers):
        """步数异常：暴增/停滞"""
        steps = snap.steps_today
        if steps is None:
            return

        # 暴增
        if self._last and self._last.steps_today is not None:
            delta = steps - self._last.steps_today
            if delta > 500:
                triggers.append('steps_burst')

        # 停滞（仅当跨过 30min 阈值时触发一次，不重复）
        stagnant = getattr(snap, 'steps_stagnant_minutes', 0) or 0
        prev_stagnant = getattr(self._last, 'steps_stagnant_minutes', 0) or 0
        if stagnant >= 30 and prev_stagnant < 30:
            triggers.append('steps_stagnant')

    def _detect_gps(self, snap, triggers):
        """GPS 变化：移动/分类切换"""
        # 分类切换
        if snap.location_category and snap.location_category != (self._last.location_category if self._last else None):
            if self._last and self._last.location_category is not None:
                triggers.append('gps_category_change')

        # 显著移动（>500m）
        if (snap.location_lat and snap.location_lng
                and self._last and self._last.location_lat and self._last.location_lng):
            try:
                from silicon_perception.collection.gps import GpsSource
                dist = GpsSource._haversine(
                    self._last.location_lat, self._last.location_lng,
                    snap.location_lat, snap.location_lng,
                )
                if dist > 500:
                    triggers.append('gps_moved')
            except Exception:
                pass

    def _detect_behavior(self, snap, triggers):
        """行为状态变化：设备类别切换（同类别内 App 切换不触发）。
        使用前缀匹配提取类别：pc/phone/both/pc_idle/phone_idle/away/unknown。
        10min 冷却：同类触发间隔过短的不重复唤醒 Flash。
        """
        cur_cat = _behavior_category(snap.behavior_state)
        prev_cat = _behavior_category(getattr(self._last, 'behavior_state', None))
        if cur_cat != prev_cat and cur_cat != "unknown":
            if self._can_fire('behavior_state_change', cooldown_sec=600):
                triggers.append('behavior_state_change')

    def _detect_app_session(self, snap, triggers):
        """单 App 持续过久（>3h，仅跨阈值触发一次）→ 可能是久坐"""
        session = getattr(snap, 'active_app_session', None)
        if not session:
            return
        for part in session.split(" + "):
            try:
                if "(" in part and part.endswith(")min)"):
                    mins = int(part.split("(")[-1].replace("min)", ""))
                    # 跨阈值守卫：检查上一 tick 是否也超了 180
                    prev_mins = 0
                    prev_session = getattr(self._last, 'active_app_session', None)
                    if prev_session:
                        for pp in prev_session.split(" + "):
                            try:
                                if "(" in pp and pp.endswith(")min)"):
                                    prev_mins = max(prev_mins, int(pp.split("(")[-1].replace("min)", "")))
                            except Exception:
                                pass
                    if mins > 180 and prev_mins < 180:
                        triggers.append('app_session_long')
                    return
            except Exception:
                pass

    def _detect_user_status(self, snap, triggers):
        """用户状态变更 + 沉默"""
        if (snap.user_status
                and snap.user_status != getattr(self._last, 'user_status', None)
                and snap.last_message_seconds > 180):
            triggers.append('status_change_silent')
