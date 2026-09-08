# 漫想创建层
#
# 功能：
# 1. 定义事件类型枚举
# 2. 按概率随机创建行为事件
# 3. 支持概率动态调整（用户追踪/休眠事件概率累加）
# 4. Phase 1: 实现关键词生成、记忆抓取事件
# 5. 支持玩游戏关键词检测的概率优先调整

from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
import random
import asyncio
from .host_hooks import ordinary
import logging
import os

from .event_types import EventType, WanderEvent
from .event_handlers import EventHandlerFactory
from .user_status import UserStatus, get_user_status
from .activity_engine import ActivityEngine

logger = logging.getLogger(__name__)


def _format_elapsed(seconds: Optional[float]) -> str:
    """Render an elapsed duration without assigning it an incorrect meuser."""
    if seconds is None:
        return "未知"
    try:
        total_minutes = max(0, int(float(seconds) / 60))
    except (TypeError, ValueError):
        return "未知"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


def _status_duration_text() -> str:
    """Elapsed time since the current explicit/automatic status transition."""
    try:
        from .user_status import get_status_history
        history = get_status_history() or []
        if not history:
            return "未知"
        stamp = history[-1].get("time")
        when = datetime.fromisoformat(str(stamp))
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return _format_elapsed((datetime.now() - when).total_seconds())
    except Exception:
        return "未知"


def _private_message_idle_text() -> str:
    """Elapsed time since the last private message, not physical absence."""
    try:
        from mirrow_core.shared_state import get_last_private_chat_time
        # The getter performs one bounded chronicle recovery after restart;
        # it never falls back to the mode-switch/device-idle timestamp.
        last = get_last_private_chat_time(resolve_from_store=True)
        if last is None:
            return "未知"
        if last.tzinfo is not None:
            last = last.astimezone().replace(tzinfo=None)
        return _format_elapsed((datetime.now() - last).total_seconds())
    except Exception:
        return "未知"



@dataclass
class ProbabilityState:
    """概率状态 — 滑动窗口 4 级升/降权 + SLEEP 硬冷却"""

    # ── 滑动窗口（新核心机制） ──
    _recent_events: deque = field(default_factory=lambda: deque(maxlen=8))

    # ── SLEEP 硬冷却 ──
    _sleep_cooldown: bool = False  # SLEEP 触发后下一次跳过

    # ── 自省持久化（唯一保留的跨 session 状态） ──
    self_reflection_bonus: float = 0.0
    events_since_self_reflection: int = 0

    _PERSIST_FILE: str = field(default="")

    def __post_init__(self):
        if not self._PERSIST_FILE:
            import os
            self._PERSIST_FILE = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "wander_prob_state.json"
            )
        self._load_persist_state()

    def _load_persist_state(self):
        import json, os
        try:
            if os.path.exists(self._PERSIST_FILE):
                with open(self._PERSIST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.self_reflection_bonus = data.get("self_reflection_bonus", 0.0)
                self.events_since_self_reflection = data.get("events_since_self_reflection", 0)
        except Exception:
            pass

    def _save_persist_state(self):
        import json, os
        try:
            os.makedirs(os.path.dirname(self._PERSIST_FILE), exist_ok=True)
            with open(self._PERSIST_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "self_reflection_bonus": self.self_reflection_bonus,
                    "events_since_self_reflection": self.events_since_self_reflection,
                }, f)
        except Exception:
            pass

    def reset(self):
        """漫想模式开启时重置。自省 bonus 持久化跨会话不重置。"""
        self._recent_events.clear()
        self._sleep_cooldown = False

    # ── 滑动窗口因子查表 ──

    def _get_window_factor(self, event_type: EventType) -> float:
        """按窗口内出现次数返回权重因子"""
        count = sum(1 for e in self._recent_events if e == event_type)
        if count == 0:
            return 1.8   # 🟢 新鲜度加成
        elif count == 1:
            return 1.0   # 🟡 正常
        elif count == 2:
            return 0.4   # 🟠 轻度抑制
        else:  # 3+
            return 0.05  # 🔴 重度抑制

    # ── 事件记录 ──

    def record_event(self, event_type: EventType):
        """事件触发后记录到滑动窗口"""
        self._recent_events.append(event_type)

    def on_event(self, event_type: EventType):
        """统一的事件触发后处理"""
        self.record_event(event_type)
        # SLEEP 冷却
        if event_type == EventType.SLEEP:
            self._sleep_cooldown = True
        else:
            self._sleep_cooldown = False
        # 自省持久化
        self.events_since_self_reflection += 1
        if event_type == EventType.SELF_REFLECTION:
            self.self_reflection_bonus = 0.0
            self.events_since_self_reflection = 0
            self._save_persist_state()
        elif self.events_since_self_reflection >= 15:
            self.self_reflection_bonus += 0.05
            self.events_since_self_reflection = 0
            self._save_persist_state()

    # ── 用户状态 → 概率分布 ──

    STATUS_PROBABILITIES = {
        UserStatus.GAMING: {
            EventType.USER_TRACKING: 0.38,
            EventType.SLEEP: 0.08,
            EventType.KEYWORD_EXPANSION: 0.10,
            EventType.MEMORY_FETCH: 0.10,
            EventType.BROWSE_NEWS: 0.07,
            EventType.SELF_REFLECTION: 0.05,
            EventType.BROWSE_BOOKMARKS: 0.07,
            EventType.LISTEN_MUSIC: 0.05,
        },
        UserStatus.OUT: {
            EventType.USER_TRACKING: 0.18,   # +8% vs IDLE
            EventType.SLEEP: 0.08,
            EventType.KEYWORD_EXPANSION: 0.18,
            EventType.MEMORY_FETCH: 0.16,
            EventType.BROWSE_NEWS: 0.09,
            EventType.SELF_REFLECTION: 0.04,
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.10,    # OUT 多听歌
        },
        UserStatus.BATHING: {
            EventType.USER_TRACKING: 0.08,
            EventType.SLEEP: 0.03,           # -5% vs 基准
            EventType.KEYWORD_EXPANSION: 0.20,
            EventType.MEMORY_FETCH: 0.16,
            EventType.BROWSE_NEWS: 0.09,
            EventType.SELF_REFLECTION: 0.12, # +8% 独处自省
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.08,
        },
        UserStatus.EATING: {
            EventType.USER_TRACKING: 0.08,
            EventType.SLEEP: 0.06,
            EventType.KEYWORD_EXPANSION: 0.20,
            EventType.MEMORY_FETCH: 0.16,
            EventType.BROWSE_NEWS: 0.17,     # +8% 吃饭看新闻
            EventType.SELF_REFLECTION: 0.02, # -2%
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.06,
        },
        UserStatus.NAPPING: {
            EventType.USER_TRACKING: 0.08,
            EventType.SLEEP: 0.06,
            EventType.KEYWORD_EXPANSION: 0.20,
            EventType.MEMORY_FETCH: 0.21,    # +5% 翻回忆
            EventType.BROWSE_NEWS: 0.09,
            EventType.SELF_REFLECTION: 0.04,
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.08,
        },
        UserStatus.SLEEPING: {
            EventType.USER_TRACKING: 0.06,
            EventType.SLEEP: 0.06,
            EventType.KEYWORD_EXPANSION: 0.18,
            EventType.MEMORY_FETCH: 0.21,    # +5% 翻回忆
            EventType.BROWSE_NEWS: 0.09,
            EventType.SELF_REFLECTION: 0.04,
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.12,    # +4% 深夜听歌
        },
        UserStatus.OTHER: {
            EventType.USER_TRACKING: 0.18,   # +8% vs IDLE
            EventType.SLEEP: 0.08,
            EventType.KEYWORD_EXPANSION: 0.18,
            EventType.MEMORY_FETCH: 0.16,
            EventType.BROWSE_NEWS: 0.09,
            EventType.SELF_REFLECTION: 0.04,
            EventType.BROWSE_BOOKMARKS: 0.10,
            EventType.LISTEN_MUSIC: 0.06,
        },
        UserStatus.CODING: {
            EventType.USER_TRACKING: 0.38,
            EventType.SLEEP: 0.27,
            EventType.KEYWORD_EXPANSION: 0.08,
            EventType.MEMORY_FETCH: 0.08,
            EventType.BROWSE_NEWS: 0.06,
            EventType.SELF_REFLECTION: 0.05,
            EventType.BROWSE_BOOKMARKS: 0.05,
            EventType.LISTEN_MUSIC: 0.05,
        },
    }

    # ── eating 按 location 分表（2026-08-01） ──
    # 在家吃：类似当前 EATING 表（BROWSE_NEWS 高，边吃边刷）
    STATUS_PROBABILITIES_EATING_HOME = {
        EventType.USER_TRACKING: 0.08,
        EventType.SLEEP: 0.06,
        EventType.KEYWORD_EXPANSION: 0.20,
        EventType.MEMORY_FETCH: 0.16,
        EventType.BROWSE_NEWS: 0.17,
        EventType.SELF_REFLECTION: 0.02,
        EventType.BROWSE_BOOKMARKS: 0.10,
        EventType.LISTEN_MUSIC: 0.06,
    }
    # 外出吃：USER_TRACKING 高（好奇在外面干嘛），SLEEP 更低（不会睡觉）
    STATUS_PROBABILITIES_EATING_OUT = {
        EventType.USER_TRACKING: 0.22,   # +14% vs 在家
        EventType.SLEEP: 0.02,           # 外出不可能睡
        EventType.KEYWORD_EXPANSION: 0.14,
        EventType.MEMORY_FETCH: 0.12,
        EventType.BROWSE_NEWS: 0.09,
        EventType.SELF_REFLECTION: 0.04,
        EventType.BROWSE_BOOKMARKS: 0.10,
        EventType.LISTEN_MUSIC: 0.10,    # 外出多听歌
    }

    def get_probabilities(
        self, user_status: Optional[UserStatus] = None, idle_seconds: float = 0.0
    ) -> Dict[EventType, float]:
        """计算当前各事件概率（legacy 兼容，内部转发 get_probabilities_rich）。"""
        return self.get_probabilities_rich(
            status_meta=None, user_status=user_status, idle_seconds=idle_seconds
        )

    def get_probabilities_rich(
        self, status_meta=None, user_status: Optional[UserStatus] = None,
        idle_seconds: float = 0.0
    ) -> Dict[EventType, float]:
        """计算当前各事件概率（含滑动窗口因子 + SLEEP 冷却 + 追踪加成）。

        Args:
            status_meta: StatusMeta（优先，含 location 信息）
            user_status: UserStatus enum（status_meta 为 None 时的回退）
            idle_seconds: 空闲秒数
        """
        # 空闲时长追踪加成
        hours_idle = idle_seconds / 3600.0
        if hours_idle <= 3.0:
            idle_tracking_bonus = hours_idle * 0.10
        elif hours_idle <= 4.0:
            idle_tracking_bonus = 0.30 + (hours_idle - 3.0) * 0.35
        else:
            idle_tracking_bonus = min(0.65 + (hours_idle - 4.0) * 0.05, 0.80)

        mobile_tracking_bonus = 0.0
        try:
            from mirrow_core.shared_state import get_mobile_connected
            if get_mobile_connected():
                mobile_tracking_bonus = 0.08
        except ImportError:
            pass

        # 获取基础概率（优先 StatusMeta，回退 UserStatus）
        probs = None
        if status_meta is not None:
            cat = status_meta.activity_category
            loc = status_meta.location
            # eating 按 location 分表
            if cat == "eating" and loc == "out":
                probs = dict(self.STATUS_PROBABILITIES_EATING_OUT)
            elif cat == "eating" and loc == "home":
                probs = dict(self.STATUS_PROBABILITIES_EATING_HOME)
            elif cat == "eating":
                # unknown location → 默认在家
                probs = dict(self.STATUS_PROBABILITIES_EATING_HOME)
            elif cat == "idle":
                probs = self._dynamic_base_probs()
            else:
                # 其他 activity_category → 查 STATUS_PROBABILITIES（用 UserStatus 键）
                try:
                    us = UserStatus(cat)
                    raw = self.STATUS_PROBABILITIES.get(us)
                    if raw:
                        probs = dict(raw)
                except Exception:
                    pass
            if probs is None:
                probs = self._dynamic_base_probs()
        elif user_status and user_status != UserStatus.IDLE:
            raw_probs = self.STATUS_PROBABILITIES.get(user_status)
            probs = dict(raw_probs) if raw_probs else self._dynamic_base_probs()
        else:
            probs = self._dynamic_base_probs()

        # 公开搜索 + Dots 的只读浏览在各种状态下都可以作为轻量休闲活动。
        probs.setdefault(EventType.BROWSE_XIAOHONGSHU, 0.10)
        # 私有朋友圈是独立领域能力；所有状态下均保持低权重可选。
        probs.setdefault(EventType.BROWSE_SOCIAL_FEED, 0.08)

        # 叠加追踪加成
        if idle_tracking_bonus > 0:
            probs = self._apply_tracking_bonus(probs, idle_tracking_bonus)
        if mobile_tracking_bonus > 0:
            probs = self._apply_tracking_bonus(probs, mobile_tracking_bonus)

        # ★ 应用滑动窗口因子
        probs = self._apply_window_factors(probs)

        # ★ SLEEP 冷却
        if self._sleep_cooldown and EventType.SLEEP in probs:
            probs[EventType.SLEEP] = 0.0

        # 归一化
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        return probs

    def _dynamic_base_probs(self) -> Dict[EventType, float]:
        """IDLE 模式动态基础概率（含自省持久化 bonus）"""
        probs = {
            EventType.KEYWORD_EXPANSION: 0.20,
            EventType.MEMORY_FETCH: 0.18,
            EventType.USER_TRACKING: 0.18,
            EventType.SLEEP: 0.16,
            EventType.BROWSE_NEWS: 0.09,
            EventType.BROWSE_XIAOHONGSHU: 0.10,
            EventType.BROWSE_SOCIAL_FEED: 0.08,
            EventType.SELF_REFLECTION: 0.04,
            EventType.BROWSE_BOOKMARKS: 0.05,
            EventType.LISTEN_MUSIC: 0.06,
        }
        # 仅保留自省 bonus（旧 bonus 系统其余部分已删除）
        if self.self_reflection_bonus > 0:
            probs[EventType.SELF_REFLECTION] += self.self_reflection_bonus
            # 从其他事件均摊
            others = {e: p for e, p in probs.items() if e != EventType.SELF_REFLECTION}
            other_total = sum(others.values())
            if other_total > 0:
                deduction = self.self_reflection_bonus / other_total
                for e in others:
                    probs[e] = max(0.01, probs[e] - probs[e] * deduction)
        # 归一化
        total = sum(probs.values())
        return {k: v / total for k, v in probs.items()} if total > 0 else probs

    def _apply_window_factors(self, probs: Dict[EventType, float]) -> Dict[EventType, float]:
        """应用滑动窗口 4 级因子"""
        result = {}
        for e, p in probs.items():
            factor = self._get_window_factor(e)
            result[e] = p * factor
        return result

    def _apply_tracking_bonus(
        self, probs: Dict[EventType, float], bonus: float
    ) -> Dict[EventType, float]:
        """在已有概率分布上叠加追踪加成"""
        tracking = probs.get(EventType.USER_TRACKING, 0)
        new_tracking = min(tracking + bonus, 1.0)
        actual_bonus = new_tracking - tracking
        if actual_bonus <= 0:
            return probs

        others = {e: p for e, p in probs.items() if e != EventType.USER_TRACKING}
        others_total = sum(others.values())
        if others_total <= 0:
            return probs

        result = {EventType.USER_TRACKING: new_tracking}
        for e, p in others.items():
            result[e] = max(0, p - actual_bonus * (p / others_total))

        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        return result


class WanderCreator:
    """
    漫想创建层 - 按概率随机创建行为事件

    核心逻辑：
    - 漫想模式开启时，每隔5分钟创建一个事件
    - 支持概率动态调整
    - Phase 2: 支持小红书、QQ空间事件
    - 支持玩游戏关键词检测的概率优先调整
    """

    # 默认配置
    DEFAULT_EVENT_INTERVAL = 5 * 60  # 事件间隔：5分钟（秒）

    def __init__(
        self,
        event_interval: int = None,
        on_event_created: Optional[Callable[[WanderEvent], None]] = None,
        call_llm_func: Optional[Callable] = None,
        pro_llm_func: Optional[Callable] = None,
        share_llm_func: Optional[Callable] = None,
        get_memories_func: Optional[Callable] = None,
        tracking_service: Optional[Any] = None,
        web_search_func: Optional[Callable] = None,
        get_recent_user_messages_func: Optional[Callable] = None,
        get_idle_seconds_func: Optional[Callable] = None,
        song_cache: Optional[Any] = None,
        k_self_book: Optional[Any] = None,
        music_mcp_client: Optional[Any] = None,
        on_push_to_user: Optional[Callable] = None,
        on_notification: Optional[Callable] = None,
        activity_engine_enabled: bool = False,
        runtime_v3_enabled: bool = True,
    ):
        """
        初始化漫想创建层

        Args:
            event_interval: 事件创建间隔（秒）
            on_event_created: 事件创建后的回调函数
            call_llm_func: LLM调用函数
            get_memories_func: 获取记忆的函数（用于记忆抓取事件）
            tracking_service: 用户追踪服务实例
            web_search_func: 网页搜索异步函数（用于看新闻事件）
            get_recent_user_messages_func: 获取最近用户消息的函数
            get_idle_seconds_func: 获取用户空闲秒数的函数
            song_cache: 音乐缓存实例（用于听歌事件）
            k_self_book: AI 自我书实例（用于听歌事件品味记录）
            music_mcp_client: 音乐 MCP 客户端（用于听歌事件播放）
        """
        self.event_interval = event_interval or self.DEFAULT_EVENT_INTERVAL
        self._on_event_created = on_event_created
        self._call_llm = call_llm_func
        # pro_llm_func is retained only for old direct callers. Production
        # injects share_llm_func, which follows the current Flash cost policy.
        self._share_llm = share_llm_func or pro_llm_func
        self._on_push_to_user = on_push_to_user
        self._on_notification = on_notification
        self._get_memories = get_memories_func
        self._get_recent_user_messages = get_recent_user_messages_func
        self._get_idle_seconds = get_idle_seconds_func

        # 概率状态（滑动窗口 4 级因子 + SLEEP 冷却）
        self._probability_state = ProbabilityState()

        # 后台任务
        self._create_task: Optional[asyncio.Task] = None
        self._running = False
        self._event_lock = asyncio.Lock()

        # 事件处理器工厂
        self._handler_factory = EventHandlerFactory(
            call_llm_func=call_llm_func,
            get_memories_func=get_memories_func,
            tracking_service=tracking_service,
            web_search_func=web_search_func,
            song_cache=song_cache,
            k_self_book=k_self_book,
            music_mcp_client=music_mcp_client,
            on_notification=on_notification,
        )

        # 活动引擎开关（默认关：DeepSeek 涨价期间冻结，成本重设计后开启）
        self._activity_engine_enabled = activity_engine_enabled
        self._runtime_v3_enabled = runtime_v3_enabled
        self._runtime_runner = None

        # 活动引擎（全自主区间式行动）：计划 → 持续活动会话 → 节点 → 结算
        self._activity_engine = ActivityEngine(
            call_llm_func=call_llm_func,
            handler_factory=self._handler_factory,
            probability_state=self._probability_state,
            get_idle_seconds=get_idle_seconds_func,
            get_recent_user_messages=get_recent_user_messages_func,
            on_push_to_user=on_push_to_user,
            on_event_created=on_event_created,
        )

    def _ensure_runtime_runner(self):
        """Construct v3 without opening its database until the feature is enabled."""
        if self._runtime_runner is not None:
            return self._runtime_runner
        from .node_execution_adapter import NodeExecutionAdapter
        from .plan_decision_adapter import PlanDecisionAdapter
        from .runtime_controller import WanderRuntimeController
        from .runtime_decision_adapters import NodeReviewAdapter, SettlementAdapter
        from .runtime_runner import WanderRuntimeRunner
        from .runtime_share_adapter import RuntimeShareAdapter
        from .runtime_store import WanderRuntimeStore
        from .self_reflection_adapter import SelfReflectionAdapter
        from .wish_commit_adapter import WishCommitAdapter
        from .wish_store import get_wish_store

        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "events", "wander_runtime.db")
        store = WanderRuntimeStore(db_path)
        controller = WanderRuntimeController(store)
        allowed = {
            EventType.KEYWORD_EXPANSION,
            EventType.MEMORY_FETCH,
            EventType.BROWSE_NEWS,
            EventType.BROWSE_XIAOHONGSHU,
            EventType.BROWSE_SOCIAL_FEED,
            EventType.BROWSE_TAOBAO,
            EventType.VISIT_LOUNGE,
            EventType.BROWSE_BOOKMARKS,
            EventType.SELF_REFLECTION,
            EventType.SLEEP,
            EventType.USER_TRACKING,
        }
        try:
            # 只有音乐 MCP 在运行时才把听歌加入计划目录；进入节点后必须
            # 通过同一真实播放边界，不能降级为只选歌/只分析。
            music_client = self._handler_factory.music_mcp_client
            if music_client and music_client.is_running():
                allowed.add(EventType.LISTEN_MUSIC)
        except Exception:
            pass
        share_adapter = None
        if self._share_llm and self._on_push_to_user:
            share_adapter = RuntimeShareAdapter(
                store, self._share_llm, self._on_push_to_user
            )
        self._runtime_runner = WanderRuntimeRunner(
            store=store,
            controller=controller,
            # Initial planning chooses one lived activity.  A second activity
            # remains possible only as a review-time switch after the first
            # activity has produced a real thought/reason for changing course.
            planner=PlanDecisionAdapter(store, allowed_event_types=allowed, max_activities=1),
            executor=NodeExecutionAdapter(
                store,
                self._handler_factory,
                get_idle_seconds=self._activity_engine._idle_seconds,
            ),
            reviewer=NodeReviewAdapter(store),
            settlement=SettlementAdapter(store),
            self_reflection=SelfReflectionAdapter(store),
            wish_commit=WishCommitAdapter(store, get_wish_store()),
            share_adapter=share_adapter,
            context_provider=self._runtime_context_bundle,
            on_activity_settled=self._probability_state.on_event,
            on_notification=self._on_notification,
            default_interval_seconds=self.event_interval,
        )
        return self._runtime_runner

    def _v3_today_counts(self) -> Dict[str, int]:
        """v3 运行时今日活动计数（display_name → 次数），供可做事件列表排序与标注。

        display_name 与 EVENT_CATALOG 对齐（自省/刷小红书/听歌…），今日未做过的活动缺席。
        """
        try:
            runner = self._runtime_runner
            if runner is None:
                return {}
            from .event_catalog import strategy_for
            today = datetime.now().strftime("%Y-%m-%d")
            counts: Dict[str, int] = {}
            for a in runner.store.list_activities_created_on(today):
                try:
                    label = strategy_for(EventType(a["activity_type"])).display_name
                except Exception:
                    label = str(a.get("activity_type") or "未知")
                counts[label] = counts.get(label, 0) + 1
            return counts
        except Exception:
            return {}

    def _v3_today_wander(self, counts: Optional[Dict[str, int]] = None) -> str:
        """v3 运行时今天已完成的漫想活动文本，供 planner 感知频率。"""
        try:
            if counts is None:
                counts = self._v3_today_counts()
            if not counts:
                return ""
            summary = "、".join(
                f"{label} {n} 次" for label, n in sorted(counts.items(), key=lambda x: -x[1])
            )
            return f"今日已完成：{summary}。"
        except Exception:
            return ""

    async def _runtime_context_bundle(self):
        from mirrow_core.shared_state import get_active_session_id, get_latest_persona_prompt
        from .plan_decision_adapter import RuntimeContext
        from .runtime_decision_adapters import DecisionContext
        from .runtime_runner import RuntimeContextBundle
        from .self_reflection_adapter import SelfReflectionContext
        from .user_status import get_user_status_context

        persona = get_latest_persona_prompt() or ""
        session_id = get_active_session_id() or ""
        mood = self._activity_engine._mood()
        user_status = get_user_status_context() or ""
        away = self._activity_engine._away_context()
        physical_idle = _format_elapsed(self._activity_engine._physical_idle_seconds())
        status_duration = _status_duration_text()
        private_message_idle = _private_message_idle_text()
        today_wander = self._activity_engine._today_wander()
        today_counts: Dict[str, int] = {}
        if self._runtime_v3_enabled:
            today_counts = self._v3_today_counts()
            v3_today = self._v3_today_wander(today_counts)
            if v3_today:
                today_wander = v3_today
        recent_text = ""
        if self._get_recent_user_messages:
            try:
                recent = await self._get_recent_user_messages(limit=12)
                recent_text = "\n".join(str(item)[:500] for item in (recent or []))
            except Exception:
                logger.warning("v3 读取近期对话失败", exc_info=True)
        probabilities = self._runtime_probabilities()
        decision = DecisionContext(
            persona=persona,
            session_id=session_id,
            mood=mood,
            user_status=user_status,
            away_duration=away,
            status_duration=status_duration,
            private_message_idle=private_message_idle,
            physical_idle=physical_idle,
            today_conversation=recent_text,
            today_wander=today_wander,
        )
        try:
            from .brain_architecture import get_cached_content
            brain_summary = (get_cached_content().get("content") or "")[:5000]
        except Exception:
            brain_summary = ""
        try:
            from .wish_history import get_wish_context_for_reflection
            wish_context = get_wish_context_for_reflection()
        except Exception:
            wish_context = ""
        try:
            from .capability_catalog import get_capability_catalog
            capability_catalog = get_capability_catalog()
        except Exception:
            capability_catalog = "[]"
        return RuntimeContextBundle(
            plan=RuntimeContext(
                persona=persona,
                session_id=session_id,
                mood=mood,
                user_status=user_status,
                away_duration=away,
                status_duration=status_duration,
                private_message_idle=private_message_idle,
                physical_idle=physical_idle,
                today_conversation=recent_text,
                today_wander=today_wander,
                probabilities=probabilities,
                today_counts=today_counts,
            ),
            decision=decision,
            self_reflection=SelfReflectionContext(
                persona=persona,
                session_id=session_id,
                user_status=user_status,
                trigger_reason="自主倾向",
                capability_catalog=capability_catalog,
                brain_summary=brain_summary,
                wish_context=wish_context,
                today_conversation=recent_text,
            ),
        )

    def _runtime_probabilities(self) -> Dict[str, float]:
        try:
            from .user_status import get_status_meta
            status_meta = get_status_meta()
        except Exception:
            status_meta = None
        try:
            probabilities = self._probability_state.get_probabilities_rich(
                status_meta=status_meta,
                user_status=get_user_status(),
                idle_seconds=self._activity_engine._idle_seconds(),
            )
            return {
                (event.value if hasattr(event, "value") else str(event)): float(value)
                for event, value in probabilities.items()
            }
        except Exception:
            return {}

    def _select_event_type(self) -> EventType:
        """
        根据当前概率随机选择事件类型（使用 StatusMeta 获取 location 信息）。
        """
        # 优先获取 StatusMeta（含 location），回退 UserStatus
        try:
            from .user_status import get_status_meta as _gsm
            status_meta = _gsm()
        except Exception:
            status_meta = None
        user_status = get_user_status()
        idle_seconds = 0.0
        if self._get_idle_seconds:
            try:
                idle_seconds = self._get_idle_seconds()
            except Exception:
                pass

        probs = self._probability_state.get_probabilities_rich(
            status_meta=status_meta, user_status=user_status, idle_seconds=idle_seconds
        )
        events = list(probs.keys())
        weights = [probs[e] for e in events]

        selected = random.choices(events, weights=weights, k=1)[0]
        label = status_meta.display_label if status_meta else user_status.value
        logger.info(f"随机选择事件: {selected.value}, 状态={label}, 空闲={idle_seconds:.0f}s, 概率分布: {[(e.value, f'{p:.2%}') for e, p in probs.items()]}")

        return selected

    async def create_event(self) -> WanderEvent:
        """
        创建一个漫想事件

        Returns:
            创建的事件
        """
        if self._event_lock.locked():
            logger.warning("已有漫想事件正在执行，跳过本次创建")
            return WanderEvent(
                event_type=EventType.SLEEP,
                timestamp=datetime.now(),
                description="跳过重入事件",
                process_log="已有漫想事件正在执行，跳过本次创建",
                details={"skipped": True, "reason": "event_in_progress"}
            )

        async with self._event_lock:
            return await self._create_event_unlocked()

    @ordinary
    async def _create_event_unlocked(self) -> WanderEvent:
        """创建一个漫想事件。调用方需要先持有 _event_lock。"""
        logger.info("create_event 开始执行")

        # 获取空闲时长（事件处理 / 后续推送生成都需要）
        idle_seconds = 0.0
        if self._get_idle_seconds:
            try:
                idle_seconds = self._get_idle_seconds()
            except Exception:
                pass

        # 选择事件类型（滑动窗口 4 级因子自动抑制重复，无需 re-roll）
        event_type = self._select_event_type()
        logger.info(f"选中事件类型: {event_type.value}")

        # 创建事件，携带空闲时长信息
        event = WanderEvent(
            event_type=event_type,
            timestamp=datetime.now(),
            details={"idle_seconds": idle_seconds}
        )

        # 为 MEMORY_FETCH 事件从当前话题提取搜索上下文
        if event_type == EventType.MEMORY_FETCH and self._get_recent_user_messages:
            try:
                # 多取几条：最后1条做关键词，全部拼成话题上下文做 prompt 润滑
                recent = await self._get_recent_user_messages(limit=8)
                if recent:
                    event.details["keyword"] = recent[-1][:200]
                    topic_context = " ".join(recent)
                    if topic_context:
                        event.details["topic_context"] = topic_context[:500]
            except Exception:
                pass

        # 使用事件处理器处理事件
        handler = self._handler_factory.get_handler(event_type)
        try:
            logger.info(f"开始执行事件处理器: {event_type.value}")
            event = await handler.handle(event)
            logger.info(f"事件处理器执行完成: {event_type.value}")
        except Exception as e:
            logger.error(f"事件处理器执行失败 [{event_type.value}]: {e}")
            logger.error(f"事件处理器执行失败: {e}")
            event.process_log = f"事件执行失败: {str(e)}"
            event.details["error"] = str(e)

        # 更新概率状态：滑动窗口记录 + SLEEP 冷却 + 自省持久化
        self._probability_state.on_event(event_type)

        # 触发回调
        if self._on_event_created:
            try:
                self._on_event_created(event)
            except Exception as e:
                logger.error(f"事件创建回调执行失败: {e}")

        print(f"[WanderCreator] create_event 完成: {event.event_type.value}")
        return event

    # ==================== 后台任务管理 ====================

    async def _create_loop(self):
        """后台事件创建循环"""
        logger.info(f"漫想创建循环启动，事件间隔: {self.event_interval}秒")

        while self._running:
            try:
                if self._runtime_v3_enabled:
                    runner = self._ensure_runtime_runner()
                    result = await runner.tick()
                    logger.info("v3 漫想推进: action=%s, delay=%.1fs", result.action, result.delay_seconds)
                    await runner.wait(result.delay_seconds)
                    continue
                elif self._activity_engine_enabled:
                    # 「全自主区间式行动」v2：计划 → 持续活动会话
                    logger.info("开始一轮漫想计划...")
                    plan = await self._activity_engine.cycle()
                    logger.info(f"漫想轮次完成: {plan is not None}")
                else:
                    # 旧节拍式：抽原子事件（默认，涨价期间冻结 v2）
                    event = await self.create_event()
                    logger.info(f"漫想事件已创建: {event.event_type.value}")

                # 等待下一次
                logger.info(f"等待 {self.event_interval} 秒后进行下一轮")
                await asyncio.sleep(self.event_interval)

            except asyncio.CancelledError:
                logger.info("漫想创建循环被取消")
                print("[WanderCreator] 漫想创建循环被取消", flush=True)
                break
            except Exception as e:
                logger.error(f"漫想创建循环异常: {e}")
                print(f"[WanderCreator] 漫想创建循环异常: {e}", flush=True)
                import traceback
                traceback.print_exc()
                await asyncio.sleep(60)  # 异常后等待1分钟再重试

    async def start(self):
        """启动漫想创建（每次漫想模式激活时重置动态概率状态）"""
        if self._running:
            if self._create_task and not self._create_task.done():
                logger.info("漫想创建已在运行中，忽略重复启动")
                return
            else:
                logger.warning("漫想创建任务已结束，重新启动")
                self._running = False

        # 每次漫想模式开启时，重置动态概率调整状态
        self._probability_state.reset()
        logger.info("漫想模式激活，动态概率状态已初始化")

        self._running = True
        if self._runtime_v3_enabled:
            self._ensure_runtime_runner().initialize()
        self._create_task = asyncio.create_task(self._create_loop())
        logger.info("漫想创建已启动")

    async def stop(self):
        """停止漫想创建"""
        self._running = False
        if self._runtime_runner is not None:
            self._runtime_runner.wake()

        task_to_stop = self._create_task
        self._create_task = None  # 先清除引用，防止并发 start() 创建的新任务在 await 后被覆盖孤立
        if task_to_stop:
            task_to_stop.cancel()
            try:
                await asyncio.wait_for(task_to_stop, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        logger.info("漫想创建已停止")

    @property
    def is_running(self) -> bool:
        """创建循环是否仍在运行。"""
        return self._running and self._create_task is not None and not self._create_task.done()

    def get_probability_status(self) -> dict:
        """
        获取概率状态（供调试/显示，含 location 信息）。
        """
        try:
            from .user_status import get_status_meta as _gsm
            status_meta = _gsm()
        except Exception:
            status_meta = None
        user_status = get_user_status()
        idle_seconds = 0.0
        if self._get_idle_seconds:
            try:
                idle_seconds = self._get_idle_seconds()
            except Exception:
                pass
        probs = self._probability_state.get_probabilities_rich(
            status_meta=status_meta, user_status=user_status, idle_seconds=idle_seconds
        )
        # 滑动窗口摘要
        recent = [e.value for e in self._probability_state._recent_events]
        label = status_meta.display_label if status_meta else user_status.value
        return {
            "probabilities": {e.value: f"{p:.2%}" for e, p in probs.items()},
            "user_status": user_status.value,
            "user_status_label": label,
            "self_reflection_bonus": f"{self._probability_state.self_reflection_bonus:.2%}",
            "events_since_self_reflection": self._probability_state.events_since_self_reflection,
            "recent_events": recent,
            "sleep_cooldown": self._probability_state._sleep_cooldown,
        }


# 全局单例
_wander_creator_instance: Optional["WanderCreator"] = None


def get_wander_creator() -> WanderCreator:
    """获取全局漫想创建器实例"""
    global _wander_creator_instance
    if _wander_creator_instance is None:
        _wander_creator_instance = WanderCreator()
    return _wander_creator_instance


def init_wander_creator(
    event_interval: int = None,
    on_event_created: Optional[Callable[[WanderEvent], None]] = None,
    call_llm_func: Optional[Callable] = None,
    pro_llm_func: Optional[Callable] = None,
    share_llm_func: Optional[Callable] = None,
    get_memories_func: Optional[Callable] = None,
    tracking_service: Optional[Any] = None,
    web_search_func: Optional[Callable] = None,
    get_recent_user_messages_func: Optional[Callable] = None,
    get_idle_seconds_func: Optional[Callable] = None,
    song_cache: Optional[Any] = None,
    k_self_book: Optional[Any] = None,
    music_mcp_client: Optional[Any] = None,
    on_push_to_user: Optional[Callable] = None,
    on_notification: Optional[Callable] = None,
    activity_engine_enabled: bool = False,
    runtime_v3_enabled: bool = True,
) -> WanderCreator:
    """初始化全局漫想创建器实例"""
    global _wander_creator_instance
    _wander_creator_instance = WanderCreator(
        event_interval=event_interval,
        on_event_created=on_event_created,
        call_llm_func=call_llm_func,
        pro_llm_func=pro_llm_func,
        share_llm_func=share_llm_func,
        get_memories_func=get_memories_func,
        tracking_service=tracking_service,
        web_search_func=web_search_func,
        get_recent_user_messages_func=get_recent_user_messages_func,
        get_idle_seconds_func=get_idle_seconds_func,
        song_cache=song_cache,
        k_self_book=k_self_book,
        music_mcp_client=music_mcp_client,
        on_push_to_user=on_push_to_user,
        on_notification=on_notification,
        activity_engine_enabled=activity_engine_enabled,
        runtime_v3_enabled=runtime_v3_enabled,
    )
    return _wander_creator_instance
