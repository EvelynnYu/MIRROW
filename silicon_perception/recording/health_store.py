"""健康数据时间序列存储 (SQLite)

存储心率等传感器快照 + 哨兵事件记录，保留 7 天。
"""

import sqlite3
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.join(DB_DIR, "silicon_perception.db")
RETENTION_DAYS = 7


def _now_iso() -> str:
    return datetime.now().isoformat()


class HealthStore:
    """健康数据 SQLite 存储"""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ── 连接管理 ──────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_db()
        return self._conn

    def _init_db(self):
        conn = self._conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS health_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                heart_rate INTEGER,
                screen_active INTEGER,
                foreground_app TEXT,
                mirrow_visible INTEGER,
                user_status TEXT,
                is_period INTEGER,
                period_day INTEGER
            );

            CREATE TABLE IF NOT EXISTS sentinel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                priority TEXT NOT NULL,
                snapshot_id INTEGER,
                message TEXT,
                pushed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_health_summary (
                date TEXT PRIMARY KEY,
                steps INTEGER,
                hr_min INTEGER,
                hr_max INTEGER,
                hr_avg REAL,
                sleep_min INTEGER,
                spo2_min INTEGER,
                spo2_max INTEGER,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_health_ts ON health_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_sentinel_ts ON sentinel_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_health_summary(date);
        """)
        conn.commit()
        # 全新库必须立刻补齐迁移列（input_idle_seconds/GPS/steps 等），
        # 否则首个 tick 写入快照时缺列报错。migrate 幂等，老库重复调用无害。
        try:
            from .schema import migrate as _migrate
            applied = _migrate(conn)
            if applied:
                logger.info(f"HealthStore: 建库迁移完成: {applied}")
        except Exception as e:
            logger.warning(f"HealthStore: 建库迁移失败: {e}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 快照写入 ──────────────────────────────────────

    def insert_snapshot(
        self,
        heart_rate: Optional[int] = None,
        screen_active: Optional[bool] = None,
        foreground_app: Optional[str] = None,
        mirrow_visible: Optional[bool] = None,
        user_status: Optional[str] = None,
        is_period: Optional[bool] = None,
        period_day: Optional[int] = None,
        input_idle_seconds: Optional[float] = None,
        cumulative_steps: Optional[int] = None,
        steps_today: Optional[int] = None,
        location_lat: Optional[float] = None,
        location_lng: Optional[float] = None,
        location_address: Optional[str] = None,
        location_category: Optional[str] = None,
        behavior_state: Optional[str] = None,
        sleep_data: Optional[str] = None,
        screen_time_minutes: Optional[int] = None,
        top_app_category: Optional[str] = None,
        active_app_session: Optional[str] = None,
    ) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO health_snapshots
               (timestamp, heart_rate, screen_active, foreground_app,
                mirrow_visible, user_status, is_period, period_day,
                input_idle_seconds, cumulative_steps, steps_today,
                location_lat, location_lng, location_address, location_category,
                behavior_state, active_app_session, sleep_data, screen_time_minutes, top_app_category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                heart_rate,
                1 if screen_active else 0 if screen_active is not None else None,
                foreground_app,
                1 if mirrow_visible else 0 if mirrow_visible is not None else None,
                user_status,
                1 if is_period else 0 if is_period is not None else None,
                period_day,
                input_idle_seconds,
                cumulative_steps,
                steps_today,
                location_lat,
                location_lng,
                location_address,
                location_category,
                behavior_state,
                active_app_session,
                sleep_data,
                screen_time_minutes,
                top_app_category,
            ),
        )
        conn.commit()
        return cur.lastrowid

    # ── 快照查询 ──────────────────────────────────────

    def get_recent_snapshots(self, lookback_seconds: int = 600) -> List[Dict[str, Any]]:
        """获取最近 N 秒内的快照，用于规则评估"""
        cutoff = (datetime.now() - timedelta(seconds=lookback_seconds)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM health_snapshots WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_heart_rate_history(self, hours: int = 24) -> List[Dict[str, Any]]:
        """获取心率历史（给前端图表 API）"""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT timestamp, heart_rate FROM health_snapshots WHERE heart_rate IS NOT NULL AND timestamp >= ? ORDER BY timestamp ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_heart_rate_stats(self, seconds: int = 300) -> Dict[str, Any]:
        """获取最近 N 秒内心率统计：avg, min, max, count"""
        cutoff = (datetime.now() - timedelta(seconds=seconds)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT AVG(heart_rate) as avg_hr, MIN(heart_rate) as min_hr, MAX(heart_rate) as max_hr, COUNT(*) as cnt FROM health_snapshots WHERE heart_rate IS NOT NULL AND timestamp >= ?",
            (cutoff,),
        ).fetchone()
        if row and row["cnt"] > 0:
            return {
                "avg": round(row["avg_hr"]),
                "min": row["min_hr"],
                "max": row["max_hr"],
                "count": row["cnt"],
            }
        return {"avg": 0, "min": 0, "max": 0, "count": 0}

    def get_hr_stats_recent(self, minutes: int = 5) -> Dict[str, Any]:
        """获取最近 N 分钟内心率统计：avg, peak, count（用于上下文注入）"""
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT AVG(heart_rate) as avg_hr, MAX(heart_rate) as peak_hr, COUNT(*) as cnt FROM health_snapshots WHERE heart_rate IS NOT NULL AND timestamp >= ?",
            (cutoff,),
        ).fetchone()
        if row and row["cnt"] > 0:
            return {
                "avg": round(row["avg_hr"]),
                "peak": row["peak_hr"],
                "count": row["cnt"],
            }
        return {"avg": 0, "peak": 0, "count": 0}

    def get_daily_hr_stats(self, target_date: str = None) -> Dict[str, Any]:
        """获取指定日期全量心率统计：avg, peak, count。
        target_date: YYYY-MM-DD，默认为今天（本地时间）。
        """
        if target_date is None:
            target_date = "date('now', 'localtime')"
        else:
            target_date = f"'{target_date}'"
        conn = self._get_conn()
        row = conn.execute(
            f"SELECT AVG(heart_rate) as avg_hr, MAX(heart_rate) as peak_hr, COUNT(*) as cnt FROM health_snapshots WHERE heart_rate IS NOT NULL AND date(timestamp) = {target_date}",
        ).fetchone()
        if row and row["cnt"] > 0:
            return {
                "avg": round(row["avg_hr"]),
                "peak": row["peak_hr"],
                "count": row["cnt"],
            }
        return {"avg": 0, "peak": 0, "count": 0}

    # ── 哨兵事件 ──────────────────────────────────────

    def insert_event(
        self,
        rule_id: str,
        priority: str,
        snapshot_id: Optional[int] = None,
        message: Optional[str] = None,
        pushed: bool = False,
        event_type: str = "instant",
    ) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO sentinel_events (timestamp, rule_id, priority, snapshot_id, message, pushed, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now_iso(), rule_id, priority, snapshot_id, message, 1 if pushed else 0, event_type),
        )
        conn.commit()
        return cur.lastrowid

    def insert_app_session(self, session_start: str, session_end: str,
                            app_name: str, platform: str, duration_seconds: Optional[int] = None):
        """记录一条 App 使用会话。"""
        if duration_seconds is None:
            try:
                start = datetime.fromisoformat(session_start)
                end = datetime.fromisoformat(session_end)
                duration_seconds = int((end - start).total_seconds())
            except Exception:
                duration_seconds = 0
        if duration_seconds < 30:  # 过滤 <30s 的短暂切换
            return
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO app_sessions (session_start, session_end, app_name, platform, duration_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_start, session_end, app_name, platform, duration_seconds, _now_iso()),
        )
        conn.commit()

    def get_recent_events(self, hours: int = 24) -> List[Dict[str, Any]]:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM sentinel_events WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_push_time(self, rule_id: str) -> Optional[str]:
        """查询某规则最后一次评估的时间（用于冷却判断）。
        含 suppressed 和 LOW priority 事件——只要评估过就进入冷却，防集中爆发。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT timestamp FROM sentinel_events WHERE rule_id = ? ORDER BY timestamp DESC LIMIT 1",
            (rule_id,),
        ).fetchone()
        return row["timestamp"] if row else None

    # ── 作息表 ────────────────────────────────────────

    def upsert_user_schedule(self, work_start=None, work_end=None, lunch_start=None, lunch_end=None,
                              home_lat=None, home_lng=None, work_lat=None, work_lng=None,
                              home_radius_m=None, work_radius_m=None, amap_api_key=None):
        """写入或更新用户作息表（单行表，id=1）。前端保存时双写至此。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO user_schedule (id, work_start, work_end, lunch_start, lunch_end,
                                       home_lat, home_lng, work_lat, work_lng,
                                       home_radius_m, work_radius_m, amap_api_key, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                work_start=excluded.work_start, work_end=excluded.work_end,
                lunch_start=excluded.lunch_start, lunch_end=excluded.lunch_end,
                home_lat=excluded.home_lat, home_lng=excluded.home_lng,
                work_lat=excluded.work_lat, work_lng=excluded.work_lng,
                home_radius_m=excluded.home_radius_m, work_radius_m=excluded.work_radius_m,
                amap_api_key=excluded.amap_api_key,
                updated_at=excluded.updated_at
        """, (work_start, work_end, lunch_start, lunch_end,
              home_lat, home_lng, work_lat, work_lng,
              home_radius_m, work_radius_m, amap_api_key, _now_iso()))
        conn.commit()

    def get_user_schedule(self) -> dict:
        """读取用户作息表（含 GPS 锚点）。"""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM user_schedule WHERE id=1").fetchone()
        if not row:
            return {}
        return {k: row[k] for k in row.keys() if k != "id"}

    def set_hr_device(self, mac=None, name=None):
        """仅更新心率设备 MAC/名称（独立 surgical setter，绝不触碰 GPS/作息列）。
        不能复用 upsert_user_schedule——它 ON CONFLICT 全列覆盖会把 GPS/作息清 NULL。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO user_schedule (id, hr_device_mac, hr_device_name, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hr_device_mac=excluded.hr_device_mac,
                hr_device_name=excluded.hr_device_name,
                updated_at=excluded.updated_at
        """, (mac, name, _now_iso()))
        conn.commit()

    # ── 每日健康汇总 ──────────────────────────────────

    def upsert_daily_summary(self, date: str, **fields):
        """写入或更新某日健康汇总。fields: steps, hr_min, hr_max, hr_avg, sleep_min, spo2_min, spo2_max"""
        conn = self._get_conn()
        cols = ["date"]
        vals = [date]
        updates = []
        for k, v in fields.items():
            if v is not None:
                cols.append(k)
                vals.append(v)
                updates.append(f"{k}=excluded.{k}")
        if not updates:
            return
        updates.append("updated_at=excluded.updated_at")
        vals.append(datetime.now().isoformat())
        cols.append("updated_at")
        placeholders = ",".join("?" * len(cols))
        set_clause = ",".join(updates)
        sql = (
            f"INSERT INTO daily_health_summary ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {set_clause}"
        )
        conn.execute(sql, vals)
        conn.commit()

    def get_daily_summaries(self, from_date: str, to_date: str) -> list:
        """查询日期范围内的每日健康汇总。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM daily_health_summary WHERE date >= ? AND date <= ? ORDER BY date DESC",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 周趋势 ──────────────────────────────────────

    def get_weekly_comparison(self) -> dict:
        """本周 vs 上周步数/睡眠对比。"""
        from datetime import date, timedelta
        today = date.today()
        weekday = today.weekday()  # Monday=0
        this_week_start = (today - timedelta(days=weekday)).isoformat()
        last_week_start = (today - timedelta(days=weekday + 7)).isoformat()
        last_week_end = (today - timedelta(days=weekday + 1)).isoformat()

        conn = self._get_conn()
        this_row = conn.execute(
            "SELECT AVG(steps) as avg_steps, AVG(sleep_min) as avg_sleep FROM daily_health_summary WHERE date >= ? AND date <= ?",
            (this_week_start, today.isoformat()),
        ).fetchone()
        last_row = conn.execute(
            "SELECT AVG(steps) as avg_steps, AVG(sleep_min) as avg_sleep FROM daily_health_summary WHERE date >= ? AND date <= ?",
            (last_week_start, last_week_end),
        ).fetchone()

        result = {}
        if this_row and last_row:
            ts, ls = this_row["avg_steps"], last_row["avg_steps"]
            if ts and ls and ls > 0:
                result["steps_delta"] = round((ts - ls) / ls * 100)
            ts2, ls2 = this_row["avg_sleep"], last_row["avg_sleep"]
            if ts2 and ls2 and ls2 > 0:
                result["sleep_delta"] = round((ts2 - ls2) / ls2 * 100)
        return result

    # ── 天气查询 ──────────────────────────────────────

    def get_today_weather(self) -> Optional[dict]:
        """获取今日天气（供上下文原料函数调用）。无数据时返回 None。"""
        today = datetime.now().strftime("%Y-%m-%d")
        conn = self._get_conn()
        row = conn.execute(
            "SELECT weather_temp, weather_humidity, weather_desc "
            "FROM daily_health_summary WHERE date=? AND weather_temp IS NOT NULL",
            (today,),
        ).fetchone()
        if row:
            return {"temp": row["weather_temp"], "humidity": row["weather_humidity"], "desc": row["weather_desc"]}
        return None

    # ── 睡眠详情查询 ──────────────────────────────────

    def get_sleep_detail(self, date: str = None) -> Optional[dict]:
        """获取指定日期的睡眠详情（含子项）。默认今天。无数据时返回 None。"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        conn = self._get_conn()
        row = conn.execute(
            "SELECT sleep_min, deep_sleep_min, shallow_sleep_min, rem_sleep_min, awake_min, sleep_score "
            "FROM daily_health_summary WHERE date=? AND sleep_min IS NOT NULL",
            (date,),
        ).fetchone()
        if row:
            return dict(row)
        return None

    # ── 清理 ──────────────────────────────────────────

    def cleanup_old_data(self):
        """删除超过保留期的数据"""
        cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
        status_cutoff = (datetime.now() - timedelta(days=180)).isoformat()
        conn = self._get_conn()
        conn.execute("DELETE FROM health_snapshots WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM sentinel_events WHERE timestamp < ?", (cutoff,))
        # status_change_log 保留 180 天（长期分析价值）
        try:
            conn.execute("DELETE FROM status_change_log WHERE timestamp < ?", (status_cutoff,))
        except Exception:
            pass  # 表可能不存在（来自 health_tracker 合并前）
        conn.commit()
        logger.info(f"Sentinel: cleaned up data before {cutoff}")


# ── 模块级单例 ──────────────────────────────────────

_default_store: Optional[HealthStore] = None


def get_store() -> HealthStore:
    """返回 HealthStore 模块级单例。所有外部模块通过此函数访问，不各自 new HealthStore()。"""
    global _default_store
    if _default_store is None:
        _default_store = HealthStore()
    return _default_store
