"""行为基准模块 — 学习用户的个人行为模式，为哨兵提供"正常/异常"参照系。

纯 SQL 聚合，无 LLM 调用。每 tick 轻量查询，每天首次 tick 刷新缓存。
Phase 1: 基线持久化 + check() 结构化返回 + 渐进置信度 + 回复间隔二维细分。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 结构化返回类型
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Deviation:
    """单维度偏离信号。"""
    dimension: str           # heart_rate / steps / reply_interval / pc_active
    current_value: float
    baseline_mean: float
    baseline_stddev: float
    z_score: float
    direction: str           # 'high' | 'low' | 'normal'
    severity: str            # 'normal' | 'mild' | 'moderate' | 'severe'
    description: str         # 中文描述（给 Flash prompt）
    compound_group_id: Optional[str] = None  # Phase 5: 同 tick 多维度偏离共享 UUID


@dataclass
class BaselineResult:
    """check() 的完整返回值 — 所有消费者各取所需。"""
    text: str = ""                              # 合并的中文描述（→ Flash prompt）
    deviations: list = field(default_factory=list)   # list[Deviation]（→ anomaly_detector + 前端）
    compound_group_id: Optional[str] = None     # Phase 5 填充，多维度关联 UUID
    confidence: float = 0.0
    cold_start: bool = True
    trend_signals: dict = field(default_factory=dict)  # {dimension: trend_description}


# ═══════════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════════

class BehaviorProfile:
    """用户的个人行为基准。回答一个问题：「用户现在这样，正常吗？」"""

    def __init__(self, store=None):
        self._store = store  # HealthStore 实例
        self._cache: Dict[str, Any] = {}
        self._last_refresh_date: str = ""
        # 启动时尝试恢复持久化基线
        self._restore_from_db()

    # ── 公开接口 ──────────────────────────────────────

    def check(self, snapshot, last_message_seconds: float) -> BaselineResult:
        """每 tick 调用。返回结构化的基准偏离结果。

        各消费者：
          - result.text        → Flash prompt 注入
          - result.deviations  → anomaly_detector eval 命名空间 + 前端
          - result.confidence  → trigger_detector 冷启动抑制
          - result.cold_start  → 前端冷启动横幅
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_refresh_date:
            self._refresh(today)

        conf = self._overall_confidence()
        result = BaselineResult(confidence=conf, cold_start=conf < 0.7)

        # 置信度前缀（渐进输出，取代二元截断）
        if conf < 0.3:
            prefix = "(基准学习初期·仅供参考) "
        elif conf < 0.7:
            prefix = "(基准初期) "
        else:
            prefix = ""

        hints = []
        deviations = []

        # 心率偏离（陈旧值不参与——防 stale 触发假偏离）
        hr = snapshot.heart_rate
        _is_stale = getattr(snapshot, 'is_stale', None)
        if hr and not (_is_stale and _is_stale("heart_rate", 600)):
            dev = self._check_hr_deviation(hr, datetime.now())
            if dev:
                deviations.append(dev)
                hints.append(dev.description)

        # 回复间隔偏离
        bhv = getattr(snapshot, 'behavior_state', '') or ''
        dev = self._check_silence_deviation(last_message_seconds, datetime.now(), bhv)
        if dev:
            deviations.append(dev)
            hints.append(dev.description)

        # 步数偏离（陈旧值不参与）
        steps = getattr(snapshot, 'steps_today', None)
        if steps is not None and not (_is_stale and _is_stale("steps", 900)):
            dev = self._check_steps_deviation(steps, datetime.now())
            if dev:
                deviations.append(dev)
                hints.append(dev.description)

        # PC 活跃偏离
        idle = getattr(snapshot, 'input_idle_seconds', None)
        if idle is not None:
            dev = self._check_idle_deviation(idle, datetime.now())
            if dev:
                deviations.append(dev)
                hints.append(dev.description)

        if hints:
            result.text = prefix + "。".join(hints)
        result.deviations = deviations

        # 复合信号检测：同 tick 多维度偏离 → 共享 compound_group_id → 升级严重度
        if len(deviations) >= 2:
            import uuid
            cid = str(uuid.uuid4())[:8]
            result.compound_group_id = cid
            for d in deviations:
                d.compound_group_id = cid
                # 多维度同时偏离 → 自动升级严重度
                if d.severity == "mild":
                    d.severity = "moderate"
                elif d.severity == "moderate":
                    d.severity = "severe"

        # 趋势信号（从 trend_calculator 获取，如有）
        try:
            from silicon_perception.analysis.trend_calculator import get_trend_signals
            result.trend_signals = get_trend_signals()
        except Exception:
            pass

        # 写入偏离记录表（有偏离时）
        if deviations and self._store:
            self._log_deviations(deviations)

        return result

    def get_context(self) -> str:
        """供 context_scheduler 使用。返回详细的 7 天行为特征。"""
        conf = self._overall_confidence()
        if conf < 0.3:
            return ""

        lines = ["过去 7 天："]

        sleep = self._cache.get("sleep", {})
        if sleep:
            dur_median = sleep.get("duration_median")
            if dur_median:
                lines.append(f"睡眠 {dur_median/60:.1f}h")

        steps = self._cache.get("steps", {})
        if steps:
            wd = steps.get("workday_mean", 0)
            rd = steps.get("restday_mean", 0)
            if wd or rd:
                lines.append(f"步数 工作日~{wd:.0f} 休息日~{rd:.0f}")

        hr_data = self._cache.get("heart_rate", {})
        if hr_data:
            rest = hr_data.get("resting_mean", 0)
            if rest:
                lines.append(f"静息心率~{rest:.0f}")

        reply = self._cache.get("reply_interval", {})
        if reply:
            over = reply.get("overall_median_min", 0)
            if over:
                lines.append(f"回复间隔~{over:.0f}min")

        if len(lines) == 1:
            return ""
        return " · ".join(lines)

    def get_last_user_message_seconds(self) -> float:
        """返回距离用户最后一条消息的秒数（私聊+群聊取最新）。
        替代 shared_state._last_user_message_time，零 clock 不兼容风险。
        时区安全：统一转本地时区比较，防 naive/aware datetime 混用。"""
        import sqlite3 as _sqlite3
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()
            if not chronicle:
                return 999999.0
            conn = _sqlite3.connect(chronicle.db_path)
            conn.row_factory = _sqlite3.Row
            now = datetime.now().astimezone()
            row = conn.execute(
                "SELECT timestamp FROM conversation_messages WHERE role='user' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            row2 = conn.execute(
                "SELECT created_at FROM group_chat_messages WHERE sender!='K' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            conn.close()

            def _safe_parse(ts_str: str):
                """安全解析时间戳，处理 naive/aware/Z 后缀/+08:00 等所有格式。"""
                if not ts_str:
                    return None
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        # naive → 假定为本地时间
                        from datetime import timezone, timedelta
                        local_offset = now.utcoffset() or timedelta(hours=8)
                        dt = dt.replace(tzinfo=timezone(local_offset))
                    return dt.astimezone(now.tzinfo)
                except Exception:
                    return None

            t1 = _safe_parse(row["timestamp"]) if row else None
            t2 = _safe_parse(row2["created_at"]) if row2 else None
            latest = max(t for t in (t1, t2) if t is not None)
            return max(0, (now - latest).total_seconds())
        except Exception:
            return 999999.0

    def get_debug_view(self) -> dict:
        """供前端面板使用。返回所有维度的基准值和置信度。"""
        result = {"confidence": self._overall_confidence(), "dimensions": {}}
        for dim in ["reply_interval", "heart_rate", "steps", "sleep", "outing", "pc_active", "push_feedback"]:
            data = self._cache.get(dim, {})
            result["dimensions"][dim] = {
                "available": bool(data),
                "sample_days": data.get("sample_days", 0),
                "summary": self._dim_summary(dim, data),
            }
        return result

    # ── 内部：持久化 ────────────────────────────────

    def _restore_from_db(self):
        """启动时从 behavior_baselines 表恢复最近一次基线到内存缓存。"""
        if not self._store:
            return
        try:
            conn = self._store._get_conn()
            # 取最新 computed_at 的所有维度基线
            latest = conn.execute(
                "SELECT computed_at FROM behavior_baselines ORDER BY computed_at DESC LIMIT 1"
            ).fetchone()
            if not latest:
                logger.info("BehaviorProfile: 无持久化基线，冷启动")
                return
            rows = conn.execute(
                "SELECT dimension, period_type, stat_name, stat_value, sample_days, confidence "
                "FROM behavior_baselines WHERE computed_at = ?",
                (latest["computed_at"],)
            ).fetchall()

            # 重建 _cache 结构
            restored = {}
            for r in rows:
                dim = r["dimension"]
                if dim not in restored:
                    restored[dim] = {"sample_days": r["sample_days"]}
                period = r["period_type"]
                stat = r["stat_name"]
                val = r["stat_value"]
                if period == "all":
                    if stat == "sample_days":
                        restored[dim]["sample_days"] = int(val)
                    else:
                        restored[dim][stat] = val
                else:
                    if period not in restored[dim]:
                        restored[dim][period] = {}
                    restored[dim][period][stat] = val

            # 恢复（置信度打 9 折防过期）
            for dim, data in restored.items():
                self._cache[dim] = data
            self._last_refresh_date = latest["computed_at"][:10]

            conf = self._overall_confidence() * 0.9
            logger.info(
                f"BehaviorProfile: 从DB恢复基线 (computed={latest['computed_at']}, "
                f"dims={list(restored.keys())}, conf_restored={conf:.2f})"
            )
        except Exception as e:
            logger.warning(f"BehaviorProfile: 基线恢复失败: {e}")

    def _persist_baselines(self):
        """将当前 _cache 写入 behavior_baselines 表。"""
        if not self._store:
            return
        try:
            conn = self._store._get_conn()
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

            for dim, data in self._cache.items():
                if not data:
                    continue
                sd = data.get("sample_days", 0)
                # 扁平化：all / 各时段 → 每行一条
                for key, val in data.items():
                    if key == "sample_days":
                        conn.execute(
                            "INSERT OR REPLACE INTO behavior_baselines "
                            "(dimension, period_type, stat_name, stat_value, sample_days, confidence, computed_at) "
                            "VALUES (?, 'all', 'sample_days', ?, ?, ?, ?)",
                            (dim, val, sd, self._overall_confidence(), now_iso)
                        )
                    elif isinstance(val, dict):
                        for stat_name, stat_val in val.items():
                            if isinstance(stat_val, (int, float)):
                                conn.execute(
                                    "INSERT OR REPLACE INTO behavior_baselines "
                                    "(dimension, period_type, stat_name, stat_value, sample_days, confidence, computed_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (dim, key, stat_name, stat_val, sd, self._overall_confidence(), now_iso)
                                )
                    elif isinstance(val, (int, float)) and key not in ("sample_days",):
                        conn.execute(
                            "INSERT OR REPLACE INTO behavior_baselines "
                            "(dimension, period_type, stat_name, stat_value, sample_days, confidence, computed_at) "
                            "VALUES (?, 'all', ?, ?, ?, ?, ?)",
                            (dim, key, val, sd, self._overall_confidence(), now_iso)
                        )

            conn.commit()
            logger.info(f"BehaviorProfile: 基线已持久化 ({len(self._cache)} 维度)")
        except Exception as e:
            logger.warning(f"BehaviorProfile: 基线持久化失败: {e}")

    def _log_deviations(self, deviations: list):
        """将偏离信号写入 baseline_deviations 表。
        持续同向偏离：UPDATE repeat_count += 1，保留 first_seen_at 不变。
        新偏离：INSERT，first_seen_at = now, repeat_count = 1。
        """
        try:
            conn = self._store._get_conn()
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            window_ago = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")

            for d in deviations:
                # 查同维度同向 15 分钟内的最近记录
                existing = conn.execute(
                    "SELECT id, repeat_count, first_seen_at FROM baseline_deviations "
                    "WHERE dimension = ? AND deviation_direction = ? AND last_seen_at >= ? "
                    "ORDER BY last_seen_at DESC LIMIT 1",
                    (d.dimension, d.direction, window_ago)
                ).fetchone()

                if existing:
                    new_count = (existing["repeat_count"] or 1) + 1
                    conn.execute(
                        "UPDATE baseline_deviations SET current_value = ?, z_score = ?, "
                        "severity = ?, last_seen_at = ?, repeat_count = ?, "
                        "timestamp = ? WHERE id = ?",
                        (d.current_value, d.z_score, d.severity, now_iso, new_count,
                         now_iso, existing["id"])
                    )
                else:
                    conn.execute(
                        "INSERT INTO baseline_deviations "
                        "(timestamp, dimension, current_value, baseline_mean, baseline_stddev, "
                        "z_score, deviation_direction, severity, compound_group_id, "
                        "first_seen_at, repeat_count, last_seen_at, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (now_iso, d.dimension, d.current_value, d.baseline_mean,
                         d.baseline_stddev, d.z_score, d.direction, d.severity,
                         getattr(d, 'compound_group_id', None),
                         now_iso, 1, now_iso, now_iso)
                    )
            conn.commit()
        except Exception as e:
            logger.debug(f"BehaviorProfile: 偏离记录写入失败: {e}")

    # ── 内部：缓存刷新 ────────────────────────────────

    def _refresh(self, today: str):
        """每天首次 tick 时刷新所有维度的缓存（包裹 asyncio.to_thread 防阻塞）。"""
        if not self._store:
            return
        try:
            self._cache["reply_interval"] = self._compute_reply_interval()
            self._cache["heart_rate"] = self._compute_heart_rate()
            self._cache["steps"] = self._compute_steps()
            self._cache["sleep"] = self._compute_sleep()
            self._cache["outing"] = self._compute_outing()
            self._cache["pc_active"] = self._compute_pc_active()
            self._cache["push_feedback"] = self._compute_push_feedback()
            self._last_refresh_date = today
            logger.info(f"BehaviorProfile: 基准已刷新 (confidence={self._overall_confidence():.2f})")
            # 持久化到 DB
            self._persist_baselines()
            # 触发趋势计算
            try:
                from silicon_perception.analysis.trend_calculator import compute_trends
                compute_trends(self._store)
            except Exception as e:
                logger.debug(f"BehaviorProfile: 趋势计算跳过: {e}")
            # 基线漂移检测（每天一次，基线刷新后）
            self._check_baseline_drift()
        except Exception as e:
            logger.warning(f"BehaviorProfile: 刷新失败: {e}")

    # ── 内部：各维度计算 ──────────────────────────────

    def _compute_reply_interval(self) -> dict:
        """统计用户回复间隔（时段 + 行为状态，二维独立不交叉）。"""
        import sqlite3 as _sqlite3
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()
            if not chronicle:
                return {}
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            with _sqlite3.connect(chronicle.db_path) as conn:
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(
                    "SELECT timestamp FROM conversation_messages "
                    "WHERE role='user' AND active_date >= ? ORDER BY timestamp",
                    (since,)
                ).fetchall()
            if len(rows) < 5:
                return {"sample_days": 0}

            # 批量获取 behavior_state：一次查询所有相关时间点的状态
            ts_list = [r["timestamp"] for r in rows]
            bhv_map = {}
            try:
                store_conn = self._store._get_conn()
                store_conn.row_factory = _sqlite3.Row
                for ts in ts_list:
                    bhv_row = store_conn.execute(
                        "SELECT behavior_state FROM health_snapshots "
                        "WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                        (ts,)
                    ).fetchone()
                    if bhv_row and bhv_row["behavior_state"]:
                        bhv_map[ts] = bhv_row["behavior_state"]
            except Exception:
                pass

            # 计算间隔，双维度分组
            all_intervals = []
            period_intervals = {"morning": [], "afternoon": [], "evening": [], "night": []}
            behavior_intervals = {}  # key = behavior_state string

            for i in range(1, len(rows)):
                try:
                    t1 = datetime.fromisoformat(rows[i-1]["timestamp"])
                    t2 = datetime.fromisoformat(rows[i]["timestamp"])
                    gap = (t2 - t1).total_seconds() / 60
                    if 0 < gap < 1440:
                        all_intervals.append(gap)
                        h = t2.hour
                        # 维度A：时段
                        if 6 <= h < 12: period_intervals["morning"].append(gap)
                        elif 12 <= h < 18: period_intervals["afternoon"].append(gap)
                        elif 18 <= h < 24: period_intervals["evening"].append(gap)
                        else: period_intervals["night"].append(gap)
                        # 维度B：真实行为状态（来自 health_snapshots）
                        ts_str = rows[i]["timestamp"]
                        bhv = bhv_map.get(ts_str, "unknown")
                        if bhv not in behavior_intervals:
                            behavior_intervals[bhv] = []
                        behavior_intervals[bhv].append(gap)
                except Exception:
                    pass

            if not all_intervals:
                return {"sample_days": 0}

            def _interval_stats(vals):
                if not vals: return {}
                vals.sort()
                n = len(vals)
                return {
                    "median_min": vals[n // 2],
                    "p25_min": vals[n // 4],
                    "p75_min": vals[3 * n // 4],
                    "count": n,
                }

            result = {
                "sample_days": min(30, (datetime.now() - datetime.fromisoformat(since)).days),
                "overall": _interval_stats(all_intervals),
            }
            for period, vals in period_intervals.items():
                if vals:
                    result[period] = _interval_stats(vals)
            for bhv, vals in behavior_intervals.items():
                if vals and len(vals) >= 3:  # 最少3个样本才建基线
                    result[f"bhv_{bhv}"] = _interval_stats(vals)

            # 向后兼容顶层字段
            overall = result.get("overall", {})
            if overall:
                result["overall_median_min"] = overall.get("median_min", 0)
                result["p25_min"] = overall.get("p25_min", 0)
                result["p75_min"] = overall.get("p75_min", 0)

            return result
        except Exception as e:
            logger.debug(f"BehaviorProfile: reply_interval 计算失败: {e}")
            return {}

    def _compute_heart_rate(self) -> dict:
        """统计心率按时段分布（trimmed mean 防异常值污染）。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=7)).isoformat()
            rows = conn.execute(
                "SELECT heart_rate, strftime('%H', timestamp) as hour FROM health_snapshots "
                "WHERE heart_rate IS NOT NULL AND timestamp >= ?",
                (since,)
            ).fetchall()
            if len(rows) < 10:
                return {"sample_days": 0}

            morning = []; afternoon = []; evening = []; night = []; all_vals = []
            for r in rows:
                h = int(r["hour"]); v = r["heart_rate"]
                all_vals.append(v)
                if 6 <= h < 12: morning.append(v)
                elif 12 <= h < 18: afternoon.append(v)
                elif 18 <= h < 24: evening.append(v)
                else: night.append(v)

            def _stats(vals):
                if not vals: return {}
                vals.sort()
                n = len(vals)
                if n >= 20:
                    trim = max(1, n // 20)
                    vals = vals[trim:-trim]
                    n = len(vals)
                return {
                    "mean": sum(vals) / n,
                    "median": vals[n // 2],
                    "p75": vals[3 * n // 4] if n >= 4 else vals[-1],
                    "p25": vals[n // 4] if n >= 4 else vals[0],
                    "stddev": (sum((v - sum(vals)/n)**2 for v in vals) / n) ** 0.5,
                    "count": n,
                }

            return {
                "sample_days": min(7, (datetime.now() - datetime.fromisoformat(since)).days),
                "all": _stats(all_vals),
                "morning": _stats(morning),
                "afternoon": _stats(afternoon),
                "evening": _stats(evening),
                "night": _stats(night),
                "resting_mean": sum(sorted(all_vals)[:len(all_vals)//3]) / max(1, len(all_vals)//3) if all_vals else 0,
            }
        except Exception as e:
            logger.debug(f"BehaviorProfile: heart_rate 计算失败: {e}")
            return {}

    def _compute_steps(self) -> dict:
        """统计步数（工作日/休息日）。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT date, steps FROM daily_health_summary WHERE date >= ? AND steps > 0",
                (since,)
            ).fetchall()
            if len(rows) < 3:
                return {"sample_days": len(rows)}

            rest_dates = self._get_rest_dates(since)
            wd_steps = []; rd_steps = []
            for r in rows:
                if r["date"] in rest_dates:
                    rd_steps.append(r["steps"])
                else:
                    wd_steps.append(r["steps"])

            return {
                "sample_days": len(rows),
                "workday_mean": sum(wd_steps) / max(1, len(wd_steps)),
                "restday_mean": sum(rd_steps) / max(1, len(rd_steps)),
                "overall_mean": (sum(wd_steps) + sum(rd_steps)) / max(1, len(wd_steps) + len(rd_steps)),
            }
        except Exception as e:
            logger.debug(f"BehaviorProfile: steps 计算失败: {e}")
            return {}

    def _compute_sleep(self) -> dict:
        """统计睡眠模式。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT sleep_min FROM daily_health_summary WHERE date >= ? AND sleep_min > 0",
                (since,)
            ).fetchall()
            if len(rows) < 3:
                return {"sample_days": len(rows)}
            durations = sorted(r["sleep_min"] for r in rows)
            n = len(durations)
            return {
                "sample_days": n,
                "duration_median": durations[n // 2],
                "duration_mean": sum(durations) / n,
                "duration_min": durations[0],
                "duration_max": durations[-1],
            }
        except Exception:
            return {}

    def _compute_outing(self) -> dict:
        """统计外出模式。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT timestamp, new_status FROM status_change_log "
                "WHERE timestamp >= ? AND (new_status='out' OR new_status='sleeping') ORDER BY timestamp",
                (since,)
            ).fetchall()
            if not rows:
                return {"sample_days": 0}
            days_with_out = set()
            for r in rows:
                days_with_out.add(r["timestamp"][:10])
            sample_days = min(30, (datetime.now() - datetime.fromisoformat(since)).days)
            return {
                "sample_days": sample_days,
                "outing_days": len(days_with_out),
                "outing_freq": len(days_with_out) / max(1, sample_days),
            }
        except Exception:
            return {}

    def _compute_pc_active(self) -> dict:
        """统计 PC 活跃时段。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=7)).isoformat()
            rows = conn.execute(
                "SELECT input_idle_seconds, strftime('%H', timestamp) as hour FROM health_snapshots "
                "WHERE input_idle_seconds IS NOT NULL AND timestamp >= ?",
                (since,)
            ).fetchall()
            if len(rows) < 10:
                return {"sample_days": 0}
            active_by_hour = {}
            for r in rows:
                h = r["hour"]
                if h not in active_by_hour:
                    active_by_hour[h] = [0, 0]
                active_by_hour[h][1] += 1
                if r["input_idle_seconds"] < 300:
                    active_by_hour[h][0] += 1
            return {
                "sample_days": min(7, (datetime.now() - datetime.fromisoformat(since)).days),
                "active_hours": {h: round(c[0] / max(1, c[1]), 2) for h, c in active_by_hour.items()},
            }
        except Exception:
            return {}

    def _compute_push_feedback(self) -> dict:
        """统计推送的回复率。"""
        try:
            conn = self._store._get_conn()
            since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT rule_id, timestamp FROM sentinel_events "
                "WHERE pushed=1 AND date(timestamp) >= ? ORDER BY timestamp",
                (since,)
            ).fetchall()
            if not rows:
                return {"sample_days": 0}
            by_type = {}
            for r in rows:
                t = r["rule_id"]
                by_type[t] = by_type.get(t, 0) + 1
            return {
                "sample_days": min(7, (datetime.now() - datetime.strptime(since, "%Y-%m-%d")).days),
                "total_pushes": len(rows),
                "by_type": by_type,
            }
        except Exception:
            return {}

    # ── 内部：工具函数 ────────────────────────────────

    @staticmethod
    def _get_rest_dates(since: str) -> set:
        """获取 since 至今的所有休息日日期集合（周末 + 日历自定义休息日）。"""
        rest_dates = set()
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
            today = datetime.now()
            d = since_dt
            while d <= today:
                if d.weekday() >= 5:
                    rest_dates.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            from calendar_manager.database import _get_conn as _cal_conn
            cal = _cal_conn()
            rows = cal.execute(
                "SELECT date_string FROM calendar_events WHERE event_type='rest_day' AND is_active=1 AND date_string >= ?",
                (since,)
            ).fetchall()
            for r in rows:
                rest_dates.add(r["date_string"])
        except Exception:
            pass
        return rest_dates

    def _make_deviation(self, dimension: str, current: float, baseline_mean: float,
                        baseline_stddev: float, direction: str, description: str) -> Deviation:
        """构建 Deviation，自动计算 z_score 和 severity。"""
        z = (current - baseline_mean) / max(0.01, baseline_stddev)
        az = abs(z)
        if az < 1.0:
            severity = "normal"
        elif az < 2.0:
            severity = "mild"
        elif az < 3.0:
            severity = "moderate"
        else:
            severity = "severe"
        return Deviation(
            dimension=dimension, current_value=current, baseline_mean=baseline_mean,
            baseline_stddev=baseline_stddev, z_score=round(z, 2),
            direction=direction, severity=severity, description=description,
        )

    # ── 内部：偏离检查 ────────────────────────────────

    def _check_hr_deviation(self, hr: int, now: datetime) -> Optional[Deviation]:
        data = self._cache.get("heart_rate", {})
        if not data:
            return None
        h = now.hour
        if 6 <= h < 12: period = "morning"
        elif 12 <= h < 18: period = "afternoon"
        elif 18 <= h < 24: period = "evening"
        else: period = "night"
        stats = data.get(period, data.get("all", {}))
        if not stats:
            return None
        mean = stats.get("mean", 0)
        stddev = stats.get("stddev", 10)
        if mean < 1:
            return None
        diff = hr - mean
        if abs(diff) < 10:
            return None
        direction = "high" if diff > 0 else "low"
        desc = f"心率{hr}，比此时段均值{'高' if diff > 0 else '低'}{abs(diff):.0f}"
        return self._make_deviation("heart_rate", hr, mean, stddev, direction, desc)

    def _check_silence_deviation(self, silence_sec: float, now: datetime, behavior_state: str = "") -> Optional[Deviation]:
        data = self._cache.get("reply_interval", {})
        if not data:
            return None

        # 维度A：时段
        h = now.hour
        if 6 <= h < 12: period_key, period_cn = "morning", "早上"
        elif 12 <= h < 18: period_key, period_cn = "afternoon", "下午"
        elif 18 <= h < 24: period_key, period_cn = "evening", "晚上"
        else: period_key, period_cn = "night", "深夜"
        period_stats = data.get(period_key, {})

        # 维度B：真实行为状态
        bhv_key = f"bhv_{behavior_state}" if behavior_state else ""
        bhv_stats = data.get(bhv_key, {}) if bhv_key else {}
        bhv_label = {"pc_work_dev":"写代码","pc_work_office":"办公","pc_entertainment":"玩游戏",
                      "pc_browser":"上网","pc_passive":"看视频","phone_social":"刷社交",
                      "phone_game":"手游","phone_video":"看视频","phone_mirrow":"看MIRROW",
                      "phone_active":"玩手机","both_active":"多设备","both_idle":"休息中"}.get(behavior_state, behavior_state)

        # 双维度取交集构建范围
        medians = []
        p75s = []
        labels = [period_cn]
        if period_stats and period_stats.get("median_min", 0) >= 1:
            medians.append(period_stats["median_min"])
            p75s.append(period_stats["p75_min"])
        if bhv_stats and bhv_stats.get("median_min", 0) >= 1:
            medians.append(bhv_stats["median_min"])
            p75s.append(bhv_stats["p75_min"])
            labels.append(bhv_label)

        if not medians:
            # 回退整体
            overall = data.get("overall", {})
            m = overall.get("median_min", data.get("overall_median_min", 0))
            p = overall.get("p75_min", data.get("p75_min", 0))
            if m < 1:
                return None
            medians = [m]
            p75s = [p]
            labels = ["全天"]

        median = sum(medians) / len(medians)
        p75 = sum(p75s) / len(p75s)
        range_min = min(medians)
        range_max = max(medians)
        range_str = f"{range_min:.0f}-{range_max:.0f}min" if range_max > range_min + 1 else f"{median:.0f}min"
        state_str = " · ".join(labels)

        silence_min = silence_sec / 60

        if silence_min < p75 * 1.5:
            direction = "normal"
            desc = f"现在是{state_str}，常规回复间隔{range_str}，已沉默{silence_min:.0f}min（正常范围）"
        else:
            direction = "high"
            desc = f"现在是{state_str}，常规回复间隔{range_str}，已沉默{silence_min:.0f}min（偏长）"
        return self._make_deviation("reply_interval", silence_min, median, max(p75 - median, 0.1), direction, desc)

    def _check_steps_deviation(self, steps: int, now: datetime) -> Optional[Deviation]:
        data = self._cache.get("steps", {})
        if not data:
            return None
        is_weekend = now.weekday() >= 5
        baseline = data.get("restday_mean" if is_weekend else "workday_mean", data.get("overall_mean", 0))
        if baseline < 1:
            return None
        stddev = baseline * 0.3  # 估算 stddev
        ratio = steps / baseline
        if ratio < 0.3:
            desc = f"步数{steps}，通常{baseline:.0f}（严重偏低）"
        elif ratio < 0.6:
            desc = f"步数{steps}，通常{baseline:.0f}（偏低）"
        else:
            return None
        return self._make_deviation("steps", steps, baseline, stddev, "low", desc)

    def _check_idle_deviation(self, idle_sec: float, now: datetime) -> Optional[Deviation]:
        data = self._cache.get("pc_active", {})
        if not data:
            return None
        hour_key = str(now.hour).zfill(2)
        active_ratio = data.get("active_hours", {}).get(hour_key, 0.5)
        idle_min = idle_sec / 60
        if active_ratio > 0.5 and idle_min > 60:
            desc = f"已空闲{idle_min:.0f}min，此时段通常活跃"
            return self._make_deviation("pc_active", idle_min, 30, 30, "high", desc)
        return None

    # ── 内部：基线漂移检测 ──────────────────────────

    def _check_baseline_drift(self):
        """检测基线是否出现长期同向漂移（生活方式改变而非异常）。
        连续 >=14 天同向偏离 → 标记 drift → 21 天持续 → 自动重基线。
        """
        if not self._store:
            return
        try:
            conn = self._store._get_conn()
            since_21 = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")
            for dim in ["heart_rate", "steps", "reply_interval"]:
                rows = conn.execute(
                    "SELECT deviation_direction, COUNT(*) as cnt FROM baseline_deviations "
                    "WHERE dimension = ? AND created_at >= ? GROUP BY deviation_direction",
                    (dim, since_21)
                ).fetchall()
                if len(rows) < 2:
                    continue
                total = sum(r["cnt"] for r in rows)
                if total < 14:
                    continue  # 样本不足
                for r in rows:
                    ratio = r["cnt"] / total if total > 0 else 0
                    direction = r["deviation_direction"]
                    if ratio > 0.8 and direction in ("high", "low"):
                        current_drift = self._cache.get("_drift", {})
                        current_drift[dim] = {
                            "direction": direction,
                            "ratio": round(ratio, 2),
                            "total_days": total,
                            "detected_at": datetime.now().strftime("%Y-%m-%d"),
                        }
                        self._cache["_drift"] = current_drift
                        logger.info(
                            f"BehaviorProfile: 基线漂移检测到 {dim} {direction} "
                            f"(ratio={ratio:.1%}, days={total})"
                        )
                        # 21 天持续 → 缩短回看窗口（强调近期数据）
                        if total >= 21:
                            logger.info(f"BehaviorProfile: {dim} 基线漂移确认，建议重基线")
        except Exception as e:
            logger.debug(f"BehaviorProfile: 漂移检测失败: {e}")

    # ── 内部：辅助 ────────────────────────────────────

    def _overall_confidence(self) -> float:
        """0.0~1.0，基于数据量和各维度样本的综合置信度。"""
        total = 0
        weights = {
            "reply_interval": 0.25,
            "heart_rate": 0.25,
            "steps": 0.20,
            "sleep": 0.15,
            "pc_active": 0.10,
            "push_feedback": 0.05,
        }
        for dim, w in weights.items():
            days = self._cache.get(dim, {}).get("sample_days", 0)
            total += w * min(1.0, days / 7.0)
        return round(total, 2)

    def _dim_summary(self, dim: str, data: dict) -> str:
        """单维度的人类可读摘要。"""
        if not data:
            return "无数据"
        if dim == "reply_interval":
            m = data.get("overall_median_min", data.get("overall", {}).get("median_min", 0))
            return f"回复间隔中位数 {m:.0f}min" if m else "无数据"
        elif dim == "heart_rate":
            m = data.get("all", {}).get("mean", 0)
            return f"平均心率 {m:.0f}" if m else "无数据"
        elif dim == "steps":
            w = data.get("workday_mean", 0)
            return f"工作日均 {w:.0f}步" if w else "无数据"
        elif dim == "sleep":
            m = data.get("duration_median", 0)
            return f"睡眠中位数 {m/60:.1f}h" if m else "无数据"
        elif dim == "outing":
            d = data.get("outing_days", 0)
            return f"{d}天外出" if d else "无数据"
        elif dim == "push_feedback":
            t = data.get("total_pushes", 0)
            return f"{t}次推送" if t else "无数据"
        return str(data)


# ═══════════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════════

_behavior_profile: Optional[BehaviorProfile] = None


def get_behavior_profile() -> BehaviorProfile:
    global _behavior_profile
    if _behavior_profile is None:
        from silicon_perception.recording.health_store import get_store
        _behavior_profile = BehaviorProfile(store=get_store())
    return _behavior_profile
