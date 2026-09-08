"""行为状态推断 — 融合 PC进程+键鼠空闲+手机App，输出自然语言状态。

纯规则引擎，每 tick 运行，零 LLM 调用。输出写入 health_snapshots.behavior_state。

输出示例（用户称呼取自 mirrow_core.persona.USER_NAME，默认"用户"）：
- 用户在玩电脑(黎明杀机)
- 用户在用手机刷小红书
- 用户同时在用手机(微信)和电脑(Code)
- 用户电脑在放东西(Chrome)
- 用户没在设备前

本模块是状态字符串格式的唯一权威来源：消费方（如 trigger_detector）必须
import 下方的前缀常量或 behavior_category()，禁止硬编码字面量做匹配。
"""

import logging
from typing import Optional

from mirrow_core.persona import USER_NAME

logger = logging.getLogger(__name__)


# ── 状态前缀常量（文案与逻辑解耦：改称呼不破坏类别匹配）──────
STATE_PREFIX_BOTH = f"{USER_NAME}同时在用手机"      # both
STATE_PREFIX_PHONE = f"{USER_NAME}在用手机"          # phone
STATE_PREFIX_PC = f"{USER_NAME}在玩电脑"             # pc
STATE_PREFIX_PC_IDLE = f"{USER_NAME}电脑在放东西"    # pc_idle
STATE_PREFIX_PHONE_IDLE = "手机亮屏"                 # phone_idle
STATE_PREFIX_AWAY = f"{USER_NAME}没在设备前"         # away


def behavior_category(state: Optional[str]) -> str:
    """从 behavior_state 字符串提取设备类别。

    返回: both / phone / pc / pc_idle / phone_idle / away / unknown
    infer_behavior_state() 新增输出格式时在此同步加一个前缀分支。
    注意 BOTH 必须先于 PHONE 判断（保持当前顺序）。
    """
    if not state:
        return "unknown"
    if state.startswith(STATE_PREFIX_BOTH):
        return "both"
    if state.startswith(STATE_PREFIX_PHONE):
        return "phone"
    if state.startswith(STATE_PREFIX_PC):
        return "pc"
    if state.startswith(STATE_PREFIX_PC_IDLE):
        return "pc_idle"
    if state.startswith(STATE_PREFIX_PHONE_IDLE):
        return "phone_idle"
    if state.startswith(STATE_PREFIX_AWAY):
        return "away"
    return "unknown"


def infer_behavior_state(
    input_idle_seconds: Optional[float],
    foreground_exe: Optional[str],
    foreground_app: Optional[str],
    screen_active: Optional[bool],
    mobile_app_name: Optional[str],
    mobile_screen_on: Optional[bool] = None,
    phone_session_seconds: Optional[float] = None,
) -> str:
    """根据多源输入推断当前行为状态，返回自然语言。

    Args:
        phone_session_seconds: 手机连续亮屏秒数。用于区分"瞥一眼"（<2min→保持pc）和"真正双设备使用"（≥2min→both）。

    Returns:
        自然语言行为状态字符串。兼容旧调用方（仍以 phone_/pc_ 前缀 + unknown 枚举）。
    """
    pc_active = input_idle_seconds is not None and input_idle_seconds < 120  # 2min 内操作过
    pc_on = screen_active is True  # 屏幕画面在变化
    phone_active = mobile_screen_on is True and mobile_app_name is not None

    # PC 显示名：优先窗口标题（人类可读），回退 exe 去后缀
    pc_name = _pc_display_name(foreground_exe, foreground_app)

    # ── 同时用两台设备 ──
    # Case A: PC 活跃（idle<120s）+ 手机连续亮屏 ≥60s → 双设备（瞥一眼通知不算，防 pc→both 抖动触发 L1）
    # Case B: PC 空闲（idle>60s）+ 手机长亮（≥120s）→ 注意力转移（放下电脑看手机）
    pc_attended = input_idle_seconds is not None and input_idle_seconds < 120
    phone_60s = (phone_session_seconds or 0) >= 60
    phone_long = (phone_session_seconds or 0) >= 120
    if phone_active and pc_name and ((pc_attended and phone_60s) or phone_long):
        return f"{STATE_PREFIX_BOTH}({mobile_app_name})和电脑({pc_name})"
    if phone_active and ((pc_attended and phone_60s) or phone_long):
        return f"{STATE_PREFIX_BOTH}({mobile_app_name})和电脑"

    # ── 只用手机 ──
    if phone_active:
        return f"{STATE_PREFIX_PHONE}刷{mobile_app_name}"

    # ── 只用电脑 ──
    if pc_active and pc_name:
        return f"{STATE_PREFIX_PC}({pc_name})"
    if pc_active:
        return STATE_PREFIX_PC

    # ── 电脑开着但人不在操作（可能在放视频/挂机）──
    if pc_on and not pc_active and (input_idle_seconds or 0) < 1800:
        if pc_name:
            return f"{STATE_PREFIX_PC_IDLE}({pc_name})"
        return STATE_PREFIX_PC_IDLE

    # ── 手机亮屏但无前台 App ──
    if mobile_screen_on and not phone_active:
        return f"{STATE_PREFIX_PHONE_IDLE}(无前台App)"

    # ── 设备前没人（双设备空闲 >5min）──
    if not pc_active and not phone_active:
        idle_min = (input_idle_seconds or 0) / 60
        if idle_min > 5:
            return STATE_PREFIX_AWAY

    # 兜底
    return "unknown"


def _pc_display_name(exe: Optional[str], app: Optional[str]) -> Optional[str]:
    """PC 进程名 → 人类可读显示名。
    优先用 exe 精简名（精确稳定），回退到窗口标题。
    """
    if exe:
        name = exe.rsplit(".exe", 1)[0]
        # 精简常见后缀
        for suffix in ("-Win64-Shipping", "-win64-shipping", "-Win64", "-win64", ".exe"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        return name
    if app and len(app) > 1:
        # 截断过长标题
        return app[:50] + "..." if len(app) > 50 else app
    return None
