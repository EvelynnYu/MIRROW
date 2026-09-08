"""
settings_manager — JSON 设置持久化（开源精简版）

设置文件路径可通过环境变量 MIRROW_SETTINGS_FILE 指定，默认当前工作目录
下的 settings.json。截断参数的默认值统一来自 truncation_config，不在
此处重复维护。
"""
import os
import json
import threading
from typing import Dict, Any

from mirrow_core.truncation_config import get_truncation_defaults_for_settings

SETTINGS_FILE = os.getenv("MIRROW_SETTINGS_FILE", "settings.json")
_lock = threading.Lock()
_defaults: Dict[str, Any] = {
    "aiPersona": None,
    "dnd_enabled": False,
    "persona_user_name": None,   # 用户称呼（persona.py 读取，env 优先）
    "persona_ai_name": None,     # AI 名字（persona.py 读取，env 优先）
    # ── 截断参数（元数据由 truncation_config.py 管理）──
    **get_truncation_defaults_for_settings(),
}


def _load() -> Dict[str, Any]:
    """加载设置文件"""
    if not os.path.exists(SETTINGS_FILE):
        return dict(_defaults)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_defaults)
        merged.update(data)
        return merged
    except Exception:
        return dict(_defaults)


def _save(data: Dict[str, Any]):
    """原子保存设置到文件（写临时文件→rename，防止写入中途崩溃损坏文件）"""
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)  # atomic on POSIX/Windows


def get_settings() -> Dict[str, Any]:
    """获取所有设置"""
    with _lock:
        return _load()


def get_setting(key: str):
    """获取单个设置"""
    with _lock:
        return _load().get(key)


def set_setting(key: str, value) -> None:
    """更新单个设置"""
    with _lock:
        data = _load()
        data[key] = value
        _save(data)


def get_ai_persona_simplified() -> str:
    """获取简化版 AI 人设，用于内部 prompt 注入"""
    persona = get_setting("aiPersona")
    if not persona:
        return ""
    if isinstance(persona, dict):
        name = persona.get("name", "")
        traits = persona.get("traits", "")
        style = persona.get("style", "")
        parts = [p for p in [name, traits, style] if p]
        return "、".join(parts)
    if isinstance(persona, str):
        from mirrow_core.truncation_config import get_truncation_limit
        _psc = get_truncation_limit("persona_simplified_cap")
        if _psc > 0 and len(persona) > _psc:
            return persona[:_psc]
        return persona
    return ""
