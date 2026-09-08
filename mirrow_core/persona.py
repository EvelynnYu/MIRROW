"""
persona — 用户/AI 称呼配置层

为调用方提供中性默认称呼（"用户"/"AI"）及配置读取。
调用方需显式使用本模块；旧模块仍有固定角色文案，不能据此认定全库已完成称呼通用化。
优先级：环境变量 PERSONA_USER_NAME / PERSONA_AI_NAME → settings.json → 默认值。

AI 的完整人设（性格/说话风格）不在此处——通过各模块 init 时的
persona_prompt / call_llm_func 注入，由使用者自行编写。
"""
import os


def _resolve(env_key: str, setting_key: str, default: str) -> str:
    val = os.getenv(env_key, "").strip()
    if val:
        return val
    try:
        from mirrow_core.settings_manager import get_setting
        val = get_setting(setting_key)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return default


def get_user_name() -> str:
    """用户称呼（展示文案/行为状态字符串中使用）。"""
    return _resolve("PERSONA_USER_NAME", "persona_user_name", "用户")


def get_ai_name() -> str:
    """AI 名字（展示文案中使用）。"""
    return _resolve("PERSONA_AI_NAME", "persona_ai_name", "AI")


def get_timeline_anchor() -> str:
    """Optional host-authored relationship/history context; blank by default."""
    return _resolve("PERSONA_TIMELINE_ANCHOR", "persona_timeline_anchor", "")


def get_group_sender_labels() -> dict[str, str]:
    """Optional JSON mapping for host-owned group participant labels."""
    raw = _resolve("PERSONA_GROUP_SENDER_LABELS", "persona_group_sender_labels", "")
    if not raw:
        return {}
    try:
        import json
        value = json.loads(raw)
        return {str(key): str(label) for key, label in value.items()} if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


# 模块级常量：导入时解析一次（改名后需重启进程）
USER_NAME: str = get_user_name()
AI_NAME: str = get_ai_name()
