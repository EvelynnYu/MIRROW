"""防抖 / 最小驻留 — 防止状态高频抖动 + coding 需持续确认。

设计：候选状态必须**稳定持续 required_sec 秒**才提交为新状态。
- 大多数 presence：短窗确认（confirm 基于 min_dwell_sec 量级）。
- coding：需前台编码工具持续 coding_sustain_sec（防打游戏中途切屏误切）。
- gaming 期间切 coding：更长确认窗 gaming_coding_sustain_sec。

sustain 机制天然兼具"最小驻留"——同一候选没稳定够久不会切，杜绝 A→B→A 抖动。
物理约束（transitions 图）拦截非法转移。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .model import Presence, Activity, RestMode, StateVector

logger = logging.getLogger(__name__)

# 非法转移（物理约束）：从 rest 直接跳到高活跃 activity 不合理，须先经"醒来/回到设备前"
# 这里只列真正物理不可能的；大多数转移合法。
_ILLEGAL = {
    # (from_rest, to_activity) — 睡眠中不可能直接在打游戏/写代码（须先醒来切出 rest）
    (RestMode.SLEEPING, Activity.GAMING),
    (RestMode.SLEEPING, Activity.CODING),
}


def _now() -> datetime:
    return datetime.now()


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


class Debouncer:
    """状态防抖器。持有已提交状态，按 sustain 要求决定是否切换。"""

    def __init__(self, committed: StateVector):
        self.committed = committed
        self._pending: Optional[StateVector] = None
        self._pending_since: Optional[str] = None  # ISO

    def propose(self, candidate: StateVector, required_sec: float, now_iso: Optional[str] = None):
        """提交一个候选，返回 (committed_state, changed: bool)。

        candidate.key() 与已提交相同 → 保持，清空 pending。
        不同 → 需候选稳定持续 required_sec 才提交。
        """
        now_s = now_iso or _now().isoformat()

        if candidate.key() == self.committed.key():
            self._pending = None
            self._pending_since = None
            return self.committed, False

        # 物理约束：非法转移直接拒绝（保持原状态）
        if (self.committed.rest_mode, candidate.activity) in _ILLEGAL:
            logger.debug(f"Debouncer: 拒绝非法转移 {self.committed.rest_mode}→{candidate.activity}")
            self._pending = None
            self._pending_since = None
            return self.committed, False

        # 候选变化 → 重置 pending 计时
        if self._pending is None or self._pending.key() != candidate.key():
            self._pending = candidate
            self._pending_since = now_s
            return self.committed, False

        # 候选稳定 → 检查是否持续够久
        since = _parse(self._pending_since) if self._pending_since else None
        now_dt = _parse(now_s)
        if since and now_dt:
            elapsed = (now_dt - since).total_seconds()
            if elapsed >= required_sec:
                self.committed = candidate
                self._pending = None
                self._pending_since = None
                return candidate, True
        return self.committed, False

    def force_commit(self, state: StateVector):
        """强制提交（如用户手动声明 / 休息模式切入切出）。"""
        self.committed = state
        self._pending = None
        self._pending_since = None
