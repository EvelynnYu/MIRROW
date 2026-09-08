"""日终数据聚合器 — daily_health_summary 的唯一写入入口

所有聚合查询从 health_snapshots / app_sessions / conversation_messages 读取，
不依赖任何内存缓存（HealthTracker._cache 等），纯数据库查询。

调用时机：
- 日期交接时（HealthTracker._maybe_write_daily_summary → 改调此处）
- 话题结束时（兜底：如果今天还没聚合过）
"""

import asyncio
import json as _json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class DailyAggregator:
    """日终聚合器 — 聚合 health_snapshots → daily_health_summary。"""

    def __init__(self, health_store):
        self._store = health_store

    # ── 主入口 ──────────────────────────────────────────

    def aggregate(self, target_date: str) -> Dict[str, Any]:
        """聚合 target_date 的全天数据，写入 daily_health_summary。返回写入的字段。"""
        fields = {}

        # 步数（从 health_snapshots 取最后值）
        steps = self._aggregate_steps(target_date)
        if steps is not None:
            fields.update(steps)

        # 心率统计
        hr = self._aggregate_hr(target_date)
        if hr:
            fields.update(hr)

        # PC 活跃分钟数
        pc = self._aggregate_pc_active(target_date)
        if pc is not None:
            fields["pc_active_min"] = pc

        # 回复计数
        reply = self._aggregate_reply_count(target_date)
        if reply is not None:
            fields["reply_count"] = reply

        # 行为状态分布
        dist = self._aggregate_behavior_state(target_date)
        if dist:
            fields["behavior_state_dist"] = dist

        # 屏幕使用时长（从 app_sessions 汇总，替代旧"最后快照"逻辑）
        screen = self._aggregate_screen_time(target_date)
        if screen is not None:
            fields["screen_time_total_min"] = screen

        # GPS 位置变化链（健康快照中的位置分类去重拼接）
        gps = self._aggregate_gps(target_date)
        if gps:
            fields["gps_chain"] = gps

        if fields:
            self._store.upsert_daily_summary(target_date, **fields)
            logger.info(f"DailyAggregator: {target_date} 聚合完成 — {list(fields.keys())}")

        return fields

    # ── 各维度聚合 ──────────────────────────────────────

    def _aggregate_steps(self, target_date: str) -> Optional[Dict[str, Any]]:
        """从 health_snapshots 取当天最后一条步数记录。返回 {steps, outing_steps, home_steps, avg_steps_7d, avg_outing_steps_7d}。"""
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT steps_today FROM health_snapshots "
                "WHERE date(timestamp)=? AND steps_today IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (target_date,)
            ).fetchone()
            if not row or not row["steps_today"]:
                return None

            fields = {"steps": int(row["steps_today"])}

            # 外出步数 / 在家步数（外出=用户状态为 out 期间的步数差值，从 status_change_log 估算）
            try:
                outing_steps = self._estimate_outing_steps(target_date, fields["steps"])
                fields["outing_steps"] = outing_steps
                fields["home_steps"] = max(0, fields["steps"] - outing_steps)
            except Exception:
                fields["outing_steps"] = 0
                fields["home_steps"] = fields["steps"]

            # 近 7 日均值
            try:
                seven_days_ago = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
                yesterday = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                summaries = self._store.get_daily_summaries(seven_days_ago, yesterday)
                if summaries:
                    vals = [s.get("steps", 0) or 0 for s in summaries if s.get("steps")]
                    if vals:
                        fields["avg_steps_7d"] = round(sum(vals) / len(vals), 1)
                    out_vals = [s.get("outing_steps", 0) or 0 for s in summaries if s.get("outing_steps")]
                    if out_vals:
                        fields["avg_outing_steps_7d"] = round(sum(out_vals) / len(out_vals), 1)
            except Exception:
                pass

            return fields
        except Exception as e:
            logger.debug(f"DailyAggregator: 步数聚合失败: {e}")
            return None

    def _estimate_outing_steps(self, target_date: str, total_steps: int) -> int:
        """估算外出期间步数。从 status_change_log 统计当日外出时长比例。"""
        try:
            conn = self._store._get_conn()
            # 检查 status_change_log 表是否存在（哨兵未启动时可能不存在）
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='status_change_log'"
            ).fetchone()
            if not table_check:
                return 0

            rows = conn.execute(
                "SELECT from_status, to_status, timestamp FROM status_change_log "
                "WHERE active_date=? ORDER BY id ASC",
                (target_date,)
            ).fetchall()

            total_out_min = 0
            out_start = None
            for r in rows:
                if r["to_status"] == "out" and out_start is None:
                    out_start = r["timestamp"]
                elif r["from_status"] == "out" and out_start is not None:
                    try:
                        t1 = datetime.fromisoformat(out_start)
                        t2 = datetime.fromisoformat(r["timestamp"])
                        total_out_min += (t2 - t1).total_seconds() / 60
                    except Exception:
                        pass
                    out_start = None

            if total_out_min > 0 and total_out_min < 1440:
                # 外出步数占比 ≈ 外出时间占全天比例（粗略估算）
                awake_min = 16 * 60  # 假设清醒 16h
                ratio = min(total_out_min / awake_min, 1.0)
                return int(total_steps * ratio * 0.7)  # 外出步频通常低于在家
        except Exception:
            pass
        return 0

    def _aggregate_hr(self, target_date: str) -> Optional[Dict[str, Any]]:
        """从 health_snapshots 聚合全天心率统计。"""
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT MIN(heart_rate) as hr_min, MAX(heart_rate) as hr_max, "
                "AVG(heart_rate) as hr_avg, COUNT(*) as cnt "
                "FROM health_snapshots "
                "WHERE date(timestamp)=? AND heart_rate IS NOT NULL AND heart_rate > 0",
                (target_date,)
            ).fetchone()
            if not row or row["cnt"] == 0:
                return None
            return {
                "hr_min": row["hr_min"],
                "hr_max": row["hr_max"],
                "hr_avg": int(row["hr_avg"]) if row["hr_avg"] else None,
            }
        except Exception as e:
            logger.debug(f"DailyAggregator: HR 聚合失败: {e}")
            return None

    def _aggregate_pc_active(self, target_date: str) -> Optional[int]:
        """统计 PC 活跃的快照数（input_idle < 300s）。每个 tick≈60s，count≈分钟数。"""
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM health_snapshots "
                "WHERE date(timestamp)=? AND input_idle_seconds IS NOT NULL AND input_idle_seconds < 300",
                (target_date,)
            ).fetchone()
            return row["cnt"] if row else None
        except Exception as e:
            logger.debug(f"DailyAggregator: PC active 聚合失败: {e}")
            return None

    def _aggregate_reply_count(self, target_date: str) -> Optional[int]:
        """从 conversation_messages 统计当日用户消息数。"""
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()
            if not chronicle:
                return None
            import sqlite3 as _sqlite3
            with _sqlite3.connect(chronicle.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM conversation_messages "
                    "WHERE role='user' AND active_date=?",
                    (target_date,)
                ).fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.debug(f"DailyAggregator: reply_count 聚合失败: {e}")
            return None

    def _aggregate_behavior_state(self, target_date: str) -> Optional[str]:
        """从 health_snapshots 聚合当日行为状态分布，返回 JSON 字符串。"""
        try:
            conn = self._store._get_conn()
            rows = conn.execute(
                "SELECT behavior_state, COUNT(*) as cnt FROM health_snapshots "
                "WHERE date(timestamp)=? AND behavior_state IS NOT NULL AND behavior_state != 'unknown' "
                "GROUP BY behavior_state ORDER BY cnt DESC",
                (target_date,)
            ).fetchall()
            if not rows:
                return None
            dist = {r["behavior_state"]: r["cnt"] for r in rows}
            return _json.dumps(dist, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"DailyAggregator: behavior_state 聚合失败: {e}")
            return None

    def _aggregate_screen_time(self, target_date: str) -> Optional[int]:
        """从 app_sessions 汇总当日手机屏幕使用时长（秒→分钟）。
        替代旧逻辑"取 health_snapshots 最后快照值"——那个值是瞬时快照，不是全天累计。
        """
        try:
            conn = self._store._get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) as total_sec FROM app_sessions "
                "WHERE date(session_start)=? AND platform='phone'",
                (target_date,)
            ).fetchone()
            if row and row["total_sec"]:
                return max(0, int(row["total_sec"] // 60))
        except Exception as e:
            logger.debug(f"DailyAggregator: screen_time 聚合失败: {e}")
        return None


    def _aggregate_gps(self, target_date: str) -> Optional[str]:
        """从 health_snapshots 提取当日位置分类变化链（去重相邻重复 + 过滤中间噪点）。
        如 "家 → 公司 → 海底捞 → 家"。
        """
        try:
            conn = self._store._get_conn()
            rows = conn.execute(
                "SELECT location_address, location_category FROM health_snapshots "
                "WHERE date(timestamp)=? AND location_address IS NOT NULL "
                "ORDER BY timestamp",
                (target_date,)
            ).fetchall()
            if not rows:
                return None

            # 去重相邻重复 + 提取有意义的地标名
            chain = []
            seen = set()
            for r in rows:
                label = r["location_address"] or r["location_category"] or ""
                if not label:
                    continue
                # 用地址前 10 字做去重 key（同一地址不同格式也合并）
                key = label[:10]
                if key not in seen:
                    seen.add(key)
                    if chain and chain[-1] == label:
                        continue
                    chain.append(label)

            if not chain:
                return None

            # 最多保留 8 个节点
            if len(chain) > 8:
                chain = chain[:4] + ["..."] + chain[-3:]
            return " → ".join(chain)
        except Exception as e:
            logger.debug(f"DailyAggregator: GPS 聚合失败: {e}")
            return None
