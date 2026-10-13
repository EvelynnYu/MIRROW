"""Flash 触发器（v4 — 2026-07-05 重构）
三级唤醒体系：
  L1: 快照对比 — 对比当前 tick 和上一 tick 的 DataSnapshot，值变化 → 事件
  L2: 救命规则 — C1/C2，在 sentinel._tick_inner 中直接推送，不经过这里
  L3: 保底定时 — 普通状态 2h、睡眠/小憩 4h 无触发才唤醒

行为状态变化先经过稳定窗口和同类冷却，再交给 Flash 判断是否 PUSH；
其余 L1 事件仍按各自的边沿/冷却规则处理。
数据来源：DataSnapshot（与 health_snapshots 同源），零额外 DB 查询。
"""

import logging
import time
from typing import Optional, List
from datetime import datetime

from .behavior_state import behavior_category as _behavior_category
from .location_episode import LocationEpisode

logger = logging.getLogger(__name__)


_PERIODIC_ACTIVE_SECONDS = 2 * 60 * 60
_PERIODIC_REST_SECONDS = 4 * 60 * 60
_REST_STATUSES = {"sleeping", "napping"}
_BEHAVIOR_STABLE_SECONDS = 3 * 60
_BEHAVIOR_CHANGE_COOLDOWN_SECONDS = 30 * 60
_REST_RECHECK_SECONDS = 10 * 60
_STATUS_STABLE_SECONDS = 3 * 60


def periodic_check_interval_seconds(snapshot=None) -> int:
    """返回当前 L3 保底间隔；真实变化仍由 L1 即时唤醒。"""
    status = str(getattr(snapshot, "user_status", "") or "").strip().lower()
    return _PERIODIC_REST_SECONDS if status in _REST_STATUSES else _PERIODIC_ACTIVE_SECONDS

class TriggerDetector:
    """L1 快照对比 + L3 保底定时 — 收集唤醒 Flash 的触发事件"""

    def __init__(self):
        self._last: Optional['DataSnapshot'] = None  # 上一 tick 的快照
        self._last_hr_5min: Optional[int] = None  # 5min 前心率（用于骤升骤降检测）
        self._last_flash_time: float = time.monotonic()  # 初始化为当前时间，避免首次就触发 L3
        self._last_fired_triggers: List[dict] = []  # debug: 最近触发记录
        self._last_fire_time: dict = {}  # trigger_name → monotonic timestamp，冷却追踪
        self._confirmed_behavior_category: Optional[str] = None
        self._pending_behavior_category: Optional[str] = None
        self._pending_behavior_since: Optional[float] = None
        self._rest_recheck: Optional[dict] = None
        self._confirmed_user_status = None
        self._pending_user_status = None
        self._pending_user_status_since = None
        self._location_episode = LocationEpisode()

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
            self._detect_wifi(snapshot, triggers)
            self._detect_behavior(snapshot, triggers)
            self._detect_app_session(snapshot, triggers)
            self._detect_user_status(snapshot, triggers)
            self._detect_semantic_observations(snapshot, triggers)
        else:
            self._confirmed_user_status = snapshot.user_status or None
            initial_category = _behavior_category(getattr(snapshot, 'behavior_state', None))
            if initial_category != "unknown":
                self._confirmed_behavior_category = initial_category

        # ── L3: 保底定时 ──
        # 保底只负责在长期无事件时偶尔让 Flash 重新看一眼；传感器变化仍由
        # L1 即时唤醒，因此没有必要每 30 分钟制造固定注意力和调用成本。
        if now - self._last_flash_time > periodic_check_interval_seconds(snapshot):
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

        from .public_diagnostics import record_neuron_execution
        record_neuron_execution("trigger_detector", status="ok",
                                output_preview=f"触发: {','.join(triggers)}" if triggers else "(无触发)")

        return triggers

    def _can_fire(self, name: str, cooldown_sec: float = 600) -> bool:
        """检查冷却：name 类触发在 cooldown_sec 秒内只允许一次。"""
        now = time.monotonic()
        last = self._last_fire_time.get(name)
        if last is not None and now - last < cooldown_sec:
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
        """独立定位样本组成移动段；持续位移不重复唤醒。"""
        triggers.extend(self._location_episode.observe(snap, previous=self._last))

    def _detect_wifi(self, snap, triggers):
        """WiFi 连接变化：连上家 WiFi → 回家信号"""
        if self._last is None:
            return
        was_home = getattr(self._last, 'wifi_is_home', False)
        is_home = getattr(snap, 'wifi_is_home', False)
        previous_serial = int(getattr(self._last, 'wifi_transition_serial', 0) or 0)
        current_serial = int(getattr(snap, 'wifi_transition_serial', 0) or 0)
        if is_home and not was_home and current_serial > previous_serial:
            triggers.append('wifi_home_arrival')

    def _detect_behavior(self, snap, triggers):
        """行为状态变化：设备类别切换（同类别内 App 切换不触发）。
        使用前缀匹配提取类别：pc/phone/both/pc_idle/phone_idle/away/unknown。
        新类别必须连续稳定 3min 才确认；确认后的同类变化 30min 内不重复
        唤醒 Flash。短暂切换或传感器抖动只更新候选态，不构成真实变化。
        """
        cur_cat = _behavior_category(snap.behavior_state)
        prev_cat = _behavior_category(getattr(self._last, 'behavior_state', None))
        # 手机数据源刚恢复/刚断开时，pc↔phone 只是可见性变化，不是人类伙伴真的切换设备。
        # 两侧都存在新鲜 current_app 观测，才允许包含 phone/both 的行为变化唤醒 Flash。
        phone_involved = cur_cat in {"phone", "phone_idle", "both"} or prev_cat in {
            "phone", "phone_idle", "both",
        }
        if phone_involved:
            prev_phone_known = not self._last.is_stale("current_app", 360)
            cur_phone_known = not snap.is_stale("current_app", 360)
            if not (prev_phone_known and cur_phone_known):
                self._pending_behavior_category = None
                self._pending_behavior_since = None
                return
        if cur_cat == "unknown":
            self._pending_behavior_category = None
            self._pending_behavior_since = None
            return

        confirmed = self._confirmed_behavior_category
        if confirmed is None:
            confirmed = prev_cat if prev_cat != "unknown" else cur_cat
            self._confirmed_behavior_category = confirmed

        if cur_cat == confirmed:
            self._pending_behavior_category = None
            self._pending_behavior_since = None
            return

        now = time.monotonic()
        if self._pending_behavior_category != cur_cat:
            self._pending_behavior_category = cur_cat
            self._pending_behavior_since = now
            return

        pending_since = self._pending_behavior_since
        if pending_since is None or now - pending_since < _BEHAVIOR_STABLE_SECONDS:
            return

        # 到达稳定窗口后才推进确认态。即便同类唤醒仍在冷却，也要接受这个
        # 已经稳定的新状态，避免冷却结束后把旧变化误报成刚刚发生。
        previous_confirmed = confirmed
        self._confirmed_behavior_category = cur_cat
        self._pending_behavior_category = None
        self._pending_behavior_since = None
        if self._can_fire(
            'behavior_state_change',
            cooldown_sec=_BEHAVIOR_CHANGE_COOLDOWN_SECONDS,
        ):
            # 编码前后态（"从X变到Y"），Flash/摘要据此理解是"同时用电脑+手机 → 只用手机"这类变化
            triggers.append(f'behavior_state_change:{previous_confirmed}->{cur_cat}')

    def _detect_app_session(self, snap, triggers):
        """单 App 持续过久（>3h，仅跨阈值触发一次）→ 可能是久坐"""
        def durations(value):
            result = {}
            for part in (value or "").split(" + "):
                if "(" not in part or not part.endswith("min)"):
                    continue
                name, duration = part.rsplit("(", 1)
                try:
                    result[name] = max(result.get(name, 0), int(duration[:-4]))
                except ValueError:
                    continue
            return result

        current = durations(getattr(snap, 'active_app_session', None))
        previous = durations(getattr(self._last, 'active_app_session', None))
        if any(minutes > 180 and previous.get(name, 0) <= 180
               for name, minutes in current.items()):
            triggers.append('app_session_long')

    def _detect_user_status(self, snap, triggers):
        """状态连续稳定后再唤醒；同类冷却不干预 Flash 的联系决定。"""
        status = snap.user_status
        if not status or status == self._confirmed_user_status:
            self._pending_user_status = None
            self._pending_user_status_since = None
            return
        if self._confirmed_user_status is None:
            self._confirmed_user_status = status
            return
        now = time.monotonic()
        if status != self._pending_user_status:
            self._pending_user_status = status
            self._pending_user_status_since = now
            return
        if now - self._pending_user_status_since < _STATUS_STABLE_SECONDS:
            return
        self._confirmed_user_status = status
        self._pending_user_status = None
        self._pending_user_status_since = None
        if snap.last_message_seconds > 180 and self._can_fire('status_change_silent'):
            triggers.append('status_change_silent')

    def _detect_semantic_observations(self, snap, triggers):
        """Wake Flash once when a sustained cross-source state becomes notable.

        The observation builder owns facts; this detector only notices the
        false->true edge.  Flash remains responsible for deciding whether AI
        should say anything.
        """
        current = {
            str(item.get("key")): str(item.get("state"))
            for item in (getattr(snap, "semantic_observations", None) or [])
            if isinstance(item, dict) and item.get("key") and item.get("attention_eligible") is True
        }
        previous = {
            str(item.get("key")): str(item.get("state"))
            for item in (getattr(self._last, "semantic_observations", None) or [])
            if isinstance(item, dict) and item.get("key") and item.get("attention_eligible") is True
        }
        for key, state in current.items():
            if previous.get(key) == state:
                continue
            trigger = f"cross_source_attention:{state}"
            if self._can_fire(trigger, cooldown_sec=30 * 60):
                triggers.append(trigger)

        # First-layer judgment may have said no while evidence was still weak.
        # One later look is allowed only for the same rest declaration and while
        # a fresh, attention-eligible observation still exists. It never pushes.
        pending = self._rest_recheck
        declaration = getattr(snap, "user_status_since", None)
        if pending and (
            getattr(snap, "user_status", None) not in _REST_STATUSES
            or declaration != pending["declaration"]
        ):
            self._rest_recheck = None
            pending = None
        if pending and not pending["used"] and current.get(pending["key"]) == pending["state"]:
            if time.monotonic() - pending["judged_at"] >= _REST_RECHECK_SECONDS:
                triggers.append(f"cross_source_recheck:{pending['state']}")
                pending["used"] = True

    def record_flash_decision(self, snapshot, triggers: List[str], should_push: bool) -> None:
        """Remember only a declined rest comparison for one bounded recheck."""
        if should_push and self._rest_recheck is not None:
            self._rest_recheck = None
        states = [
            trigger.split(":", 1)[1]
            for trigger in triggers
            if trigger.startswith("cross_source_attention:rest_declaration_with_")
        ]
        if not states:
            return
        if should_push:
            return
        self._rest_recheck = {
            "declaration": getattr(snapshot, "user_status_since", None),
            "key": "rest_declaration_alignment",
            "state": states[0],
            "judged_at": time.monotonic(),
            "used": False,
        }
