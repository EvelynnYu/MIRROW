# 用户状态管理
#
# 用户可从前端选择9种状态：游戏/外出/洗澡/吃饭/小憩/睡眠/空闲/其他/写代码
# 影响：1)漫想事件概率分布  2)LLM上下文提示词  3)思念浓度计算
# 空闲为默认模式，固定描述"用户不知道在干嘛"
#
# === StatusMeta 层级模型（2026-08-01） ===
# 9 个 legacy enum 值是扁平的，但实际存在「在家/外出」两级分类：
#   在家: gaming, coding, bathing, eating, napping, sleeping, idle, other
#   外出: out
# eating 特殊 —— 可在家也可外出，歧义由 StatusMeta.location 承载。
# StateEngine（state/model.py）已将状态拆为 Presence×Activity×RestMode 三维，
# StatusMeta 把这份已有数据暴露给所有 consumer（漫想/上下文/哨兵/情绪/…）。
# legacy enum 值不变 —— 存量字符串比较全兼容。

import os
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# 持久化文件路径（与 last_reply_time.json 同目录）
_USER_STATUS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "user_status.json"
)


class UserStatus(Enum):
    """用户前端状态"""
    GAMING = "gaming"       # 玩游戏
    OUT = "out"             # 外出/工作（外出工作）
    BATHING = "bathing"     # 洗澡
    EATING = "eating"       # 吃饭
    NAPPING = "napping"     # 小憩（临时休息）
    SLEEPING = "sleeping"   # 睡眠（正式休息）
    IDLE = "idle"           # 空闲（默认，固定描述）
    OTHER = "other"         # 其他行程
    CODING = "coding"       # 写代码


_user_status: UserStatus = UserStatus.IDLE
_user_status_custom_text: str = ""
_status_history: list = []  # [{time: "ISO", from: "idle", to: "gaming"}, ...]

# 固定描述（IDLE 专用）
IDLE_FIXED_DESCRIPTION = "用户不知道在干嘛"

# 状态显示名称映射
STATUS_DISPLAY_NAMES = {
    UserStatus.GAMING: "游戏",
    UserStatus.OUT: "外出",
    UserStatus.BATHING: "洗澡",
    UserStatus.EATING: "吃饭",
    UserStatus.NAPPING: "小憩",
    UserStatus.SLEEPING: "睡眠",
    UserStatus.IDLE: "空闲",
    UserStatus.OTHER: "其他",
    UserStatus.CODING: "写代码",
}

# 仅有状态名时的默认描述（当用户未填写自定义文本时用）
DEFAULT_DESCRIPTIONS = {
    UserStatus.GAMING: "用户在玩游戏",
    UserStatus.OUT: "用户出门/上班了",
    UserStatus.BATHING: "用户正在洗澡",
    UserStatus.EATING: "用户正在吃饭",
    UserStatus.NAPPING: "用户正在小憩",
    UserStatus.SLEEPING: "用户正在睡觉",
    UserStatus.OTHER: "用户有其它行程",
    UserStatus.CODING: "用户在写代码",
}

# 快捷短语预设（前端 focus 输入框时弹出下拉，点击填入后可继续编辑）
STATUS_PRESETS = {
    "gaming": ["APEX", "黎明杀机"],
    "out": ["上班", "出去玩", "拍照", "回老家"],
    "eating": ["出门吃饭", "在家吃饭", "做饭", "公司食堂"],
    "bathing": ["日常洗澡", "事后洗澡"],
    "napping": ["办公室午休"],
    "other": ["做家务", "做手工"],
    "coding": [],
    "sleeping": [],
    "idle": [],
}


# ═══════════════════════════════════════════
# StatusMeta — 层级元数据（伴生 legacy string，不改 enum）
# ═══════════════════════════════════════════

# legacy → location 默认映射（无 StateVector 时的回退）
_LEGACY_LOCATION_DEFAULT = {
    "gaming": "home", "coding": "home", "bathing": "home",
    "eating": "home", "napping": "home", "sleeping": "home",
    "idle": "home", "other": "home",
    "out": "out",
}

# legacy → activity_category（去掉 location 维度后的纯活动分类）
_LEGACY_ACTIVITY_CATEGORY = {
    "gaming": "gaming", "coding": "coding", "bathing": "bathing",
    "eating": "eating", "napping": "napping", "sleeping": "sleeping",
    "idle": "idle", "other": "other", "out": "idle",
}

# legacy → rest_mode
_LEGACY_REST_MODE = {
    "sleeping": "sleeping", "napping": "napping",
}


@dataclass
class StatusMeta:
    """用户状态的层级元数据 —— 伴生 legacy string，不改 enum。

    StateEngine（state/model.py）已把状态拆为 Presence×Activity×RestMode 三维。
    bridge 已在向前端推送完整的 2D 数据。本 dataclass 把这份数据暴露给后端所有 consumer。
    """

    legacy: str              # "eating", "gaming", "out" ... (= UserStatus.value)
    location: str            # "home" | "out" | "unknown"
    activity_category: str   # "gaming"|"coding"|"eating"|"bathing"|"sleeping"|"napping"|"idle"|"other"
    rest_mode: str           # "sleeping" | "napping" | "none"
    source: str              # "auto" | "explicit" | "latent" | "unknown"

    @property
    def display_label(self) -> str:
        """前端/上下文用的层级标签，如 '在家·游戏' / '外出·吃饭' / '外出'"""
        loc = {"home": "在家", "out": "外出"}.get(self.location, "")
        if not loc:
            # unknown → 回退旧标签
            try:
                return STATUS_DISPLAY_NAMES.get(UserStatus(self.legacy), self.legacy)
            except Exception:
                return self.legacy
        act_map = {
            "gaming": "游戏", "coding": "代码", "eating": "吃饭",
            "bathing": "洗澡", "sleeping": "睡眠", "napping": "小憩",
            "idle": "空闲", "other": "其他",
        }
        act = act_map.get(self.activity_category, self.activity_category)
        if self.activity_category == "idle" and self.location == "out":
            return "外出"  # "外出·空闲" 太冗余
        if self.activity_category == "idle":
            return f"{loc}·空闲"
        return f"{loc}·{act}"

    @property
    def behavior_mode(self) -> str:
        """替代 sentinel.py:1254 的三元 hack。

        吃个饭不再永远 rest——在家吃=rest，外出吃=away。
        """
        if self.rest_mode in ("sleeping", "napping"):
            return "rest"
        if self.activity_category == "bathing":
            return "rest"
        if self.location == "out":
            return "away"
        if self.activity_category == "eating" and self.location == "home":
            return "rest"
        if self.activity_category == "eating" and self.location == "out":
            return "away"
        return "active"

    @classmethod
    def from_state_vector(cls, sv, source: str = "auto") -> "StatusMeta":
        """从 StateEngine 的 StateVector 派生（权威路径）。

        Args:
            sv: StateVector (from silicon_perception.state.model)
            source: "auto" | "explicit" | "latent"
        """
        legacy = sv.to_legacy()

        # location from presence
        presence_val = sv.presence.value if hasattr(sv.presence, 'value') else str(sv.presence)
        if presence_val == "out":
            location = "out"
        elif presence_val in ("at_computer", "on_phone", "away"):
            location = "home"
        else:
            location = "unknown"

        # activity_category: rest_mode > activity > presence-derived
        rest_val = sv.rest_mode.value if hasattr(sv.rest_mode, 'value') else str(sv.rest_mode)
        act_val = sv.activity.value if hasattr(sv.activity, 'value') else str(sv.activity)

        if rest_val != "none":
            activity_category = rest_val
        elif act_val != "none":
            activity_category = act_val
        elif presence_val == "out":
            activity_category = "idle"
        else:
            activity_category = "idle"

        return cls(
            legacy=legacy,
            location=location,
            activity_category=activity_category,
            rest_mode=rest_val if rest_val else "none",
            source=source,
        )

    @classmethod
    def from_legacy_only(cls, status_str: str) -> "StatusMeta":
        """回退：无 StateVector 时从 legacy string 推断。

        用于：手动声明 / 引擎未就绪 / 旧数据恢复。
        eating 默认 location="home"（Phase 6 用 GPS 新鲜值覆盖）。
        """
        location = _LEGACY_LOCATION_DEFAULT.get(status_str, "unknown")
        activity_category = _LEGACY_ACTIVITY_CATEGORY.get(status_str, "idle")
        rest_mode = _LEGACY_REST_MODE.get(status_str, "none")
        return cls(
            legacy=status_str,
            location=location,
            activity_category=activity_category,
            rest_mode=rest_mode,
            source="unknown",
        )

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容 dict（持久化用）。"""
        return {
            "legacy": self.legacy,
            "location": self.location,
            "activity_category": self.activity_category,
            "rest_mode": self.rest_mode,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StatusMeta":
        """从 dict 反序列化（持久化恢复用）。"""
        return cls(
            legacy=d.get("legacy", "idle"),
            location=d.get("location", "unknown"),
            activity_category=d.get("activity_category", "idle"),
            rest_mode=d.get("rest_mode", "none"),
            source=d.get("source", "unknown"),
        )


# 模块级 StatusMeta（与 _user_status 同步维护）
_status_meta: StatusMeta = StatusMeta.from_legacy_only("idle")


def get_status_meta() -> StatusMeta:
    """获取当前 StatusMeta（层级元数据）。"""
    return _status_meta


def set_status_meta(meta: StatusMeta):
    """设置 StatusMeta（由 bridge / manager / sentinel 调用）。"""
    global _status_meta
    _status_meta = meta


def get_status_presets() -> dict:
    """获取快捷短语预设（供前端渲染下拉选项）"""
    return dict(STATUS_PRESETS)


def get_user_status() -> UserStatus:
    return _user_status


def _record_status_change(new_status: UserStatus):
    """记录状态变更到履历（相同状态不重复记录）"""
    global _status_history
    if _user_status == new_status:
        return
    from datetime import datetime
    entry = {
        "time": datetime.now().isoformat(),
        "from": _user_status.value,
        "to": new_status.value
    }
    _status_history.append(entry)
    if len(_status_history) > 100:
        _status_history = _status_history[-100:]


def set_user_status(status: UserStatus):
    global _user_status, _user_status_custom_text
    _record_status_change(status)
    _user_status = status
    _user_status_custom_text = ""  # 状态变更时清除旧描述，防止"出门上班"残留至睡眠等不相关状态
    save_user_status()


def _derive_status_meta(status: UserStatus, source: str = "unknown") -> StatusMeta:
    """从 UserStatus 派生 StatusMeta（优先从 StateEngine 获取，回退 from_legacy_only）。

    FULL_HOLD 状态（eating/bathing）额外检查 GPS 新鲜值覆盖 location。
    """
    global _status_meta
    status_str = status.value if isinstance(status, UserStatus) else str(status)
    # 手动声明（explicit）时优先用 legacy 映射——用户的明确意图（如"外出"）不应被
    # 引擎 presence 未同步（还停在 at_computer）覆盖成 home，导致标签显示"空闲·上班"而非"外出·上班"
    if source == "explicit":
        meta = StatusMeta.from_legacy_only(status_str)
        meta.source = source
        override = _resolve_location_for_full_hold(status_str)
        if override:
            meta.location = override
        return meta
    # 1 尝试从 StateEngine 获取权威 StateVector
    try:
        from silicon_perception.state.engine import get_state_engine
        engine = get_state_engine()
        sv = engine.current()
        if sv and sv.to_legacy() == status_str:
            meta = StatusMeta.from_state_vector(sv, source)
            # FULL_HOLD 状态：GPS 新鲜值覆盖 location
            override = _resolve_location_for_full_hold(status_str)
            if override:
                meta.location = override
            return meta
    except Exception:
        pass
    # 2 回退：从 legacy string 推断
    meta = StatusMeta.from_legacy_only(status_str)
    meta.source = source
    # FULL_HOLD 状态：GPS 新鲜值覆盖 location
    override = _resolve_location_for_full_hold(status_str)
    if override:
        meta.location = override
    return meta


def set_user_status_combined(status: UserStatus, custom_text: str = ""):
    """原子写入：同时更新状态和自定义文本，单次写盘"""
    global _user_status, _user_status_custom_text, _status_meta
    if _user_status == status:
        # 同状态重复设置：仅更新自定义文本（允许用户编辑描述），不产生重复状态变更记录
        _user_status_custom_text = custom_text[:20]
        save_user_status()
        return
    _record_status_change(status)
    _user_status = status
    _user_status_custom_text = custom_text[:20]
    # 同步派生 StatusMeta（优先 StateEngine，回退 from_legacy_only）
    _status_meta = _derive_status_meta(status, "explicit")
    save_user_status()
    # 同步到 shared_state，供 sentinel 事件驱动读取（避免每 tick 轮询）
    try:
        from mirrow_core.shared_state import set_current_user_status, set_current_status_meta
        set_current_user_status(status.value)
        set_current_status_meta(_status_meta.to_dict())
    except Exception:
        pass


def _resolve_location_for_full_hold(legacy: str) -> Optional[str]:
    """FULL_HOLD 状态下，从 shared_state 读最新位置事实覆盖 location。

    engine.py 的 _FULL_HOLD 冻结了 eating/bathing 的引擎处理，
    位置变化不会触发 transition → StatusMeta.location 不更新。
    此函数在 set_user_status / set_status_meta 时被调用，
    shared_state 当前优先保存 WiFi 派生的 home/elsewhere；仅 WiFi 未确认时
    才保存 GPS 分类。

    Returns:
        "out" | "home" | None（位置事实陈旧/不可用 → None = 不覆盖）
    """
    if legacy not in ("eating", "bathing"):
        return None
    try:
        from mirrow_core.shared_state import get_current_gps_category, get_current_gps_age_seconds
        gps = get_current_gps_category()
        age = get_current_gps_age_seconds()
        if gps and age is not None and age < 600:
            if gps in ("elsewhere", "work"):
                return "out"
            elif gps == "home":
                return "home"
    except Exception:
        pass
    return None


def get_status_history(since: str = None, until: str = None) -> list:
    """获取状态变更履历，可按时间范围过滤。返回 [{time, from, to}, ...]"""
    if not since and not until:
        return list(_status_history)
    result = []
    for entry in _status_history:
        t = entry["time"]
        if since and t < since:
            continue
        if until and t > until:
            continue
        result.append(entry)
    return result


def _parse_naive(dt):
    """把 datetime 或 ISO 字符串统一转为 naive 本地时间，失败返回 None。"""
    from datetime import datetime
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _fmt_duration(seconds: float) -> str:
    """秒数转「X小时Y分钟」中文时长。"""
    total_m = int(seconds // 60)
    h = total_m // 60
    m = total_m % 60
    if h > 0 and m > 0:
        return f"{h}小时{m}分钟"
    if h > 0:
        return f"{h}小时"
    return f"{m}分钟"


def get_status_timeline_since(since_dt) -> list:
    """返回自 since_dt 起用户经历的状态分段（含各状态持续时长）。

    用于漫想：把「离开期间到底切换过哪些状态、各自多久」告诉 Pro，
    避免「睡眠8h→空闲1.5h」被按『距上次回复』误算成『离开10小时』。

    Returns:
        [{status, status_display, start(ISO), duration_seconds, is_current}, ...]
        按时间升序；忽略 <30s 的极短切换段。
    """
    from datetime import datetime
    now = datetime.now()
    since = _parse_naive(since_dt)
    if since is None or since > now:
        return []

    # 解析所有变更点（time 升序）
    trans = []
    for e in _status_history:
        t = _parse_naive(e.get("time", ""))
        if t is None:
            continue
        trans.append((t, e.get("to", ""), e.get("from", "")))
    trans.sort(key=lambda x: x[0])

    # 确定 since 时刻的活跃状态
    status_at_since = None
    for t, to_s, from_s in trans:
        if t <= since:
            status_at_since = to_s
        else:
            if status_at_since is None:
                status_at_since = from_s  # since 早于第一次变更 → 取该次的 from
            break
    if status_at_since is None:
        status_at_since = _user_status.value  # 无任何变更记录

    # 构建 [since, now] 区间内的分段边界
    boundaries = [since]
    seg_status = [status_at_since]
    for t, to_s, _from in trans:
        if since < t < now:
            boundaries.append(t)
            seg_status.append(to_s)
    boundaries.append(now)

    segments = []
    n = len(seg_status)
    for i in range(n):
        start, end = boundaries[i], boundaries[i + 1]
        dur = (end - start).total_seconds()
        sv = seg_status[i]
        # 醒来段（前一段睡眠、这一段醒来）即使很短也保留——"睡眠→空闲"是"醒来"的关键信号，
        # 否则"睡眠8h→醒来4s"会被过滤成只剩"睡眠"单段，漫想误读成"离开8小时"
        prev_sv = seg_status[i - 1] if i > 0 else None
        is_wakeup = (prev_sv in ("sleeping", "napping")) and (sv not in ("sleeping", "napping"))
        if dur < 30 and not is_wakeup:  # 忽略快速切换产生的极短段（但保留"醒来"段）
            continue
        try:
            disp = STATUS_DISPLAY_NAMES.get(UserStatus(sv), sv)
        except Exception:
            disp = sv
        segments.append({
            "status": sv,
            "status_display": disp,
            "start": start.isoformat(),
            "duration_seconds": dur,
            "is_current": (i == n - 1),
        })
    return segments


def format_status_timeline_since(since_dt) -> str:
    """把 get_status_timeline_since 的结果格式化为一行中文文本。

    仅当离开期间发生过状态切换（>=2 段）时返回内容，否则返回空串
    （单一状态段与「离开时长」信息重复，无需赘述）。
    """
    segs = get_status_timeline_since(since_dt)
    if len(segs) < 2:
        return ""
    from datetime import datetime
    parts = []
    for s in segs:
        try:
            t = datetime.fromisoformat(s["start"]).strftime("%H:%M")
        except Exception:
            t = ""
        dur = _fmt_duration(s["duration_seconds"])
        cur = "，当前" if s["is_current"] else ""
        prefix = f"{t} " if t else ""
        parts.append(f"{prefix}{s['status_display']}(持续{dur}{cur})")
    return " → ".join(parts)


def get_user_status_custom_text() -> str:
    return _user_status_custom_text


def set_user_status_custom_text(text: str):
    global _user_status_custom_text
    _user_status_custom_text = text[:20]
    save_user_status()


def save_user_status():
    """持久化当前用户状态到文件"""
    global _status_meta
    try:
        os.makedirs(os.path.dirname(_USER_STATUS_FILE), exist_ok=True)
        with open(_USER_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "status": _user_status.value,
                "custom_text": _user_status_custom_text,
                "history": _status_history[-100:],
                "meta": _status_meta.to_dict() if _status_meta else None,
            }, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"保存用户状态失败: {e}")


def restore_user_status():
    """从文件恢复用户状态，失败则保持默认 IDLE"""
    global _user_status, _user_status_custom_text, _status_history, _status_meta
    try:
        if os.path.exists(_USER_STATUS_FILE):
            with open(_USER_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            status_val = data.get("status", "idle")
            if status_val in [s.value for s in UserStatus]:
                _user_status = UserStatus(status_val)
            _user_status_custom_text = data.get("custom_text", "")[:20]
            _status_history = data.get("history", [])
            # 恢复 StatusMeta（向后兼容：旧文件无 meta 键 → from_legacy_only）
            meta_dict = data.get("meta")
            if meta_dict and isinstance(meta_dict, dict):
                _status_meta = StatusMeta.from_dict(meta_dict)
            else:
                _status_meta = StatusMeta.from_legacy_only(_user_status.value)
    except Exception:
        pass


def get_user_status_context() -> str:
    """获取用户状态的 prompt 上下文说明文字（legacy 格式，兼容旧调用方）。

    新代码请用 get_user_status_context_rich() —— 含 location 层级信息。
    """
    status_display = STATUS_DISPLAY_NAMES.get(_user_status, "未知")
    custom = _user_status_custom_text

    if _user_status == UserStatus.IDLE:
        # 自动模式下 idle="未知"（更诚实:系统不知道你在干嘛），手动模式下 idle="空闲"
        try:
            from mirrow_core import settings_manager
            mode = settings_manager.get_setting("user_status_mode")
        except Exception:
            mode = None
        if mode == "manual":
            description = IDLE_FIXED_DESCRIPTION
        else:
            description = "用户状态未知（未检测到特定活动）"
    elif custom:
        description = custom
    else:
        description = DEFAULT_DESCRIPTIONS.get(_user_status, "")

    if description:
        return f"用户状态：{status_display}，{description}"
    return f"用户状态：{status_display}"


def get_user_status_context_rich() -> str:
    """获取用户状态的 prompt 上下文说明文字（含 location 层级信息）。

    替代 get_user_status_context() 用于所有需要感知「在家/外出」的场景。
    示例输出：
      - "用户状态：在家·游戏，用户在玩游戏（APEX）"
      - "用户状态：外出·吃饭，用户正在外面吃饭"
      - "用户状态：在家·空闲，用户在家，未检测到特定活动"
      - "用户状态：外出，用户出门/上班了"
    """
    global _status_meta
    meta = _status_meta
    legacy = _user_status
    custom = _user_status_custom_text
    label = meta.display_label

    if legacy == UserStatus.IDLE:
        try:
            from mirrow_core import settings_manager
            mode = settings_manager.get_setting("user_status_mode")
        except Exception:
            mode = None
        if mode == "manual":
            if meta.location == "home":
                description = "用户在家，没特意说在做什么"
            elif meta.location == "out":
                description = "用户外出了，没特意说在做什么"
            else:
                description = IDLE_FIXED_DESCRIPTION
        else:
            if meta.location == "home":
                description = "用户在家，未检测到特定活动"
            elif meta.location == "out":
                description = "用户外出，未检测到特定活动"
            else:
                description = "用户状态未知（未检测到特定活动）"
    elif custom:
        description = custom
    else:
        description = DEFAULT_DESCRIPTIONS.get(legacy, "")

    if description:
        return f"用户状态：{label}，{description}"
    return f"用户状态：{label}"


# ============================================================================
# => idle<=>>



# ============================================================================
# 睡眠切出（唤醒） -- 两套规则共存，任一满足即切。
# 规则A: page_visible（前端启动/打开）-> 无门槛直接切（开前端就是要用）
# 规则B: user_message / phone_activity --> 需睡够阈值（默认4h,设置sleep_wake_threshold_hours可配）
# 唤醒语义由本模块实现；不依赖宿主开发文档。
# ============================================================================

def _sleep_wake_threshold_hours() -> float:
    try:
        from mirrow_core import settings_manager
        v = settings_manager.get_setting('sleep_wake_threshold_hours')
        if v and float(v) > 0: return float(v)
    except: pass
    return 4.0

def _slept_hours() -> float:
    from datetime import datetime
    for entry in reversed(_status_history):
        if entry.get('to') == 'sleeping':
            try: return (datetime.now() - datetime.fromisoformat(entry.get('time'))).total_seconds() / 3600
            except: return -1.0
    return -1.0

def should_wake_from_sleeping(trigger: str) -> bool:
    global _user_status
    if _user_status != UserStatus.SLEEPING:
        return False
    if trigger == 'page_visible':
        logger.info('wakeup: page_visible -> sleeping -> idle')
        return True
    hours = _slept_hours()
    if hours < 0 or hours < _sleep_wake_threshold_hours():
        return False
    if trigger in ('user_message', 'phone_activity'):
        logger.info(f'wakeup: slept {hours:.1f}h + {trigger} -> sleeping -> idle')
        return True
    return False

def check_wakeup(reason: str = 'user_message') -> bool:
    return should_wake_from_sleeping(reason)
