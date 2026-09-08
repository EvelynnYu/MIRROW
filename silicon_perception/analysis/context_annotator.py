"""上下文注释生成器 — 快照行 + 时间线注解

为 context_scheduler 提供两个输出：
1. 哨兵快照行（每轮上下文始终注入）
2. 哨兵时间线注解（查询今日 sentinel_events）
"""

import logging
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class ContextAnnotator:
    """生成哨兵相关的上下文注入文本"""

    def __init__(self, health_store):
        self._store = health_store

    # ── 快照行（统一数据过期模型：上次值 + 时间 + 连接状态）──

    def build_snapshot_line(self) -> str:
        """生成一行哨兵快照。始终返回。
        所有数据源遵循：上次值 + 上次采集时间 + 当前连接状态。
        """
        parts = []

        # 心率
        hr_stats = self._store.get_hr_stats_recent(minutes=5)
        if hr_stats["count"] > 0:
            parts.append(f"心率{hr_stats['avg']}")
        else:
            parts.append("心率:未连接")

        # 步数（带过期信息 + 基线对比）
        try:
            from silicon_perception.sentinel import get_sentinel
            s = get_sentinel()
            if s and s._step_source:
                ss = s._step_source
                age_min = int(ss.last_change_seconds_ago / 60) if ss.last_change_seconds_ago else 0
                steps = ss.today_steps
                steps_str = f"{steps}" if steps else "?"
                # 基线对比（偏离 >20% 时标注）
                baseline_note = ""
                if steps and isinstance(steps, int) and steps > 0:
                    try:
                        from datetime import datetime
                        today = datetime.now().strftime("%Y-%m-%d")
                        summary = s._store.get_daily_summaries(today, today)
                        if summary:
                            avg7 = summary[0].get("avg_steps_7d")
                            if avg7 and avg7 > 0:
                                pct = int(steps / avg7 * 100)
                                if pct < 80 or pct > 120:
                                    baseline_note = f"(7日均值{int(avg7)}的{pct}%)"
                    except Exception:
                        pass
                if ss.connection_state == "connected":
                    age_str = f"{age_min}min前采集" if age_min >= 5 else "刚刚"
                    parts.append(f"步数:{steps_str}{baseline_note}({age_str})")
                else:
                    parts.append(f"步数:{steps_str}{baseline_note}({age_min}min前采集，当前暂未连接)")
            else:
                parts.append("步数:未连接")
        except Exception:
            parts.append("步数:—")

        # GPS（带过期信息）
        try:
            s2 = get_sentinel()
            if s2 and s2._gps_source:
                gs = s2._gps_source
                age_min = int(gs.last_change_seconds_ago / 60) if gs.last_change_seconds_ago else 0
                if gs.connection_state == "connected" and gs.last_location:
                    age_str = f"{age_min}min前" if age_min >= 10 else "刚刚"
                    parts.append(f"GPS:已定位({age_str})")
                elif gs.last_location:
                    parts.append(f"GPS:已定位({age_min}min前采集，当前暂未连接)")
                else:
                    parts.append("GPS:未连接")
            else:
                parts.append("GPS:未连接")
        except Exception:
            parts.append("GPS:—")

        # 行为状态（融合 PC进程 + 手机App + 键鼠）
        try:
            s3 = get_sentinel()
            if s3 and s3._last_snapshot:
                bhv = getattr(s3._last_snapshot, 'behavior_state', None)
                if bhv:
                    parts.append(f"状态:{bhv}")
                idle = getattr(s3._last_snapshot, 'input_idle_seconds', None)
                app_sess = getattr(s3._last_snapshot, 'active_app_session', None)
                # PC 进程仅键鼠活跃（<2min）时注入——空闲时前台窗口无意义
                if app_sess and (idle is None or idle < 120):
                    parts.append(f"会话:{app_sess}")
        except Exception:
            pass

        # 生理期
        try:
            from calendar_manager.database import get_period_info_today
            info = get_period_info_today()
            if info and info.get("is_period"):
                day = info.get("period_day", "?")
                parts.append(f"生理期第{day}天")
            else:
                parts.append("非生理期")
        except Exception:
            pass

        return f"[哨兵] {' | '.join(parts)}"

    # ── 时间线注解 ────────────────────────────────────

    def build_timeline_annotations(self) -> List[Dict[str, str]]:
        """查询今日 sentinel_events，返回时间线注解列表。
        已推送的（🛡️ 聊天消息）不重复注入。
        同一天同一类型只注入一次。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        conn = self._store._get_conn()
        rows = conn.execute(
            """SELECT * FROM sentinel_events
               WHERE date(timestamp) = ? AND pushed = 0 AND injected_to_context = 0 AND event_type != 'L1'
               ORDER BY timestamp ASC""",
            (today,),
        ).fetchall()
        rows = [dict(r) for r in rows]

        # 同日同类型去重（只取第一条），收集注入 ID 用于标记
        seen_types = set()
        deduped = []
        injected_ids = []
        for r in rows:
            etype = r.get("event_type", "instant")
            if etype in seen_types:
                continue
            seen_types.add(etype)
            deduped.append(r)
            injected_ids.append(r["id"])

        annotations = []
        for ev in deduped:
            ts = ev["timestamp"]
            try:
                dt = datetime.fromisoformat(ts)
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = ts

            content = f"{time_str} [哨兵] {ev['message'] or ev['rule_id']}"
            annotations.append({"time": time_str, "content": content})

        # 标记已注入：后续构建不再重复注入同一事件
        if injected_ids:
            conn.execute(
                f"UPDATE sentinel_events SET injected_to_context = 1 WHERE id IN ({','.join('?'*len(injected_ids))})",
                injected_ids
            )
            conn.commit()

        return annotations
