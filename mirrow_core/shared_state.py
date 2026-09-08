"""
shared_state — 跨模块全局状态注册中心（开源精简版）

只保留开源模块（silicon_perception / wander_manager / behavior_scheduler）
实际消费的状态。所有默认值均为安全值（None/False/空），未接线时各模块
自然降级，不会崩溃。

宿主应用（你自己的 FastAPI/入口进程）负责在启动时注入：
- set_mobile_relay_callback: 手机端工具中继回调（无手机端则不设）
- set_mirrow_page_visible / set_mirrow_page_last_seen: 前端可见性心跳
- set_night_mode_active: 深夜模式开关（不用则忽略）
"""
import os
import time as _time
from datetime import datetime
from typing import Callable, Optional

# ═══════════════════════════════════════════
# 前端页面可见性（心跳上报，用于"用户是否在看界面"判断）
# ═══════════════════════════════════════════
_mirrow_page_visible: bool = False
_mirrow_page_last_seen: float = 0.0


def get_mirrow_page_visible() -> bool:
    return _mirrow_page_visible


def set_mirrow_page_visible(val: bool):
    global _mirrow_page_visible
    _mirrow_page_visible = val


def get_mirrow_page_last_seen() -> float:
    return _mirrow_page_last_seen


def set_mirrow_page_last_seen(val: float):
    global _mirrow_page_last_seen
    _mirrow_page_last_seen = val


# ═══════════════════════════════════════════
# 深夜模式开关（不使用此机制的部署保持 False 即可）
# ═══════════════════════════════════════════
_night_mode_active: bool = False


def get_night_mode_active() -> bool:
    return _night_mode_active


def set_night_mode_active(val: bool):
    global _night_mode_active
    _night_mode_active = val


# ═══════════════════════════════════════════
# 哨兵最近 tick 时间（监控/诊断用）
# ═══════════════════════════════════════════
_sentinel_last_tick: float = 0.0


def get_sentinel_last_tick() -> float:
    return _sentinel_last_tick


def set_sentinel_last_tick(val: float):
    global _sentinel_last_tick
    _sentinel_last_tick = val


# ═══════════════════════════════════════════
# 手机端工具中继回调（唯一注册中心）
# 签名: async def relay(request_id: str, tool: str, params: dict) -> dict
# 未设置时所有依赖手机的数据源静默降级
# ═══════════════════════════════════════════
_mobile_relay_callback: Optional[Callable] = None


def get_mobile_relay_callback() -> Optional[Callable]:
    return _mobile_relay_callback


def set_mobile_relay_callback(cb: Optional[Callable]):
    global _mobile_relay_callback
    _mobile_relay_callback = cb


# ═══════════════════════════════════════════
# 手机端 WebSocket 连接计数
# ═══════════════════════════════════════════
_mobile_ws_count: int = 0


def get_mobile_connected() -> bool:
    return _mobile_ws_count > 0


def incr_mobile_ws():
    global _mobile_ws_count
    _mobile_ws_count += 1


def decr_mobile_ws():
    global _mobile_ws_count
    _mobile_ws_count = max(0, _mobile_ws_count - 1)


def set_mobile_connected(val: bool):
    """兼容接口：直接设定连接态（内部换算为计数 0/1）。"""
    global _mobile_ws_count
    _mobile_ws_count = 1 if val else 0


# ═══════════════════════════════════════════
# 当前用户状态（idle/gaming/sleeping/... 见 wander_manager.user_status）
# 变更时同步记录时间戳（陈旧状态声明不应永久生效）
# ═══════════════════════════════════════════
_current_user_status: str = ""
_current_user_status_ts: Optional[str] = None


def get_current_user_status() -> str:
    return _current_user_status


def get_current_user_status_ts() -> Optional[str]:
    return _current_user_status_ts


def set_current_user_status(val: str):
    global _current_user_status, _current_user_status_ts
    _current_user_status = val
    _current_user_status_ts = datetime.now().isoformat()


# ═══════════════════════════════════════════
# 图像分析模型选择（视觉分析路径的运行时开关）
# ═══════════════════════════════════════════
_current_image_model: str = os.getenv("IMAGE_MODEL", "glm")


def get_current_image_model() -> str:
    return _current_image_model


def set_current_image_model(model: str) -> None:
    global _current_image_model
    _current_image_model = model


# ═══════════════════════════════════════════
# 勿扰开关（DND）— 一刀压制 AI 所有主动出声（哨兵/漫想推送）
# ═══════════════════════════════════════════

def get_dnd_enabled() -> bool:
    try:
        from mirrow_core import settings_manager
        return bool(settings_manager.get_setting("dnd_enabled"))
    except Exception:
        return False


def set_dnd_enabled(val: bool):
    try:
        from mirrow_core import settings_manager
        settings_manager.set_setting("dnd_enabled", bool(val))
    except Exception:
        pass


def should_suppress_proactive() -> bool:
    """判断当前是否应压制 AI 的主动表达（哨兵/漫想/状态切换推送）。"""
    return get_dnd_enabled()


# ═══════════════════════════════════════════
# 最近用户消息时间（monotonic，供沉默时长判断）
# ═══════════════════════════════════════════
_last_user_message_time: float = _time.monotonic()


def get_last_user_message_time() -> float:
    return _last_user_message_time


def set_last_user_message_time(val: float):
    global _last_user_message_time
    _last_user_message_time = val


_active_session_id = ""
_latest_persona_prompt = ""
_last_private_chat_time = None


def get_active_session_id():
    return _active_session_id


def set_active_session_id(value):
    global _active_session_id
    _active_session_id = str(value or "")


def get_latest_persona_prompt():
    return _latest_persona_prompt


def set_latest_persona_prompt(value):
    global _latest_persona_prompt
    _latest_persona_prompt = str(value or "")


def get_last_private_chat_time(resolve_from_store=True):
    """Unknown until the host supplies a real private user-message timestamp."""
    return _last_private_chat_time


def set_last_private_chat_time(value):
    global _last_private_chat_time
    if value is not None and not isinstance(value, datetime):
        raise TypeError('private chat time must be datetime or None')
    _last_private_chat_time = value.replace(tzinfo=None) if value is not None and value.tzinfo is None else (
        value.astimezone().replace(tzinfo=None) if value is not None else None
    )
