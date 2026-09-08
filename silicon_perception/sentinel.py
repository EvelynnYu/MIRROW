"""硅基哨兵主协调器

启动/停止监控循环，协调数据源采集 → 存储 → 异常检测 → 推送。
作为独立模块运行，不受 Wander 的 idle 限制。
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any, List

from .collection import (
    DataSource, DataPoint,
    HeartRateSource, ScreenActivitySource,
    MirrowVisibilitySource, UserStatusSource, PeriodSource,
    InputIdleSource, StepCountSource, GpsSource, ScreenTimeSource,
    CurrentAppSource, WeatherSource,
)
from .analysis.anomaly_detector import AnomalyDetector, Priority, DataSnapshot
from .recording.health_store import HealthStore, get_store
from .recording.schema import migrate as migrate_schema
from mirrow_core.truncation_config import get_truncation_limit

logger = logging.getLogger(__name__)

# 全局单例
_sentinel: Optional["SiliconSentinel"] = None
SENTINEL_AVAILABLE = True


def init_sentinel(
    on_push: Optional[Callable[[Dict[str, Any]], Any]] = None,
    tick_interval: int = 60,
    enable_llm_polish: bool = False,
    call_llm_func: Optional[Callable] = None,
    call_pro_llm: Optional[Callable] = None,
) -> "SiliconSentinel":
    global _sentinel
    _sentinel = SiliconSentinel(
        on_push=on_push,
        tick_interval=tick_interval,
        enable_llm_polish=enable_llm_polish,
        call_llm_func=call_llm_func,
        call_pro_llm=call_pro_llm,
    )
    return _sentinel


def get_sentinel() -> Optional["SiliconSentinel"]:
    return _sentinel


class SiliconSentinel:
    """硅基数据控制模块 — AI 的哨兵"""

    def __init__(
        self,
        on_push: Optional[Callable[[Dict[str, Any]], Any]] = None,
        tick_interval: int = 60,
        enable_llm_polish: bool = False,
        call_llm_func: Optional[Callable] = None,
        call_pro_llm: Optional[Callable] = None,
    ):
        self._on_push = on_push  # 推送回调: async (payload) -> None
        self._tick_interval = tick_interval
        self._call_pro_llm = call_pro_llm  # Pro LLM（[PUSH] 时用于生成自然消息）
        self._enable_llm_polish = enable_llm_polish
        self._call_llm = call_llm_func

        self._store = get_store()
        self._detector = AnomalyDetector(health_store=self._store)
        self._sleep_learner = None     # SleepWindowLearner(self._store)
        self._annotator = None         # Phase 3: ContextAnnotator(self._store)

        self._last_flash_context: Optional[dict] = None  # 最近一次 Flash [PUSH] 决策上下文
        self._sources: List[DataSource] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._mobile_hr_task: Optional[asyncio.Task] = None
        self._tick_count = 0
        self._tick_lock = asyncio.Lock()  # 防并发：_monitor_loop 与 /refresh 互斥
        self._deferred_push: Dict[str, dict] = {}  # trigger_key → {clean_text, triggers, created_at}
        self._current_pc_exe: Optional[str] = None     # 当前 PC session app
        self._current_phone_pkg: Optional[str] = None   # 当前手机 session app
        self._pc_session_start: Optional[str] = None
        self._phone_session_start: Optional[str] = None
        # 从 settings.json 恢复开关状态（重启不丢失）
        try:
            from mirrow_core.settings_manager import get_setting
            sentinel_cfg = get_setting("silicon_perception") or {}
        except Exception:
            sentinel_cfg = {}
        self._push_defer_seconds: int = sentinel_cfg.get("push_defer_seconds", 120)  # 延迟推送阈值
        self._enabled = sentinel_cfg.get("enabled", True)
        self._gps_enabled = sentinel_cfg.get("gps_enabled", True)
        self._mobile_hr_error = None  # 最近一次手机 BLE 错误消息
        self._tick_interval = tick_interval  # 基础间隔
        self._hr_source: Optional[HeartRateSource] = None  # 在 _init_sources 中设置

        self._init_sources()

    # ── 数据源初始化 ──────────────────────────────────

    def _init_sources(self):
        hr_src = HeartRateSource()
        self._hr_source = hr_src
        self._step_source = StepCountSource()
        self._gps_source = GpsSource()
        self._current_app_source = CurrentAppSource()
        self._last_snapshot: Optional[DataSnapshot] = None  # 缓存最新快照，供 API 读取
        self._sources = [
            hr_src,
            ScreenActivitySource(),
            InputIdleSource(),
            MirrowVisibilitySource(),
            UserStatusSource(),
            PeriodSource(),
            self._step_source,
            self._gps_source,
            ScreenTimeSource(),
            self._current_app_source,
            WeatherSource(),
        ]
        logger.info(f"Sentinel: {len(self._sources)} 个数据源已注册")

    # ── 启停 ──────────────────────────────────────────

    async def start(self):
        """启动监控循环"""
        if self._running:
            return
        self._running = True

        # 启动所有数据源
        for src in self._sources:
            try:
                await src.start()
            except Exception as e:
                logger.warning(f"Sentinel: 数据源 {src.name} 启动失败: {e}")

        self._task = asyncio.create_task(self._monitor_loop())
        self._mobile_hr_task = asyncio.create_task(self._mobile_hr_loop())

        # 执行 schema 迁移（安全幂等）
        try:
            applied = migrate_schema(self._store._get_conn())
            if applied:
                logger.info(f"Sentinel: schema 迁移完成: {applied}")
        except Exception as e:
            logger.warning(f"Sentinel: schema 迁移失败: {e}")

        # 延迟初始化分析/告警模块（避免循环导入）
        try:
            from .analysis.sleep_window import SleepWindowLearner
            from .analysis.context_annotator import ContextAnnotator
            self._sleep_learner = SleepWindowLearner(self._store)
            self._annotator = ContextAnnotator(self._store)
            from .analysis.sentinel_summarizer import SentinelSummarizer
            from .analysis.trigger_detector import TriggerDetector
            self._summarizer = SentinelSummarizer(flash_llm_func=self._call_llm)
            self._trigger_detector = TriggerDetector()
        except Exception as e:
            logger.warning(f"Sentinel: 分析模块初始化失败: {e}")

        logger.info(f"Sentinel: 监控循环已启动 (tick={self._tick_interval}s, mobile_hr=300s)")

    async def stop(self):
        """停止监控循环"""
        self._running = False
        if self._task:
            self._task.cancel()
        if self._mobile_hr_task:
            self._mobile_hr_task.cancel()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for src in self._sources:
            try:
                await src.stop()
            except Exception:
                pass

        # store.close() 在 shutdown 时由 main.py 统一调用，不在这里关闭
        # 避免测试/重启场景下连接被过早关闭
        logger.info("Sentinel: 已停止")

    # ── 手机心率后台轮询 ──────────────────────────────

    # 手机 BLE 并发锁（_mobile_hr_loop 与 band_tool 回退互斥）
    _mobile_hr_lock = asyncio.Lock()

    async def _mobile_hr_loop(self):
        """PC 连不上手环时，由手机辅助采集心率。判断顺序：
        ① PC 5min 无数据 → 手机接管（不再要求外出/开关，自动兜底）
        ② relay BLE 直连
        """
        MOBILE_HR_INTERVAL = 300  # 5 分钟
        await asyncio.sleep(10)  # 启动后等 10s 让 WS 就绪

        while self._running:
            try:
                # ① PC BLE 最近有数据 → 跳过（PC 永远优先）
                pc_stats = self._store.get_hr_stats_recent(minutes=5)
                if pc_stats["count"] > 0:
                    await asyncio.sleep(30)
                    continue

                # ② 手机 BLE 直连
                try:
                    from mirrow_core.shared_state import get_mobile_relay_callback as _get_relay
                    _relay = _get_relay()
                except ImportError:
                    _relay = None
                if not _relay:
                    await asyncio.sleep(30)
                    continue

                async with self._mobile_hr_lock:  # 与 band_tool 回退互斥
                    import uuid as _uid
                    from silicon_perception.collection.heart_rate import resolve_hr_device_mac as _resolve_mac
                    hr = None
                    try:
                        ble_result = await asyncio.wait_for(
                            _relay(str(_uid.uuid4()), "mobile_heart_rate", {"mac": _resolve_mac() or ""}),
                            timeout=15.0
                        )
                        if isinstance(ble_result, dict) and ble_result.get("success") and ble_result.get("data"):
                            hr = ble_result["data"].get("heart_rate")
                            self._mobile_hr_error = None
                        elif isinstance(ble_result, dict) and not ble_result.get("success"):
                            err = ble_result.get("content", str(ble_result))
                            self._mobile_hr_error = err
                            logger.debug(f"[MobileHR] 采集失败: {err[:120]}")
                    except asyncio.TimeoutError:
                        self._mobile_hr_error = "手机 BLE 超时(15s)"
                    except Exception as e:
                        self._mobile_hr_error = f"手机 BLE 异常: {str(e)[:100]}"

                    if hr is not None:
                        try:
                            self._store.insert_snapshot(heart_rate=hr)
                            logger.info(f"[MobileHR] 外出自动采集: {hr} bpm")
                            # 同步 field_ts，让 is_stale("heart_rate") 感知这次采集时刻
                            # （否则 mobile 兜底采的心率因绕过 _build_snapshot 而无时效戳）
                            if self._last_snapshot is not None:
                                self._last_snapshot.heart_rate = hr
                                self._last_snapshot.field_ts["heart_rate"] = datetime.now().isoformat()
                        except Exception as e:
                            logger.warning(f"[MobileHR] 写入失败: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[MobileHR] 循环异常: {e}")

            await asyncio.sleep(MOBILE_HR_INTERVAL)

    # ── 动态轮询间隔 ──────────────────────────────────

    def _get_tick_interval(self) -> int:
        """根据当前场景返回应使用的轮询间隔"""
        # 深夜模式 + HR 启用 → 高频采集
        night_active = self._is_night_mode_active()
        if night_active:
            return 15
        return self._tick_interval  # 默认 60s

    @staticmethod
    def _is_night_mode_active() -> bool:
        try:
            from mirrow_core.shared_state import get_night_mode_active
            return get_night_mode_active()
        except Exception:
            return False

    @staticmethod
    def _get_last_message_seconds() -> float:
        """距离用户最后一条消息的秒数（查 SQL，私聊+群聊取最新）。"""
        try:
            from silicon_perception.analysis.behavior_profile import get_behavior_profile
            return get_behavior_profile().get_last_user_message_seconds()
        except Exception:
            return 999999.0

    async def _adaptive_sleep(self, base_interval: int):
        """分片 sleep，检测到间隔变化时提前退出"""
        chunk = 5
        elapsed = 0
        while elapsed < base_interval and self._running:
            await asyncio.sleep(min(chunk, base_interval - elapsed))
            elapsed += chunk
            new_interval = self._get_tick_interval()
            if new_interval != base_interval:
                return

    def _trust_out_status(self) -> bool:
        """是否信任当前 'out' 状态（决定是否跳过 HR 采集）。
        不信任的情况（照常采 HR，防 stale 'out' 关掉心率管道）：
        - 状态已声明 >8h（可能忘记改回）
        - PC 有在场信号矛盾（键鼠活跃 <5min 或 MIRROW 前台可见）
        """
        OUT_TRUST_HOURS = 8
        try:
            from mirrow_core.shared_state import get_current_user_status_ts
            ts = get_current_user_status_ts()
            if ts:
                age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
                if age_h > OUT_TRUST_HOURS:
                    return False  # 太久没更新，不信任
            snap = self._last_snapshot
            if snap:
                if snap.input_idle_seconds is not None and snap.input_idle_seconds < 300:
                    return False  # 键鼠 5min 内活跃 → 人在 PC 前，不是真外出
                if snap.mirrow_visible:
                    return False  # MIRROW 前台可见 → 人在
        except Exception:
            pass
        return True  # 默认信任（近期声明 + 无在场矛盾）

    async def _monitor_loop(self):
        """主监控循环 — 每 tick 采集一次全部数据源，自适应间隔"""
        while self._running:
            interval = self._get_tick_interval()
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sentinel tick 异常: {e}")

            await self._adaptive_sleep(interval)

    async def _tick(self, force: bool = False):
        """一次监控周期。force=True 跳过所有冷却和门控。"""
        async with self._tick_lock:
            return await self._tick_inner(force)

    async def _tick_inner(self, force: bool = False):
        """_tick 的实际逻辑（由 _tick_lock 保护）"""
        if not self._enabled and not force:
            return

        # 深夜模式：哨兵整体暂停（force 可跳过）
        if self._is_night_mode_active() and not force:
            return

        self._tick_count += 1
        # 检查延迟推送队列
        await self._check_deferred_push()
        tick_start = time.monotonic()

        # 1. 采集数据源（按各自的 tick_interval，HR 有额外门控）
        datapoints: Dict[str, DataPoint] = {}
        for src in self._sources:
            # tick_interval: 非每 tick 都采集的源，按周期跳过（force 强制全部采集）
            ti = getattr(src, 'tick_interval', 1)
            if not force and self._tick_count % max(1, ti) != 0:
                continue

            # HR 门控：真外出才跳过（手机 relay 兜底）。
            # 防 stale "out"：状态>8h 或被 PC 在场信号矛盾时，不信任 "out"，照常采 HR。
            if src.name == "heart_rate":
                try:
                    from wander_manager.user_status import get_user_status
                    status = get_user_status()
                    if status.value == "out" and self._trust_out_status():
                        continue
                except Exception:
                    pass
            # GPS 门控：手动关闭 → 跳过
            if src.name == "gps_location" and not self._gps_enabled:
                continue
            try:
                datapoints[src.name] = await src.read()
            except Exception as e:
                logger.debug(f"Sentinel: 数据源 {src.name} 异常: {e}")

        # 2. 聚合为 DataSnapshot，缓存供 API 读取
        snapshot = self._build_snapshot(datapoints)
        self._last_snapshot = snapshot

        # 3. 写入存储
        behavior_state_val = _infer_behavior_state(snapshot)
        snapshot.behavior_state = behavior_state_val
        # L1.5: 当前 App 会话（活跃App + 持续时长）
        parts = []
        if self._current_pc_exe and self._pc_session_start:
            dur = int((datetime.now() - datetime.fromisoformat(self._pc_session_start)).total_seconds() // 60)
            parts.append(f"{self._current_pc_exe}({dur}min)")
        if self._current_phone_pkg and self._phone_session_start:
            dur = int((datetime.now() - datetime.fromisoformat(self._phone_session_start)).total_seconds() // 60)
            name = getattr(snapshot, 'mobile_app_name', self._current_phone_pkg) or self._current_phone_pkg
            parts.append(f"{name}({dur}min)")
        snapshot.active_app_session = " + ".join(parts) if parts else None
        snap_id = self._store.insert_snapshot(
            heart_rate=snapshot.heart_rate,
            screen_active=snapshot.screen_active,
            foreground_app=snapshot.foreground_app,
            mirrow_visible=snapshot.mirrow_visible,
            user_status=snapshot.user_status,
            is_period=snapshot.is_period,
            period_day=snapshot.period_day,
            input_idle_seconds=snapshot.input_idle_seconds,
            cumulative_steps=snapshot.cumulative_steps,
            steps_today=snapshot.steps_today,
            location_lat=snapshot.location_lat,
            location_lng=snapshot.location_lng,
            location_address=snapshot.location_address,
            location_category=snapshot.location_category,
            behavior_state=behavior_state_val,
            active_app_session=snapshot.active_app_session,
            screen_time_minutes=snapshot.screen_time_minutes,
            top_app_category=snapshot.top_app_category,
        )

        # 3.5 行为基准检查：对比基线，生成偏离信号
        baseline_result = None
        try:
            from silicon_perception.analysis.behavior_profile import get_behavior_profile
            bp = get_behavior_profile()
            if bp:
                baseline_result = bp.check(snapshot, snapshot.last_message_seconds)
        except Exception as e:
            logger.debug(f"Sentinel: 行为基准检查跳过: {e}")

        # 3.6 状态引擎：仲裁 presence/activity → 100% 铁证才自动回写 user_status
        try:
            self._run_state_engine(snapshot)
        except Exception as e:
            logger.debug(f"Sentinel: 状态引擎跳过: {e}")

        # 4. 异常检测（注入基线变量）
        if baseline_result and baseline_result.deviations:
            try:
                # 提取心率/步数基线变量注入 anomaly_detector eval 命名空间
                hr_data = bp._cache.get("heart_rate", {})
                steps_data = bp._cache.get("steps", {})
                baseline_vars = {
                    "resting_hr": hr_data.get("resting_mean", 65),
                    "hr_baseline_mean": hr_data.get("all", {}).get("mean", 0),
                    "hr_baseline_stddev": hr_data.get("all", {}).get("stddev", 10),
                    "steps_baseline_mean": steps_data.get("overall_mean", 0),
                    "is_cold_start": baseline_result.cold_start,
                }
                self._detector.set_baseline_context(baseline_vars)
            except Exception:
                pass

        alerts = await self._detector.evaluate(snapshot)

        # 5. 处理告警（C1/C2 救命规则直接推送，绕过 Flash）
        for alert in alerts:
            await self._handle_alert(alert, snap_id)

        # 7. 作息窗口学习（每周一次）
        if self._sleep_learner and self._tick_count % 10080 == 0:  # ~每周
            try:
                if self._sleep_learner.should_update():
                    self._sleep_learner.learn()
            except Exception as e:
                logger.debug(f"Sentinel: 作息学习异常: {e}")

        # 7. App 会话跟踪（PC + 手机）
        self._track_app_sessions(snapshot)

        # 8. Flash 触发器（L1 快照对比 + L3 保底）
        if self._summarizer and self._trigger_detector:
            hr_stats = self._store.get_hr_stats_recent(minutes=5) if self._store else {}
            triggers = self._trigger_detector.evaluate(snapshot, hr_stats)
            if triggers:
                try:
                    today = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
                    events_today = [dict(r) for r in self._store._get_conn().execute(
                        "SELECT * FROM sentinel_events WHERE date(timestamp) = ? AND pushed = 1 ORDER BY timestamp DESC LIMIT 10",
                        (today,)
                    ).fetchall()]
                    # 过滤已解除的阈值类事件：当前传感器值已恢复正常 → 不给 Flash 看
                    trends = self._store.get_weekly_comparison() if self._store else {}
                    # 构建传感器时效信息
                    source_freshness = _build_source_freshness(datapoints, snapshot)
                    # 构建上下文信息（时间 + 状态时间线 + 作息 + 前台应用）
                    current_time, status_timeline, schedule_text = _build_tick_context(snapshot)
                    foreground_app = snapshot.foreground_app or ""
                    baseline_text = baseline_result.text if baseline_result else ""
                    await self._summarizer.update(snapshot, events_today, trends, snapshot.last_message_seconds,
                                                   triggers=triggers, source_freshness=source_freshness,
                                                   current_time=current_time, status_timeline=status_timeline,
                                                   schedule_text=schedule_text, foreground_app=foreground_app,
                                                   baseline_context=baseline_text)
                    # [PUSH] 标记：Flash 判断该主动说话 → 活跃门控 → Pro 生成 → 推送
                    summary = self._summarizer.get_summary()
                    # 记录 Flash 决策。推送成功由 flash_push/pattern 记录——此处仅记录未推送的（被门控/冷却/Flash判否）。
                    # pattern 优先，不重复写 info。（设计约定：sentinel_events 同一次评估不应有 info+pattern 两条）
                    has_push = "[PUSH]" in summary
                    actually_pushed = False
                    if has_push and self._on_push and self._call_pro_llm:
                        clean = summary.replace("[PUSH]", "").strip()
                        if self._should_suppress_push(snapshot) and clean:
                            # 正在对话 → 延迟推送。2min 后若仍未回复 → 补推；若回复了 → 入上下文
                            key = "|".join(triggers)[:80] if triggers else "flash"
                            self._deferred_push[key] = {"clean": clean, "triggers": triggers, "at": time.monotonic()}
                            logger.debug(f"Sentinel: 延迟推送 ({key})")
                        elif clean:
                            skip_due_to_cooldown = False
                            last_push = self._store.get_last_push_time("flash_push")
                            if last_push:
                                try:
                                    import datetime as _dt
                                    last_dt = _dt.datetime.fromisoformat(last_push)
                                    if (_dt.datetime.now() - last_dt).total_seconds() < 1800:
                                        logger.debug("Sentinel: flash_push 频率约束跳过 (上次: %s)", last_push[:19])
                                        skip_due_to_cooldown = True
                                except Exception:
                                    pass
                            if not skip_due_to_cooldown:
                                clean = summary.replace("[PUSH]", "").strip()
                                if clean:
                                    try:
                                        pro_result = await self._call_pro_llm(clean)
                                        content = pro_result.get("content", "") if isinstance(pro_result, dict) else str(pro_result)
                                        reasoning = pro_result.get("reasoning") if isinstance(pro_result, dict) else None
                                        if content:
                                            # 保存 Flash 决策上下文（供监控台调试）
                                            self._last_flash_context = {
                                                "timestamp": __import__('datetime').datetime.now().isoformat(),
                                                "summary": clean,
                                                "triggers": triggers,
                                                "baseline_context": baseline_text if baseline_result else "",
                                                "sensor_snapshot": {
                                                    "hr": snapshot.heart_rate,
                                                    "steps": snapshot.steps_today,
                                                    "idle": f"{snapshot.input_idle_seconds:.0f}s" if snapshot.input_idle_seconds else "N/A",
                                                    "screen": snapshot.screen_active,
                                                    "location": snapshot.location_category or "N/A",
                                                },
                                                "trends": trends,
                                                "pushed": True,
                                                "push_content": content,
                                            }
                                            # 持久化
                                            self._store.insert_event(
                                                rule_id="flash_push", priority="pattern",
                                                message=clean, pushed=True,
                                                event_type="pattern",
                                            )
                                            # 推送（含思考过程）
                                            await self._on_push({
                                                "message": content, "rule_id": "flash_push",
                                                "priority": "medium",
                                                "timestamp": __import__('datetime').datetime.now().isoformat(),
                                                "reasoning": reasoning,
                                            })
                                            actually_pushed = True
                                            logger.info(f"Perception PUSH: {content[:80]}")
                                    except Exception as e:
                                        logger.warning(f"Perception Pro call failed: {e}")
                    # 记录 Flash 决策（仅未推送时——推送成功由 flash_push/pattern 记录）
                    if not actually_pushed:
                        self._store.insert_event(
                            rule_id="flash_judge", priority="info",
                            message=f"triggers={triggers} push={has_push} | {summary[:200]}",
                            pushed=False, event_type="flash_judge",
                        )
                except Exception as e:
                    logger.warning(f"Flash pipeline failed: {e}")

        # 9. 设备行为链（每 30 tick ≈ 30min 刷新）
        if self._tick_count % 30 == 0 and self._call_llm:
            asyncio.create_task(self.build_behavior_chain())

        # 10. 定期清理（每 100 tick ≈ 100 分钟）
        if self._tick_count % 100 == 0:
            try:
                self._store.cleanup_old_data()
            except Exception:
                pass

        tick_ms = (time.monotonic() - tick_start) * 1000
        if self._tick_count <= 3 or self._tick_count % 30 == 0:
            hr_str = str(snapshot.heart_rate) if snapshot.heart_rate else "N/A"
            iv = self._get_tick_interval()
            logger.info(f"Sentinel tick #{self._tick_count}: HR={hr_str}, alerts={len(alerts)}, interval={iv}s, {tick_ms:.0f}ms")

    # ── App 会话 + 设备行为链 ──────────────────────────

    async def build_behavior_chain(self, target_date: str = None) -> Optional[str]:
        """L1.5: Flash 压缩今日 app_sessions → 设备行为链，存入 daily_health_summary。"""
        if not self._call_llm:
            return None
        import datetime as _dt
        today = target_date or _dt.datetime.now().strftime("%Y-%m-%d")
        try:
            conn = self._store._get_conn()
            rows = conn.execute(
                "SELECT app_name, platform, session_start, session_end, duration_seconds "
                "FROM app_sessions WHERE date(session_start)=? ORDER BY session_start",
                (today,)
            ).fetchall()
            if not rows:
                return None

            # 构建时间线
            timeline = []
            for r in rows:
                start = r["session_start"][11:16] if len(r["session_start"]) >= 16 else ""
                end = r["session_end"][11:16] if r["session_end"] and len(r["session_end"]) >= 16 else ""
                dur = (r["duration_seconds"] or 0) // 60
                plat = "PC" if r["platform"] == "pc" else "📱"
                timeline.append(f"{start}-{end} {plat}{r['app_name']}({dur}min)")

            raw = " → ".join(timeline)
            if len(raw) < 200:
                chain = raw
            else:
                # Flash 压缩
                prompt = f"""将以下设备使用时间线压缩为一段自然语言描述（40-80字）。
用"用户"开头，第三人称。捕捉时间段+App+节奏模式，不要罗列每一项。

时间线：{raw}

输出一段话，不要前缀。"""
                try:
                    result = await asyncio.wait_for(
                        self._call_llm([{"role": "user", "content": prompt}]),
                        timeout=20.0,
                    )
                    chain = result.strip() if isinstance(result, str) else result.get("content", raw[:200])
                except Exception:
                    chain = raw[:200]

            # 存入 daily_health_summary
            self._store.upsert_daily_summary(today, {"behavior_chain": chain})
            logger.info(f"Sentinel: 设备行为链已生成 ({len(chain)}字)")
            return chain
        except Exception as e:
            logger.debug(f"Sentinel: 行为链生成失败: {e}")
            return None

    # ── 快照构建 ──────────────────────────────────────

    def _track_app_sessions(self, snapshot):
        """跟踪 PC + 手机 App 使用会话。L0 门控：PC 空闲>2min / 手机熄屏 → 闭合不记录。

        主从关系：
        - app_sessions 表 → 主存储（结构化行，供 DailyAggregator + behavior_chain 查询）
        - health_snapshots.active_app_session → 从缓存（实时快照行文本，供 summarizer + context_annotator）
        两处各有独立消费者，不要删除任一处。
        """
        now = datetime.now().isoformat()
        try:
            pc_active = snapshot.input_idle_seconds is not None and snapshot.input_idle_seconds < 120
            phone_active = getattr(snapshot, 'mobile_screen_on', None) is True

            # PC 会话
            pc_exe = getattr(snapshot, 'foreground_exe', None) if pc_active else None
            if pc_exe and pc_exe != self._current_pc_exe:
                if self._current_pc_exe and self._pc_session_start:
                    self._store.insert_app_session(
                        self._pc_session_start, now, self._current_pc_exe, 'pc', None)
                self._current_pc_exe = pc_exe
                self._pc_session_start = now
            elif not pc_exe and self._current_pc_exe and self._pc_session_start:
                # PC 空闲→闭合当前会话
                self._store.insert_app_session(
                    self._pc_session_start, now, self._current_pc_exe, 'pc', None)
                self._current_pc_exe = None
                self._pc_session_start = None

            # 手机会话
            phone_pkg = getattr(snapshot, 'mobile_app_package', None) if phone_active else None
            if phone_pkg and phone_pkg != self._current_phone_pkg:
                if self._current_phone_pkg and self._phone_session_start:
                    self._store.insert_app_session(
                        self._phone_session_start, now, self._current_phone_pkg, 'phone', None)
                self._current_phone_pkg = phone_pkg
                self._phone_session_start = now
            elif not phone_pkg and self._current_phone_pkg and self._phone_session_start:
                # 手机熄屏→闭合当前会话
                self._store.insert_app_session(
                    self._phone_session_start, now, self._current_phone_pkg, 'phone', None)
                self._current_phone_pkg = None
                self._phone_session_start = None
        except Exception as e:
            logger.debug(f"Sentinel: 会话跟踪异常: {e}")

    def _run_state_engine(self, snapshot: DataSnapshot):
        """状态引擎：消费快照 → 仲裁 → 自动回写 user_status（100% 铁律）。

        失败静默（不影响 tick）。首次导入失败后禁用，避免每 tick 刷错误。
        """
        if getattr(self, "_state_engine_disabled", False):
            return
        # 设置开关（kill switch）：默认开启；置 False 可暂停自动状态切换
        try:
            from mirrow_core import settings_manager
            if settings_manager.get_setting("state_engine_enabled") is False:
                return
        except Exception:
            pass
        try:
            from silicon_perception.state.engine import get_state_engine
            from silicon_perception.state import bridge
        except Exception as e:
            logger.warning(f"状态引擎不可用，已禁用: {e}")
            self._state_engine_disabled = True
            return
        trans = get_state_engine().tick(snapshot)
        if trans is not None:
            bridge.apply_transition(trans)

    def _build_snapshot(self, datapoints: Dict[str, DataPoint]) -> DataSnapshot:
        """将各数据源的数据聚合为统一快照，同时计算派生字段"""
        s = DataSnapshot(timestamp=datetime.now().isoformat())

        # Heart rate
        hr_dp = datapoints.get("heart_rate")
        if hr_dp and hr_dp.data:
            s.heart_rate = hr_dp.data.get("heart_rate")
            if hr_dp.captured_at:
                s.field_ts["heart_rate"] = hr_dp.captured_at
            elif self._last_snapshot and self._last_snapshot.field_ts.get("heart_rate"):
                s.field_ts["heart_rate"] = self._last_snapshot.field_ts["heart_rate"]
        # HR 重启恢复：首次 tick 从源对象拿恢复的时间戳（防启动就显示"过期"）
        if not s.field_ts.get("heart_rate") and self._hr_source and self._hr_source._last_success_time:
            s.field_ts["heart_rate"] = self._hr_source._last_success_time

        # Screen activity
        scr_dp = datapoints.get("screen_activity")
        if scr_dp and scr_dp.data:
            s.screen_active = scr_dp.data.get("screen_active")
            s.foreground_app = scr_dp.data.get("foreground_app")
            s.foreground_exe = scr_dp.data.get("foreground_exe")

        # Mirrow visibility
        mir_dp = datapoints.get("mirrow_visibility")
        if mir_dp and mir_dp.data:
            s.mirrow_visible = mir_dp.data.get("mirrow_visible")

        # 最后用户消息时间（从 main.py 全局变量读取，而非页面可见性心跳）
        s.last_message_seconds = self._get_last_message_seconds()

        # User status
        st_dp = datapoints.get("user_status")
        if st_dp and st_dp.data:
            s.user_status = st_dp.data.get("user_status")

        # Period
        per_dp = datapoints.get("period")
        if per_dp and per_dp.data:
            s.is_period = per_dp.data.get("is_period", False)
            s.period_day = per_dp.data.get("period_day")

        # Input idle
        idle_dp = datapoints.get("input_idle")
        if idle_dp and idle_dp.data:
            s.input_idle_seconds = idle_dp.data.get("input_idle_seconds")

        # Steps（5min 轮询 → 保留上次已知值）
        step_dp = datapoints.get("step_count")
        if step_dp and step_dp.data:
            s.cumulative_steps = step_dp.data.get("cumulative_steps")
            # 检查 available 标志，断连时不使用过期值
            if step_dp.data.get("available", True):
                s.steps_today = step_dp.data.get("steps_today")
                if step_dp.captured_at:
                    s.field_ts["steps"] = step_dp.captured_at
            # else: s.steps_today 保持 None，anomaly_detector 跳过 B2
            # 步数停滞分钟数（由 trigger_detector L1 检测）
            if self._step_source.last_change_seconds_ago is not None:
                s.steps_stagnant_minutes = int(self._step_source.last_change_seconds_ago / 60)
        elif self._last_snapshot:
            s.cumulative_steps = self._last_snapshot.cumulative_steps
            s.steps_today = self._last_snapshot.steps_today
            s.steps_stagnant_minutes = self._last_snapshot.steps_stagnant_minutes
            if self._last_snapshot.field_ts.get("steps"):
                s.field_ts["steps"] = self._last_snapshot.field_ts["steps"]

        # GPS（10min 轮询 → 保留上次已知值）
        gps_dp = datapoints.get("gps_location")
        if gps_dp and gps_dp.data:
            s.location_lat = gps_dp.data.get("location_lat")
            s.location_lng = gps_dp.data.get("location_lng")
            s.location_address = gps_dp.data.get("location_address")
            s.location_category = gps_dp.data.get("location_category")
            if gps_dp.captured_at:
                s.field_ts["gps"] = gps_dp.captured_at
            elif self._last_snapshot and self._last_snapshot.field_ts.get("gps"):
                # Case A 轮询失败：保留上次值 + 上次时间戳（age 继续增长）
                s.location_lat = self._last_snapshot.location_lat
                s.location_lng = self._last_snapshot.location_lng
                s.location_address = self._last_snapshot.location_address
                s.location_category = self._last_snapshot.location_category
                s.field_ts["gps"] = self._last_snapshot.field_ts["gps"]
        elif self._last_snapshot:
            s.location_lat = self._last_snapshot.location_lat
            s.location_lng = self._last_snapshot.location_lng
            s.location_address = self._last_snapshot.location_address
            s.location_category = self._last_snapshot.location_category
            if self._last_snapshot.field_ts.get("gps"):
                s.field_ts["gps"] = self._last_snapshot.field_ts["gps"]
        # GPS 时间戳：每 tick 从源对象同步最新采集时间（位置不变也刷新）
        if self._gps_source._last_success_time:
            s.field_ts["gps"] = self._gps_source._last_success_time

        # 屏幕使用时长（30min 轮询 → 保留上次已知值）
        st_dp = datapoints.get("screen_time")
        if st_dp and st_dp.data:
            if st_dp.data.get("available", True):
                s.screen_time_minutes = st_dp.data.get("screen_time_minutes")
                s.top_app_category = st_dp.data.get("top_app_category")
                if st_dp.captured_at:
                    s.field_ts["screen_time"] = st_dp.captured_at
            elif self._last_snapshot:
                s.screen_time_minutes = self._last_snapshot.screen_time_minutes
                s.top_app_category = self._last_snapshot.top_app_category
                if self._last_snapshot.field_ts.get("screen_time"):
                    s.field_ts["screen_time"] = self._last_snapshot.field_ts["screen_time"]
        elif self._last_snapshot:
            s.screen_time_minutes = self._last_snapshot.screen_time_minutes
            s.top_app_category = self._last_snapshot.top_app_category
            if self._last_snapshot.field_ts.get("screen_time"):
                s.field_ts["screen_time"] = self._last_snapshot.field_ts["screen_time"]

        # 当前前台App（2min 轮询 → 实时包名 + 屏幕状态）
        ca_dp = datapoints.get("current_app")
        if ca_dp and ca_dp.data:
            s.mobile_app_package = ca_dp.data.get("package") or None
            s.mobile_screen_on = ca_dp.data.get("screen_on") if ca_dp.data.get("available") else None
            s.mobile_app_name = ca_dp.data.get("app_name") if ca_dp.data.get("available") else None
            if ca_dp.captured_at:
                s.field_ts["current_app"] = ca_dp.captured_at
            elif self._last_snapshot and self._last_snapshot.field_ts.get("current_app"):
                s.field_ts["current_app"] = self._last_snapshot.field_ts["current_app"]
        elif self._last_snapshot:
            s.mobile_app_package = getattr(self._last_snapshot, 'mobile_app_package', None)
            s.mobile_screen_on = getattr(self._last_snapshot, 'mobile_screen_on', None)
            s.mobile_app_name = getattr(self._last_snapshot, 'mobile_app_name', None)
            if self._last_snapshot.field_ts.get("current_app"):
                s.field_ts["current_app"] = self._last_snapshot.field_ts["current_app"]

        # 写哨兵心跳（供 main.py watchdog 检查）
        try:
            from mirrow_core.shared_state import set_sentinel_last_tick
            set_sentinel_last_tick(time.monotonic())
        except Exception:
            pass

        # 派生：心率统计
        stats_5min = self._store.get_heart_rate_stats(300)
        stats_prev = self._store.get_heart_rate_stats(600)
        # prev_5min = stats_prev 减去 stats_5min（近似）
        if stats_5min["count"] > 0:
            s.hr_avg_5min = stats_5min["avg"]
        if stats_prev["count"] > stats_5min["count"]:
            # 5-10分钟前：用总量减去近5分钟来近似
            prev_count = stats_prev["count"] - stats_5min["count"]
            prev_sum = stats_prev["avg"] * stats_prev["count"] - stats_5min["avg"] * stats_5min["count"]
            s.hr_avg_prev_5min = int(prev_sum / prev_count) if prev_count > 0 else None

        # 派生：连续高心率采样次数（3分钟和5分钟分开统计）
        stats_3min = self._store.get_hr_stats_recent(180)
        s.hr_count_3min = stats_3min["count"] if stats_3min else 0
        s.hr_count_5min = stats_5min["count"]

        # 连续高于阈值的次数：计数近 3min 内 HR>105 的实际采样数
        try:
            high_row = self._store._get_conn().execute(
                "SELECT COUNT(*) as cnt FROM health_snapshots "
                "WHERE heart_rate > 105 AND timestamp >= ?",
                ((datetime.now() - timedelta(minutes=3)).isoformat(),)
            ).fetchone()
            s.hr_sustained_high_count = high_row["cnt"] if high_row else 0
        except Exception:
            s.hr_sustained_high_count = 0

        return s

    # ── 告警处理 ──────────────────────────────────────

    async def _handle_alert(self, alert: Dict[str, Any], snapshot_id: int):
        """处理一条告警：活跃门控 → 深夜静默 → 推送 → 记录"""
        message = alert["message"]
        rule_id = alert["rule_id"]
        priority = alert["priority"]

        # 活跃门控：用户正在聊天/工作/游戏中 → 不推送，仅入上下文
        if self._should_suppress_push(self._last_snapshot):
            self._store.insert_event(
                rule_id=rule_id, priority=priority,
                snapshot_id=snapshot_id, message=message, pushed=False,
            )
            logger.debug(f"Sentinel: 用户活跃中，{rule_id} 仅入上下文")
            return

        # ⚠️ 此深夜静默仅 force 路径生效（正常 loop 在 _tick_inner 上游封堵）
        night_active = self._is_night_mode_active()
        hr_rules = {"C1", "C2", "H1", "H2", "H4", "M1", "M2", "M4"}
        if night_active and rule_id in hr_rules:
            self._store.insert_event(
                rule_id=rule_id, priority=priority,
                snapshot_id=snapshot_id, message=message, pushed=False,
            )
            logger.debug(f"Sentinel: 深夜模式静默告警 {rule_id}")
            return

        # LLM 润色（异步，不阻塞）
        if self._enable_llm_polish and self._call_llm and alert.get("message_prompt_extra"):
            try:
                polished = await self._polish_message(message, alert["message_prompt_extra"])
                if polished:
                    message = polished
            except Exception as e:
                logger.warning(f"Sentinel LLM 润色失败: {e}")

        # 推送
        if self._on_push:
            try:
                await self._on_push({
                    "message": message,
                    "rule_id": rule_id,
                    "priority": priority,
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as e:
                logger.error(f"Sentinel 推送失败: {e}")

        # 记录事件
        self._store.insert_event(
            rule_id=rule_id,
            priority=priority,
            snapshot_id=snapshot_id,
            message=message,
            pushed=True,
        )

    def _should_suppress_push(self, snapshot) -> bool:
        """活跃门控：仅当用户正在实时对话时抑制推送。
        AI 在用户打游戏/写代码/刷手机时可以主动发消息——与漫想模式一致。
        """
        if not snapshot:
            return False
        # DND 手动勿扰开关：一刀压制所有主动推送
        try:
            from mirrow_core.shared_state import should_suppress_proactive
            if should_suppress_proactive():
                return True
        except Exception:
            pass
        # 阈值秒内发过消息 → 正在实时对话，不打扰
        if snapshot.last_message_seconds is not None and snapshot.last_message_seconds < self._push_defer_seconds:
            return True
        return False

    async def _check_deferred_push(self):
        """检查延迟推送队列：阈值秒后用户仍未回复 → 执行 PUSH。回复了 → 放弃。"""
        if not self._deferred_push or not self._call_pro_llm or not self._on_push:
            return
        now = time.monotonic()
        for key, item in list(self._deferred_push.items()):
            if now - item["at"] < self._push_defer_seconds:
                continue
            del self._deferred_push[key]
            # 再次确认用户未回复
            if self._should_suppress_push(self._last_snapshot):
                logger.debug(f"Sentinel: 延迟推送取消（用户已回复）→ 入上下文 ({key})")
                continue
            # 执行推送
            try:
                pro_result = await self._call_pro_llm(item["clean"])
                content = pro_result.get("content", "") if isinstance(pro_result, dict) else str(pro_result)
                if content:
                    self._store.insert_event(
                        rule_id="flash_push", priority="pattern",
                        message=item["clean"], pushed=True, event_type="pattern",
                    )
                    await self._on_push({
                        "message": content, "rule_id": "flash_push",
                        "priority": "medium",
                        "timestamp": __import__('datetime').datetime.now().isoformat(),
                        "reasoning": pro_result.get("reasoning") if isinstance(pro_result, dict) else None,
                    })
                    logger.info(f"Perception PUSH (deferred): {content[:80]}")
            except Exception as e:
                logger.warning(f"Sentinel: 延迟推送失败: {e}")

    async def _polish_message(self, template_msg: str, prompt_extra: str) -> Optional[str]:
        """用 Flash 润色消息，保持 AI 的语气风格"""
        prompt = f"""你是AI伴侣。哨兵系统检测到用户的异常状态，生成了以下关心消息模板：

模板：{template_msg}

附加说明：{prompt_extra}

请用你最自然的说话方式重新表达，保持语气：直白简洁、口语化、不说矫情话。如果用户可能不舒服，语气温柔但不夸张。
只输出最终消息，不要前缀或解释。"""
        try:
            _st = get_truncation_limit("sentinel_tokens")
            result = await self._call_llm(prompt, temperature=0.6, max_tokens=_st if _st > 0 else 4096)
            if result and len(result.strip()) > 3:
                return result.strip()
        except Exception:
            pass
        return None

    # ── 运行时控制 ────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        logger.info(f"Sentinel: {'启用' if value else '暂停'}")

    def toggle(self) -> bool:
        self._enabled = not self._enabled
        self._persist_toggles()
        return self._enabled

    def toggle_gps(self) -> dict:
        """切换 GPS 子开关，返回新状态"""
        self._gps_enabled = not self._gps_enabled
        self._persist_toggles()
        logger.info(f"Sentinel GPS: {'手动开启' if self._gps_enabled else '手动暂停'}")
        return {"gps_enabled": self._gps_enabled}

    def _persist_toggles(self):
        """将所有开关状态写入 settings.json（重启不丢失）"""
        try:
            from mirrow_core.settings_manager import set_setting, get_setting
            current = get_setting("silicon_perception") or {}
            current["enabled"] = self._enabled
            current["gps_enabled"] = self._gps_enabled
            set_setting("silicon_perception", current)
        except Exception as e:
            logger.warning(f"哨兵开关持久化失败: {e}")

    async def reconnect_hr(self) -> dict:
        """手动重连手环，原子操作：断开→连接→读取验证"""
        if self._hr_source is None:
            return {"success": False, "error": "HR 数据源未初始化"}
        try:
            return await self._hr_source.reconnect_and_read()
        except Exception as e:
            return {"success": False, "error": str(e)}


    @property
    def status(self) -> Dict[str, Any]:
        hr_state = "unavailable"
        if self._hr_source:
            hr_state = self._hr_source.connection_state  # "connected" / "failed" / "unavailable"

        last_hr = None
        last_hr_time = None
        stats = self._store.get_hr_stats_recent(minutes=10)
        if stats["count"] > 0:
            last_hr = stats["avg"]
            recent = self._store.get_recent_snapshots(lookback_seconds=600)
            if recent:
                last_hr_time = recent[0].get("timestamp")
            # 最近 10min 有落库心率 → 无论 PC BLE 还是手机 relay 采到，都算 connected。
            # 修此前 bug：hr_connection 仅认 PC 的 connection_state，手环在手机边时 PC 恒 failed，
            # 手机接管成功却不反映到状态，导致"数据在、却显示 Failed / 暂无读数"。
            if hr_state != "unavailable":
                hr_state = "connected"

        # 数据源健康状态 + 失败原因
        source_states = {}
        source_errors = {}
        for src in self._sources:
            cs = getattr(src, 'connection_state', None)
            if cs:
                source_states[src.name] = cs
            err = getattr(src, 'last_error', None)
            if err and cs in ("failed", "disconnected"):
                source_errors[src.name] = err
        # heart_rate 的 source_state 用综合状态（含手机接管），覆盖仅 PC BLE 的结果
        if self._hr_source:
            source_states["heart_rate"] = hr_state
            if hr_state == "failed":
                source_errors["heart_rate"] = self._mobile_hr_error or "PC/手机均未采到心率（确认手环心率广播已开 + 已配对）"
            else:
                source_errors.pop("heart_rate", None)

        # 最新快照（结构化数据，前端自行格式化）
        snap = None
        if self._last_snapshot:
            s = self._last_snapshot
            snap = {
                "heart_rate": s.heart_rate,
                "input_idle_seconds": s.input_idle_seconds,
                "screen_active": s.screen_active,
                "foreground_app": s.foreground_app,
                "foreground_exe": getattr(s, 'foreground_exe', None),
                "mirrow_visible": s.mirrow_visible,
                "user_status": s.user_status,
                "is_period": s.is_period,
                "period_day": s.period_day,
                "steps_today": s.steps_today,
                "steps_stagnant_minutes": s.steps_stagnant_minutes,
                "location_address": s.location_address,
                "location_category": s.location_category,
                "behavior_state": getattr(s, 'behavior_state', None),
                "last_message_seconds": s.last_message_seconds,
                "screen_time_minutes": getattr(s, 'screen_time_minutes', None),
                "top_app_category": getattr(s, 'top_app_category', None),
                "mobile_app_package": getattr(s, 'mobile_app_package', None),
                "mobile_screen_on": getattr(s, 'mobile_screen_on', None),
                "mobile_app_name": getattr(s, 'mobile_app_name', None),
            }
            # 天气不在 tick snapshot 里（存 daily_health_summary），单独补入供总览展示
            try:
                _w = self._store.get_today_weather()
                if _w:
                    snap["weather_temp"] = _w.get("temp")
                    snap["weather_humidity"] = _w.get("humidity")
                    snap["weather_desc"] = _w.get("desc")
            except Exception:
                pass

        return {
            "running": self._running,
            "enabled": self._enabled,
            "tick_count": self._tick_count,
            "tick_interval": self._get_tick_interval(),
            "sources": [s.name for s in self._sources],
            "source_states": source_states,
            "source_errors": source_errors,
            "last_snapshot": snap,
            "recent_alerts": self._store.get_recent_events(hours=24),
            "hr_connection": hr_state,
            "hr_last_reading": last_hr,
            "hr_last_reading_time": last_hr_time,
            "gps_enabled": self._gps_enabled,
            "hr_consecutive_failures": self._hr_source._consecutive_failures if self._hr_source else 0,
            "hr_error": self._mobile_hr_error,
            # debug
            "debug": {
                "flash_summary": self._summarizer.get_summary() if hasattr(self, '_summarizer') and self._summarizer else "",
                "flash_triggers": self._trigger_detector._last_fired_triggers if hasattr(self, '_trigger_detector') and self._trigger_detector else [],
                "last_flash_context": getattr(self, '_last_flash_context', None),
                "last_flash_time": self._trigger_detector._last_flash_time if hasattr(self, '_trigger_detector') and self._trigger_detector else 0,
                "behavior_mode": (
                    "rest" if self._last_snapshot and self._last_snapshot.user_status in ('sleeping','napping','bathing','eating')
                    else "away" if self._last_snapshot and self._last_snapshot.user_status == 'out'
                    else "active" if self._last_snapshot else "unknown"
                ),
                "rule_count": len(self._detector._rules) if self._detector else 0,
            },
        }

    def get_snapshot_line(self) -> str:
        """返回哨兵快照行（供 ingredients.py / context_scheduler 调用）。

        双行模式：结构化数据行（始终有，零 LLM 误差）+ Flash 感知行（可选）。
        """
        data_line = self._annotator.build_snapshot_line() if self._annotator else ""
        flash_line = self._summarizer.get_summary()
        if flash_line:
            return f"{data_line}\n      {flash_line}"
        return data_line

    def build_timeline_annotations(self) -> str:
        """返回哨兵时间线注解文本（供 ingredients.py / context_scheduler 调用）。"""
        if self._annotator:
            annotations = self._annotator.build_timeline_annotations()
            if annotations:
                return "\n".join(a.get("content", "") for a in annotations)
        return ""

    @staticmethod
    def check_health() -> bool:
        """供 main.py 每日维护调用。检查哨兵 tick 心跳。
        返回 True 表示正常，False 表示可能卡死。
        """
        try:
            from mirrow_core.shared_state import get_sentinel_last_tick
            import time
            last_tick = get_sentinel_last_tick()
            if last_tick <= 0:
                return True  # 还没跑过，不告警
            return (time.monotonic() - last_tick) < 120
        except Exception:
            return True


# ═══════════════════════════════════════════════════════════════
# 模块级辅助函数
# ═══════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════
# 模块级辅助函数
def _infer_behavior_state(snapshot) -> str:
    """推断当前行为状态，写入 health_snapshots.behavior_state。"""
    try:
        from silicon_perception.analysis.behavior_state import infer_behavior_state
        mobile_app_name = getattr(snapshot, 'mobile_app_name', None)
        mobile_screen = getattr(snapshot, 'mobile_screen_on', None)
        if mobile_screen is None:
            # 不臆造手机在用：仅当 screen_time 非陈旧时才用 top_app_category 推断
            if hasattr(snapshot, 'is_stale') and not snapshot.is_stale("screen_time", 2400):
                mobile_screen = (snapshot.top_app_category is not None)
            else:
                mobile_screen = None
        # 手机连续亮屏时长（区分瞥一眼 vs 真正双设备使用）
        phone_dur = 0.0
        try:
            s = get_sentinel()
            if s and s._phone_session_start:
                phone_dur = (__import__('datetime').datetime.now() - __import__('datetime').datetime.fromisoformat(s._phone_session_start)).total_seconds()
        except Exception:
            pass
        return infer_behavior_state(
            input_idle_seconds=snapshot.input_idle_seconds,
            foreground_exe=getattr(snapshot, 'foreground_exe', None),
            foreground_app=snapshot.foreground_app,
            screen_active=snapshot.screen_active,
            mobile_app_name=mobile_app_name,
            mobile_screen_on=mobile_screen,
            phone_session_seconds=phone_dur,
        )
    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════

def _build_tick_context(snapshot) -> tuple:
    """构建 Flash 上下文：当前时间、状态时间线、作息表。"""
    import datetime as _dt
    now = _dt.datetime.now()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    # 当前时间
    is_rest = False
    try:
        from calendar_manager.database import is_rest_day_today
        is_rest = is_rest_day_today()
    except Exception:
        pass
    day_type = "休息日" if is_rest else "工作日"
    current_time = f"现在是 {weekdays[now.weekday()]} {now.strftime('%H:%M')}（{day_type}）"

    # 状态时间线
    status_timeline = ""
    try:
        from silicon_perception.recording.health_store import get_store
        store = get_store()
        conn = store._get_conn()
        today = now.strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM status_change_log WHERE date(timestamp)=? ORDER BY timestamp",
            (today,)
        ).fetchall()
        if rows:
            parts = []
            for r in rows:
                ts = r["timestamp"][11:16] if len(r["timestamp"]) >= 16 else ""
                status = r.get("new_status") or r.get("status", "?")
                parts.append(f"{status}({ts})" if ts else status)
            status_timeline = " → ".join(parts)
            # 当前状态持续时长
            if rows:
                last = rows[-1]
                last_ts = last["timestamp"]
                try:
                    from dateutil.parser import parse as _dt_parse
                    last_dt = _dt_parse(last_ts).replace(tzinfo=None)
                except Exception:
                    last_dt = _dt.datetime.fromisoformat(last_ts)
                dur = now - last_dt
                dur_str = f"{int(dur.total_seconds()//3600)}h{int((dur.total_seconds()%3600)//60)}m"
                status_timeline += f"，持续{dur_str}"
    except Exception:
        pass

    # 作息表
    schedule_text = ""
    try:
        conn2 = None
        try:
            from silicon_perception.recording.health_store import get_store
            conn2 = get_store()._get_conn()
        except Exception:
            pass
        if conn2:
            row = conn2.execute("SELECT * FROM user_schedule WHERE id=1").fetchone()
            if row and row["work_start"]:
                s = f"作息 {row['work_start']}-{row['work_end']}"
                if row["lunch_start"]:
                    s += f"，午休 {row['lunch_start']}-{row['lunch_end']}"
                schedule_text = s
    except Exception:
        pass

    return current_time, status_timeline, schedule_text


def _build_source_freshness(datapoints: dict, snapshot) -> dict:
    """从 snapshot.field_ts 构建传感器时效信息（真实采集时间，非读取时刻）。
    Flash 据此区分"实时数据"和"陈旧缓存"。"""
    import datetime as _dt
    now = _dt.datetime.now()
    result = {}

    def _age(iso_str: str) -> str:
        if not iso_str:
            return ""
        try:
            dt = _dt.datetime.fromisoformat(iso_str)
            sec = (now - dt).total_seconds()
            if sec < 120:
                return "(刚刚)"
            elif sec < 3600:
                return f"({sec/60:.0f}分钟前)"
            else:
                return f"({sec/3600:.1f}小时前)"
        except Exception:
            return ""

    fts = getattr(snapshot, 'field_ts', {}) or {}

    # 心率：有值→标时效；无值→标断连
    if snapshot.heart_rate is not None:
        result["heart_rate"] = _age(fts.get("heart_rate"))
    else:
        result["heart_rate_disconnected"] = _age(fts.get("heart_rate"))

    # 步数
    if snapshot.steps_today is not None:
        result["steps"] = _age(fts.get("steps"))
    else:
        result["steps_disconnected"] = _age(fts.get("steps"))

    # GPS
    if snapshot.location_category or snapshot.location_lat is not None:
        result["gps"] = _age(fts.get("gps"))
    else:
        result["gps_disconnected"] = _age(fts.get("gps"))

    # 屏幕使用时长
    if snapshot.screen_time_minutes is not None:
        result["screen_time"] = _age(fts.get("screen_time"))

    # 当前前台 App
    if snapshot.mobile_app_package:
        result["current_app"] = _age(fts.get("current_app"))

    return result

