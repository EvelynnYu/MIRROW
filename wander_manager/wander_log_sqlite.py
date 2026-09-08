# 漫想日志 SQLite 存储
#
# 替换 JSON 持久化，支持日期查询和按会话过滤。
# 排除 SLEEP 和 USER_TRACKING（仅记录"有效信息"）。

import sqlite3
import json
import os
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "events", "event_chronicle.db")

_EXCLUDED_TYPES = {"sleep", "user_tracking"}


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接（自动创建表）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wander_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            description TEXT DEFAULT '',
            process_log TEXT DEFAULT '',
            details TEXT DEFAULT '{}',
            judgment_result TEXT DEFAULT '{}',
            pushed INTEGER DEFAULT 0,
            session_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wander_date ON wander_events(date(timestamp))
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wander_session ON wander_events(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wander_type ON wander_events(event_type)
    """)
    return conn


def store_event(
    event_id: str,
    event_type: str,
    timestamp: str,
    description: str = "",
    process_log: str = "",
    details: Optional[Dict] = None,
    judgment_result: Optional[Dict] = None,
    pushed: bool = False,
    session_id: str = "",
):
    """存储一条漫想事件。排除 SLEEP 和 USER_TRACKING。"""
    if event_type in _EXCLUDED_TYPES:
        return

    try:
        conn = _get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO wander_events
               (event_id, event_type, timestamp, description, process_log, details, judgment_result, pushed, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event_type,
                timestamp,
                description,
                process_log,
                json.dumps(details or {}, ensure_ascii=False),
                json.dumps(judgment_result or {}, ensure_ascii=False),
                1 if pushed else 0,
                session_id,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"存储漫想事件失败 [{event_id}]: {e}")


def update_judgment(event_id: str, judgment_result: Dict, pushed: bool):
    """更新事件的判断结果。"""
    if not event_id:
        return
    try:
        conn = _get_conn()
        conn.execute(
            "UPDATE wander_events SET judgment_result=?, pushed=? WHERE event_id=?",
            (json.dumps(judgment_result, ensure_ascii=False), 1 if pushed else 0, event_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"更新判断结果失败 [{event_id}]: {e}")


def query_logs(
    limit: int = 50,
    pushed: Optional[bool] = None,
    since: Optional[datetime] = None,
    date: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查询漫想日志"""
    try:
        conn = _get_conn()
        conditions = []
        params: List[Any] = []

        if pushed is not None:
            conditions.append("pushed = ?")
            params.append(1 if pushed else 0)

        if date:
            conditions.append("date(timestamp) = ?")
            params.append(date)
        elif since:
            conditions.append("timestamp >= ?")
            params.append(since.isoformat())

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM wander_events {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        results = []
        for row in rows:
            r = dict(row)
            try:
                r["details"] = json.loads(r.get("details", "{}"))
            except Exception:
                r["details"] = {}
            try:
                r["judgment_result"] = json.loads(r.get("judgment_result", "{}"))
            except Exception:
                r["judgment_result"] = {}
            r["pushed"] = bool(r.get("pushed", 0))
            results.append(r)

        return results
    except Exception as e:
        logger.warning(f"查询漫想日志失败: {e}")
        return []


def cleanup_expired_sqlite(retention_days: int = 90):
    """删除超过保留期的漫想事件。建议每 100 次写入调用一次。"""
    cutoff = (datetime.now() - __import__('datetime').timedelta(days=retention_days)).isoformat()
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM wander_events WHERE timestamp < ?", (cutoff,))
        deleted = conn.rowcount if hasattr(conn, 'rowcount') else 0
        conn.commit()
        if deleted:
            logger.info(f"Wander cleanup: deleted {deleted} events older than {retention_days}d")
    except Exception as e:
        logger.warning(f"Wander cleanup failed: {e}")


def get_stats() -> Dict[str, Any]:
    """获取统计信息"""
    try:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) FROM wander_events").fetchone()[0]
        pushed_count = conn.execute("SELECT COUNT(*) FROM wander_events WHERE pushed=1").fetchone()[0]
        type_rows = conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM wander_events GROUP BY event_type"
        ).fetchall()
        conn.close()
        return {
            "total_entries": total,
            "pushed_count": pushed_count,
            "type_counts": {r[0]: r[1] for r in type_rows},
        }
    except Exception:
        return {"total_entries": 0, "pushed_count": 0, "type_counts": {}}
