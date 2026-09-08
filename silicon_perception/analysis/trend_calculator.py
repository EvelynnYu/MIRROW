"""趋势记忆计算器 — 从 daily_health_summary 计算各维度的长期趋势。

纯 Python OLS 线性回归，零外部依赖。每天基线刷新后运行一次。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# 内存缓存（每天一变，缓存 1 小时避免重复算）
_trend_cache: Dict[str, Any] = {}
_last_computed: str = ""  # YYYY-MM-DD


def compute_trends(store) -> dict:
    """计算所有维度的 7 天和 30 天趋势。返回 {dimension: {window_days: {...}}}"""
    global _last_computed, _trend_cache
    today = datetime.now().strftime("%Y-%m-%d")
    if today == _last_computed and _trend_cache:
        return _trend_cache

    try:
        conn = store._get_conn()
        trends = {}

        # 步数趋势
        steps_trend = _compute_dimension_trend(conn, "steps", "daily_health_summary", "steps", "date")
        if steps_trend:
            trends["steps"] = steps_trend

        # 睡眠趋势
        sleep_trend = _compute_dimension_trend(conn, "sleep", "daily_health_summary", "sleep_min", "date")
        if sleep_trend:
            trends["sleep"] = sleep_trend

        # 心率趋势（从 daily_health_summary.hr_avg）
        hr_trend = _compute_dimension_trend(conn, "heart_rate", "daily_health_summary", "hr_avg", "date")
        if hr_trend:
            trends["heart_rate"] = hr_trend

        # 持久化
        _persist_trends(conn, trends)

        _trend_cache = trends
        _last_computed = today
        logger.info(f"TrendCalculator: 趋势已更新 ({len(trends)} 维度)")
        return trends
    except Exception as e:
        logger.warning(f"TrendCalculator: 趋势计算失败: {e}")
        return _trend_cache if _trend_cache else {}


def get_trend_signals() -> dict:
    """供 behavior_profile.check() 调用的轻量接口。返回 {dimension: description}"""
    if not _trend_cache:
        return {}
    signals = {}
    for dim, windows in _trend_cache.items():
        for wdays, trend in windows.items():
            if trend.get("direction") in ("rising", "falling") and trend.get("r2", 0) > 0.3:
                direction_cn = "上升" if trend["direction"] == "rising" else "下降"
                slope = trend.get("slope", 0)
                signals[dim] = f"{dim}连续{wdays}天{direction_cn}（日均{slope:+.1f}），趋势置信度{trend.get('r2', 0):.1f}"
                break  # 优先用 7 天信号
    return signals


def _compute_dimension_trend(conn, dimension: str, table: str, column: str, date_col: str) -> dict:
    """对单个维度的 30 天数据做 7 天和 30 天窗口的 OLS 线性回归。"""
    try:
        since_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = conn.execute(
            f"SELECT {date_col}, {column} FROM {table} WHERE {date_col} >= ? AND {column} > 0 ORDER BY {date_col}",
            (since_30,)
        ).fetchall()
        if len(rows) < 7:
            return {}

        values = [(i, r[column]) for i, r in enumerate(rows) if r[column] is not None]
        if len(values) < 7:
            return {}

        result = {}
        for window in (7, 30):
            window_vals = values[-window:] if len(values) >= window else values
            if len(window_vals) < 5:
                continue
            trend = _ols_trend(window_vals)
            if trend:
                result[window] = trend

        return result
    except Exception as e:
        logger.debug(f"TrendCalculator: {dimension} 趋势计算失败: {e}")
        return {}


def _ols_trend(values: list) -> Optional[dict]:
    """普通最小二乘线性回归。values: [(index, y), ...]"""
    n = len(values)
    if n < 5:
        return None

    x_vals = [v[0] for v in values]
    y_vals = [v[1] for v in values]

    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n

    # slope = Σ((x-x̄)*(y-ȳ)) / Σ((x-x̄)²)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    den = sum((x - x_mean) ** 2 for x in x_vals)

    if den == 0:
        return None

    slope = num / den
    intercept = y_mean - slope * x_mean

    # R² = 1 - (SS_res / SS_tot)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(x_vals, y_vals))
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    r2 = max(0, min(1, r2))  # clamp

    # 方向
    if r2 < 0.2 or abs(slope) < (y_mean * 0.01 if y_mean > 0 else 0.5):
        direction = "stable"
    elif slope > 0:
        direction = "rising"
    else:
        direction = "falling"

    # 归一化趋势强度
    strength = min(1.0, abs(slope) / max(0.01, y_mean)) if y_mean > 0 else 0

    if n < 7:
        direction = "uncertain"

    return {
        "mean_value": y_mean,
        "std_dev": (sum((y - y_mean) ** 2 for y in y_vals) / n) ** 0.5,
        "slope": slope,
        "r2": round(r2, 2),
        "direction": direction,
        "strength": round(strength, 3),
    }


def _persist_trends(conn, trends: dict):
    """将趋势结果写入 behavior_trends 表。"""
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        for dim, windows in trends.items():
            for wdays, trend in windows.items():
                window_start = (datetime.now() - timedelta(days=wdays)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO behavior_trends "
                    "(dimension, metric, period_type, window_start, window_days, "
                    "mean_value, std_dev, trend_slope, trend_r2, trend_direction, trend_strength, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (dim, f"daily_{dim}", "all", window_start, wdays,
                     trend["mean_value"], trend.get("std_dev"), trend["slope"],
                     trend["r2"], trend["direction"], trend["strength"], now_iso)
                )
        conn.commit()
        logger.debug(f"TrendCalculator: 趋势已持久化 ({len(trends)} 维度)")
    except Exception as e:
        logger.warning(f"TrendCalculator: 趋势持久化失败: {e}")
