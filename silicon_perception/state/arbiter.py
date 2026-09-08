"""信号仲裁器 — 100% 铁律的落地核心。

信号分级：
- T1 物理铁证：GPS 分类、键鼠 idle、PC 前台进程名、NFC（future）
- T2 设备活动：手机前台 App、屏幕方差、behavior_state
- T3 用户声明：user 手动 status

100% 铁律 = 仲裁输出规则：只有"有 T1 铁证 且 无矛盾"才输出确定状态（confidence=1.0），
否则输出 presence=unknown（confidence=0），绝不猜。

时效：PC 本地信号（idle/screen/foreground_exe）每 tick 同步新采，无 field_ts，present 即新鲜；
手机信号（gps/current_app）有 field_ts，用 snapshot.is_stale() 判陈旧。
"""

from __future__ import annotations

import logging
from typing import Optional

from .model import Presence, Activity, StateVector, SignalEvidence

logger = logging.getLogger(__name__)

# 手机信号最大有效期（约 3× 轮询间隔）
_GPS_MAX_AGE = 1800      # GPS 10min 轮询 → 30min
_CURRENT_APP_MAX_AGE = 360  # current_app 2min 轮询 → 6min


def arbitrate(snapshot, thresholds, catalog) -> StateVector:
    """从一次快照产出候选 StateVector（instantaneous，铁证或 unknown）。

    只做 presence 自动 + coding/gaming activity 候选。sleeping/napping（声明）由 engine 处理。
    activity=coding 的"持续时长"要求由 engine 的防抖器落实，本函数只给瞬时候选。
    """
    t = thresholds
    idle_active = t.resolve("idle_active_sec")
    idle_away = t.resolve("idle_away_sec")

    # ---- PC 本地信号（present 即新鲜）----
    idle = getattr(snapshot, "input_idle_seconds", None)
    screen_active = getattr(snapshot, "screen_active", None)
    fg_exe = getattr(snapshot, "foreground_exe", None)
    pc_available = idle is not None
    pc_active = pc_available and idle < idle_active

    # ---- 手机信号（查时效）----
    gps_cat = None
    if not snapshot.is_stale("gps", _GPS_MAX_AGE):
        gps_cat = getattr(snapshot, "location_category", None)
    mobile_on = None
    mobile_app = None
    mobile_pkg = None
    if not snapshot.is_stale("current_app", _CURRENT_APP_MAX_AGE):
        mobile_on = getattr(snapshot, "mobile_screen_on", None)
        mobile_app = getattr(snapshot, "mobile_app_name", None)
        mobile_pkg = getattr(snapshot, "mobile_app_package", None)
    phone_active = mobile_on is True and mobile_app is not None

    ev = []

    # ── 矛盾检测（100% 铁律核心）──
    # GPS 说在外 + PC 键鼠活跃 = 不可能（人不能一边在外一边敲家里/公司PC……除非在公司PC，
    # 但 work 分类正是公司，故 work+PC活跃是一致的；仅 elsewhere+PC活跃才矛盾）
    if gps_cat == "elsewhere" and pc_active:
        return _unknown("gps_elsewhere_but_pc_active")

    # ── OUT：GPS 物理铁证人不在家 ──
    if gps_cat in ("work", "elsewhere"):
        # 双重确认：PC 不活跃（或 PC 不可用）。PC 活跃且 work → 认定在公司电脑前（见下 at_computer）
        if gps_cat == "elsewhere" or not pc_active:
            ev.append(SignalEvidence("gps", gps_cat, 1, snapshot.field_ts.get("gps")))
            sv = StateVector(presence=Presence.OUT, confidence=1.0, evidence=ev,
                             reason_code=f"gps_{gps_cat}")
            _apply_activity(sv, fg_exe, mobile_pkg, catalog, ev, pc_active, phone_active)
            return sv

    # ── AT_COMPUTER：键鼠活跃（T1 输入时序铁证）──
    if pc_active:
        ev.append(SignalEvidence("input_idle", idle, 1))
        sv = StateVector(presence=Presence.AT_COMPUTER, confidence=1.0, evidence=ev,
                         reason_code="pc_active")
        _apply_activity(sv, fg_exe, mobile_pkg, catalog, ev, pc_active, phone_active)
        return sv

    # ── ON_PHONE：手机在用 + PC 不活跃 ──
    if phone_active and (not pc_available or idle >= idle_active):
        ev.append(SignalEvidence("current_app", mobile_app, 2, snapshot.field_ts.get("current_app")))
        sv = StateVector(presence=Presence.ON_PHONE, confidence=1.0, evidence=ev,
                         reason_code="phone_active")
        _apply_activity(sv, fg_exe, mobile_pkg, catalog, ev, pc_active, phone_active)
        return sv

    # ── AWAY：键鼠长空闲 + 屏幕不活跃 + 手机灭屏 ──
    if pc_available and idle >= idle_away and screen_active is not True:
        phone_off = (mobile_on is not True)
        if phone_off:
            ev.append(SignalEvidence("input_idle", idle, 1))
            return StateVector(presence=Presence.AWAY, confidence=1.0, evidence=ev,
                               reason_code="idle_and_devices_off")

    # ── 其余：无法 100% 确定 ──
    return _unknown("insufficient_or_ambiguous")


def _apply_activity(sv: StateVector, fg_exe, mobile_pkg, catalog, ev, pc_active, phone_active):
    """在已确定 presence 的基础上叠加 activity 候选（coding/gaming）。

    进程级复判（auto 模式）：有确认游戏进程在跑 → 直接 gaming，不管前台是什么。
    coding 仅在 PC 活跃时成立；gaming 可来自 PC 前台或手机前台。
    activity 的持续时长要求由 engine 防抖器落实，这里只标瞬时候选。
    """
    # 进程级复判：后台有确认游戏进程 → 直接 gaming（不靠前台窗口）
    if catalog.is_any_game_running():
        sv.activity = Activity.GAMING
        ev.append(SignalEvidence("game_process", "running", 1))
        return

    # PC 前台分类
    if pc_active and fg_exe:
        cls = catalog.classify(fg_exe)
        if cls == "coding":
            sv.activity = Activity.CODING
            ev.append(SignalEvidence("foreground_exe", fg_exe, 1))
            return
        if cls == "gaming":
            sv.activity = Activity.GAMING
            ev.append(SignalEvidence("foreground_exe", fg_exe, 1))
            return
    # 手机前台分类（手游）
    if phone_active and mobile_pkg:
        cls = catalog.classify(mobile_pkg)
        if cls == "gaming":
            sv.activity = Activity.GAMING
            ev.append(SignalEvidence("mobile_app_package", mobile_pkg, 2))


def _unknown(reason: str) -> StateVector:
    return StateVector(presence=Presence.UNKNOWN, confidence=0.0, reason_code=reason)
