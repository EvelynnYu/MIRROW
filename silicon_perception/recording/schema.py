"""数据库 Schema 定义与安全迁移

所有 DDL 集中在此，使用 PRAGMA table_info 检查后再 ALTER，
避免重复迁移报错。
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


def _column_exists(cursor, table: str, column: str) -> bool:
    """检查表中是否已有某列。"""
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _table_exists(cursor, table: str) -> bool:
    """检查表是否存在。"""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def migrate(conn) -> List[str]:
    """执行所有迁移，返回已应用的迁移列表。"""
    applied = []
    cursor = conn.cursor()

    # ── health_snapshots 新增列 ──
    snapshots_new_cols = [
        ("input_idle_seconds", "REAL"),
        ("cumulative_steps", "INTEGER"),
        ("steps_today", "INTEGER"),
        ("location_lat", "REAL"),
        ("location_lng", "REAL"),
        ("location_address", "TEXT"),
        ("location_category", "TEXT"),
    ]
    for col_name, col_type in snapshots_new_cols:
        if not _column_exists(cursor, "health_snapshots", col_name):
            cursor.execute(
                f"ALTER TABLE health_snapshots ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"health_snapshots.{col_name}")
            logger.info(f"[Sentinel Schema] 新增列: health_snapshots.{col_name}")

    # ── sentinel_events 新增列 ──
    events_new_cols = [
        ("event_type", "TEXT DEFAULT 'instant'"),
        ("injected_to_context", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in events_new_cols:
        if not _column_exists(cursor, "sentinel_events", col_name):
            cursor.execute(
                f"ALTER TABLE sentinel_events ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"sentinel_events.{col_name}")
            logger.info(f"[Sentinel Schema] 新增列: sentinel_events.{col_name}")

    # ── daily_health_summary 新增列 ──
    daily_new_cols = [
        ("outing_steps", "INTEGER DEFAULT 0"),
        ("home_steps", "INTEGER DEFAULT 0"),
        ("avg_steps_7d", "REAL"),
        ("avg_outing_steps_7d", "REAL"),
        ("hourly_peaks", "TEXT"),
        # 🆕 天气 (L1 聚合层, 日级)
        ("weather_temp", "INTEGER"),
        ("weather_humidity", "INTEGER"),
        ("weather_desc", "TEXT"),
        # 🆕 睡眠子项 (L1 聚合层, 从 health_snapshots.sleep_data JSON 提升)
        ("deep_sleep_min", "INTEGER"),
        ("shallow_sleep_min", "INTEGER"),
        ("rem_sleep_min", "INTEGER"),
        ("awake_min", "INTEGER"),
        ("sleep_score", "INTEGER"),
    ]
    for col_name, col_type in daily_new_cols:
        if not _column_exists(cursor, "daily_health_summary", col_name):
            cursor.execute(
                f"ALTER TABLE daily_health_summary ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"daily_health_summary.{col_name}")
            logger.info(f"[Sentinel Schema] 新增列: daily_health_summary.{col_name}")

    # ── 新表: sleep_window ──
    if not _table_exists(cursor, "sleep_window"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sleep_window (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                updated_at TEXT NOT NULL,
                bedtime_earliest TEXT NOT NULL,
                bedtime_latest TEXT NOT NULL,
                wake_earliest TEXT NOT NULL,
                wake_latest TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                sample_days INTEGER DEFAULT 0
            )
        """)
        applied.append("sleep_window (new table)")
        logger.info("[Sentinel Schema] 新表: sleep_window")

    # ── 新表: user_schedule ──
    if not _table_exists(cursor, "user_schedule"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_schedule (
                id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                work_start TEXT,
                work_end TEXT,
                lunch_start TEXT,
                lunch_end TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        applied.append("user_schedule (new table)")
        logger.info("[Sentinel Schema] 新表: user_schedule")

    # user_schedule 新增 GPS 列（SQLite 主存储，JSON 降为缓存）
    schedule_gps_cols = [
        ("home_lat", "REAL"),
        ("home_lng", "REAL"),
        ("work_lat", "REAL"),
        ("work_lng", "REAL"),
        ("home_radius_m", "INTEGER DEFAULT 200"),
        ("work_radius_m", "INTEGER DEFAULT 200"),
        ("amap_api_key", "TEXT"),
        ("hr_device_mac", "TEXT"),
        ("hr_device_name", "TEXT"),
    ]
    for col_name, col_type in schedule_gps_cols:
        if not _column_exists(cursor, "user_schedule", col_name):
            cursor.execute(
                f"ALTER TABLE user_schedule ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"user_schedule.{col_name}")
            logger.info(f"[Sentinel Schema] 新增列: user_schedule.{col_name}")

    # ── 新表: behavior_baselines ──
    if not _table_exists(cursor, "behavior_baselines"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS behavior_baselines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                period_type TEXT NOT NULL DEFAULT 'all',
                stat_name TEXT NOT NULL,
                stat_value REAL NOT NULL,
                sample_size INTEGER DEFAULT 0,
                sample_days INTEGER DEFAULT 0,
                lookback_days INTEGER DEFAULT 30,
                confidence REAL DEFAULT 0.0,
                computed_at TEXT NOT NULL,
                UNIQUE(dimension, period_type, stat_name, lookback_days, computed_at)
            )
        """)
        applied.append("behavior_baselines (new table)")
        logger.info("[Sentinel Schema] 新表: behavior_baselines")

    # ── 新表: behavior_trends ──
    if not _table_exists(cursor, "behavior_trends"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS behavior_trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                metric TEXT NOT NULL,
                period_type TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                mean_value REAL NOT NULL,
                std_dev REAL,
                trend_slope REAL,
                trend_r2 REAL,
                trend_direction TEXT NOT NULL,
                trend_strength REAL,
                computed_at TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trends_dim_window ON behavior_trends(dimension, window_days, window_start)"
        )
        applied.append("behavior_trends (new table)")
        logger.info("[Sentinel Schema] 新表: behavior_trends")

    # ── 新表: baseline_deviations ──
    if not _table_exists(cursor, "baseline_deviations"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS baseline_deviations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                dimension TEXT NOT NULL,
                current_value REAL,
                baseline_mean REAL,
                baseline_stddev REAL,
                z_score REAL,
                deviation_direction TEXT,
                severity TEXT,
                compound_group_id TEXT,
                injected_to_flash INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_deviations_ts ON baseline_deviations(timestamp)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_deviations_group ON baseline_deviations(compound_group_id)"
        )
        applied.append("baseline_deviations (new table)")
        logger.info("[Sentinel Schema] 新表: baseline_deviations")

    # baseline_deviations 去重升级：加 first_seen_at / repeat_count / last_seen_at
    dev_dedup_cols = [
        ("first_seen_at", "TEXT"),
        ("repeat_count", "INTEGER DEFAULT 1"),
        ("last_seen_at", "TEXT"),
    ]
    for col_name, col_type in dev_dedup_cols:
        if not _column_exists(cursor, "baseline_deviations", col_name):
            cursor.execute(
                f"ALTER TABLE baseline_deviations ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"baseline_deviations.{col_name}")
            logger.info(f"[Sentinel Schema] 新增列: baseline_deviations.{col_name}")

    # ── health_snapshots 新列 (Phase 1.5 / Phase 3 使用) ──
    snapshot_future_cols = [
        ("behavior_state", "TEXT"),
        ("active_app_session", "TEXT"),
        ("screen_time_minutes", "INTEGER"),
        ("top_app_category", "TEXT"),
        ("sleep_data", "TEXT"),
    ]
    for col_name, col_type in snapshot_future_cols:
        if not _column_exists(cursor, "health_snapshots", col_name):
            cursor.execute(
                f"ALTER TABLE health_snapshots ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"health_snapshots.{col_name}")

    # ── daily_health_summary 新列 (Phase 1/3 使用) ──
    daily_future_cols = [
        ("pc_active_min", "INTEGER DEFAULT 0"),
        ("reply_count", "INTEGER DEFAULT 0"),
        ("screen_time_total_min", "INTEGER DEFAULT 0"),
        ("screen_time_social_min", "INTEGER DEFAULT 0"),
        ("screen_time_entertainment_min", "INTEGER DEFAULT 0"),
        ("screen_time_mirrow_min", "INTEGER DEFAULT 0"),
        ("screen_time_work_min", "INTEGER DEFAULT 0"),
        ("behavior_chain", "TEXT"),
        ("behavior_state_dist", "TEXT"),
        ("gps_chain", "TEXT"),
    ]
    for col_name, col_type in daily_future_cols:
        if not _column_exists(cursor, "daily_health_summary", col_name):
            cursor.execute(
                f"ALTER TABLE daily_health_summary ADD COLUMN {col_name} {col_type}"
            )
            applied.append(f"daily_health_summary.{col_name}")

    # ── 新表: app_sessions (App 会话跟踪) ──
    if not _table_exists(cursor, "app_sessions"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_start TEXT NOT NULL,
                session_end TEXT,
                app_name TEXT NOT NULL,
                platform TEXT NOT NULL CHECK (platform IN ('pc','phone')),
                duration_seconds INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_sessions_start ON app_sessions(session_start)")
        applied.append("app_sessions (new table)")
        logger.info("[Sentinel Schema] 新表: app_sessions")

    # ── status_change_log（health_tracker 写入，哨兵清理）──
    if not _table_exists(cursor, "status_change_log"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS status_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                active_date TEXT NOT NULL,
                custom_text TEXT DEFAULT ''
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scl_date ON status_change_log(active_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scl_timestamp ON status_change_log(timestamp)")
        applied.append("status_change_log (new table)")
        logger.info("[Sentinel Schema] 新表: status_change_log")

    # ── pending_sync（health_tracker 失败重试队列）──
    if not _table_exists(cursor, "pending_sync"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_sync (
                id TEXT PRIMARY KEY,
                sync_type TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                active_date TEXT NOT NULL,
                injected_msg_id TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ps_active ON pending_sync(status, active_date)")
        applied.append("pending_sync (new table)")
        logger.info("[Sentinel Schema] 新表: pending_sync")

    conn.commit()
    return applied
