"""作息窗口学习 — 从 status_change_log 推算用户典型入睡/起床时间

从过去 14 天的 →sleeping/napping 和 sleeping/napping→ 转换记录中
聚类出典型入睡窗口和起床窗口。每周自动更新一次。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)


class SleepWindowLearner:
    """从状态切换日志学习用户作息窗口"""

    def __init__(self, health_store):
        self._store = health_store

    def learn(self) -> Optional[dict]:
        """学习作息窗口，写入 sleep_window 表。返回学习结果或 None。"""
        bedtimes, waketimes = self._collect_times(days=14)
        if len(bedtimes) < 3 or len(waketimes) < 3:
            logger.info("SleepWindow: 样本不足（<3天），跳过学习")
            return None

        # 去掉最早和最晚各 1 个（排除极端值）
        bedtimes.sort()
        waketimes.sort()
        bed_trimmed = bedtimes[1:-1] if len(bedtimes) >= 5 else bedtimes
        wake_trimmed = waketimes[1:-1] if len(waketimes) >= 5 else waketimes

        # 取范围
        bedtime_earliest = self._fmt(min(bed_trimmed))
        bedtime_latest = self._fmt(max(bed_trimmed))
        wake_earliest = self._fmt(min(wake_trimmed))
        wake_latest = self._fmt(max(wake_trimmed))
        confidence = min(1.0, len(bedtimes) / 10.0)  # 10 天数据 → 置信度 1.0

        conn = self._store._get_conn()
        conn.execute(
            """INSERT INTO sleep_window
               (updated_at, bedtime_earliest, bedtime_latest, wake_earliest, wake_latest, confidence, sample_days)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                bedtime_earliest, bedtime_latest,
                wake_earliest, wake_latest,
                confidence, len(bedtimes),
            ),
        )
        conn.commit()

        logger.info(
            f"SleepWindow: 已学习 入睡{bedtime_earliest}-{bedtime_latest} "
            f"起床{wake_earliest}-{wake_latest} (n={len(bedtimes)}, conf={confidence:.1f})"
        )
        return {
            "bedtime_earliest": bedtime_earliest,
            "bedtime_latest": bedtime_latest,
            "wake_earliest": wake_earliest,
            "wake_latest": wake_latest,
            "confidence": confidence,
            "sample_days": len(bedtimes),
        }

    def should_update(self) -> bool:
        """距上次学习 >7 天 → 需要更新。"""
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT updated_at FROM sleep_window ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return True
            last = datetime.fromisoformat(row["updated_at"])
            return (datetime.now() - last).days >= 7
        except Exception:
            return True

    def get_window(self) -> Optional[dict]:
        """获取当前作息窗口。"""
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT * FROM sleep_window ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    # ── 内部分析 ──────────────────────────────────────

    def _collect_times(self, days: int) -> Tuple[List[int], List[int]]:
        """从 status_change_log 收集入睡和起床时间（分钟数）。"""
        try:
            # status_change_log 在 health_tracker.db 中
            import sqlite3, os
            htdb = os.path.join(
                os.path.dirname(__file__), "..", "..", "events", "health_tracker.db"
            )
            if not os.path.exists(htdb):
                return [], []

            conn = sqlite3.connect(htdb)
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT * FROM status_change_log WHERE active_date >= ? ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"SleepWindow: 查询 status_change_log 失败: {e}")
            return [], []

        bedtimes, waketimes = [], []
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["timestamp"])
                minutes = dt.hour * 60 + dt.minute
            except Exception:
                continue

            to_s = r["to_status"]
            from_s = r["from_status"]

            # 入睡：任何状态 → sleeping 或 napping
            if to_s in ("sleeping", "napping") and from_s not in ("sleeping", "napping"):
                bedtimes.append(minutes)
            # 起床：sleeping/napping → 其他（idle 最常见）
            elif from_s in ("sleeping", "napping") and to_s not in ("sleeping", "napping"):
                waketimes.append(minutes)

        return bedtimes, waketimes

    @staticmethod
    def _fmt(minutes: int) -> str:
        """分钟数 → HH:MM。"""
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
