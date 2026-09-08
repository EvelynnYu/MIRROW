"""许愿历史 — 兼容层，内部委托给 wish_store（events/wishes.db）。

旧实现基于 data/wish_history.json，现已迁移到 SQLite。本模块保留原有函数
签名（load_wish_history / add_wish / get_wish_context_for_reflection /
get_pending_wishes / get_fulfilled_wishes / mark_fulfilled），调用方零改动。

新增：get_wish_context_for_reflection 会注入用户的反馈 comment，让 AI 下次
自省时能看到「用户对这个愿望的态度」。
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def load_wish_history() -> dict:
    """返回 {"wishes": [...], "last_updated": ""}（兼容旧 JSON 结构）。"""
    try:
        from .wish_store import get_wish_store
        wishes = get_wish_store().list_all()
        return {"wishes": wishes, "last_updated": ""}
    except Exception as e:
        logger.warning(f"加载许愿历史失败，回退空结构: {e}")
        return {"wishes": [], "last_updated": ""}


def save_wish_history(history: dict) -> bool:
    """兼容保留（SQLite 即时落盘，无需显式保存）。"""
    return True


def add_wish(feature: str, reason: str) -> Optional[dict]:
    """兼容旧路径的精确规范化新增/合并；不会创建历史愿望的副本。"""
    try:
        from .wish_store import get_wish_store
        return get_wish_store().add_or_merge(feature, reason)
    except Exception as e:
        logger.warning(f"新增许愿失败: {e}")
        return None


def get_wish_context_for_reflection() -> str:
    """格式化完整许愿板快照，供 AI 在自省前判断愿望动作。"""
    try:
        from .wish_store import get_wish_store
        return get_wish_store().get_context_for_reflection()
    except Exception as exc:
        logger.warning("加载许愿板上下文失败: %s", exc)
        return "（许愿板暂时不可用。）"


def get_pending_wishes() -> List[dict]:
    """获取所有仍在许愿中的愿望（旧 pending 名称兼容为 open）。"""
    try:
        from .wish_store import get_wish_store
        return get_wish_store().get_by_status("pending")
    except Exception:
        return []


def get_fulfilled_wishes() -> List[dict]:
    """获取所有 fulfilled 状态的愿望。"""
    try:
        from .wish_store import get_wish_store
        return get_wish_store().get_by_status("fulfilled")
    except Exception:
        return []


def mark_fulfilled(feature: str, note: str = "") -> bool:
    """按精确规范化名称标记一个愿望为已实现。"""
    try:
        from .wish_store import get_wish_store
        return get_wish_store().mark_fulfilled(feature, note)
    except Exception as e:
        logger.warning(f"标记愿望已实现失败: {e}")
        return False
