# 漫想记录（wander journal）持久化
#
# 「全自主区间式行动」的漫想记录层：持久化「活动会话」和「短期偏好槽」。
# 与 wander_log_sqlite.py 共享同一个 DB 文件（event_chronicle.db），但表平级独立：
#   - activity_sessions  持续活动会话（含节点 + 情绪快照）
#   - wander_preference  短期偏好槽（事件结算「要不要继续 Y」写进，供下次计划 LLM 补充）
#
# 旧的 wander_events 是「扁平原子事件日志」，承载不了「会话 + 节点 + 情绪 delta」。

import sqlite3
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from .activity_session import ActivitySession

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "events", "event_chronicle.db")


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接（自动建表）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_sessions (
            session_id TEXT PRIMARY KEY,
            activity_type TEXT NOT NULL,
            termination_mode TEXT NOT NULL,
            target_count INTEGER DEFAULT 0,
            target_duration_min INTEGER DEFAULT 0,
            soft_ceiling_min INTEGER DEFAULT 30,
            current_round INTEGER DEFAULT 0,
            nodes TEXT DEFAULT '[]',
            start_time TEXT NOT NULL,
            end_time TEXT DEFAULT '',
            start_mood TEXT DEFAULT '',
            end_mood TEXT DEFAULT '',
            abort_reason TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            detail TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_status ON activity_sessions(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_sessions(date(start_time))
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wander_preference (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            text TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


# ── 活动会话 CRUD ──

def upsert_session(session: ActivitySession):
    """写入/更新一条活动会话（节点每次推进后调用）。"""
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO activity_sessions
               (session_id, activity_type, termination_mode, target_count, target_duration_min,
                soft_ceiling_min, current_round, nodes, start_time, end_time,
                start_mood, end_mood, abort_reason, status, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.activity_type,
                session.termination_mode.value,
                session.target_count,
                session.target_duration_min,
                session.soft_ceiling_min,
                session.current_round,
                json.dumps([n.to_dict() for n in session.nodes], ensure_ascii=False),
                session.start_time.isoformat(),
                session.end_time.isoformat() if session.end_time else "",
                session.start_mood,
                session.end_mood,
                session.abort_reason,
                session.status,
                json.dumps(session.detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"写入活动会话失败 [{session.session_id}]: {e}")


def _row_to_session(row) -> ActivitySession:
    """把 DB 行转回 ActivitySession。"""
    data = dict(row)
    try:
        nodes = json.loads(data.get("nodes", "[]"))
    except Exception:
        nodes = []
    try:
        detail = json.loads(data.get("detail", "{}"))
    except Exception:
        detail = {}
    payload = {
        "session_id": data.get("session_id", ""),
        "activity_type": data.get("activity_type", ""),
        "termination_mode": data.get("termination_mode", "count"),
        "target_count": data.get("target_count", 0),
        "target_duration_min": data.get("target_duration_min", 0),
        "soft_ceiling_min": data.get("soft_ceiling_min", 30),
        "current_round": data.get("current_round", 0),
        "nodes": nodes,
        "start_time": data.get("start_time", ""),
        "end_time": data.get("end_time", "") or None,
        "start_mood": data.get("start_mood", ""),
        "end_mood": data.get("end_mood", ""),
        "abort_reason": data.get("abort_reason", ""),
        "status": data.get("status", "active"),
        "detail": detail,
    }
    return ActivitySession.from_dict(payload)


def get_session(session_id: str) -> Optional[ActivitySession]:
    """按 ID 取一条会话。"""
    try:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM activity_sessions WHERE session_id=?", (session_id,)).fetchone()
        conn.close()
        return _row_to_session(row) if row else None
    except Exception as e:
        logger.warning(f"读取活动会话失败 [{session_id}]: {e}")
        return None


def get_active_session() -> Optional[ActivitySession]:
    """取当前仍活跃（status='active'）的最近一条会话。"""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM activity_sessions WHERE status='active' ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return _row_to_session(row) if row else None
    except Exception as e:
        logger.warning(f"读取活跃会话失败: {e}")
        return None


def get_today_sessions() -> List[ActivitySession]:
    """取今天全部会话（按开始时间倒序）。"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM activity_sessions WHERE date(start_time)=date('now','localtime') "
            "ORDER BY start_time DESC"
        ).fetchall()
        conn.close()
        return [_row_to_session(r) for r in rows]
    except Exception as e:
        logger.warning(f"读取今日会话失败: {e}")
        return []


# ── 短期偏好槽 ──

def save_preference(text: str):
    """写入短期偏好槽（事件结算「要不要继续 Y」时）。空串表示清空。"""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO wander_preference (id, text) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, updated_at=datetime('now','localtime')",
            (text,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"写入短期偏好槽失败: {e}")


def get_preference() -> str:
    """读取短期偏好槽。"""
    try:
        conn = _get_conn()
        row = conn.execute("SELECT text FROM wander_preference WHERE id=1").fetchone()
        conn.close()
        return row["text"] if row else ""
    except Exception as e:
        logger.warning(f"读取短期偏好槽失败: {e}")
        return ""


def clear_preference():
    """清空短期偏好槽。"""
    save_preference("")
