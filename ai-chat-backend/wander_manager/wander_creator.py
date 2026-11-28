from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
import asyncio
import logging
import os
from .event_types import EventType
from .event_handlers import EventHandlerFactory
from .runtime_state_readings import RuntimeStateReadings
logger = logging.getLogger(__name__)

def _format_elapsed(seconds: Optional[float]) -> str:
    """Render an elapsed duration without assigning it an incorrect meaning."""
    if seconds is None:
        return '未知'
    try:
        total_minutes = max(0, int(float(seconds) / 60))
    except (TypeError, ValueError):
        return '未知'
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f'{hours}小时{minutes}分钟'
    if hours:
        return f'{hours}小时'
    return f'{minutes}分钟'

def _status_duration_text() -> str:
    """Elapsed time since the current explicit/automatic status transition."""
    try:
        from .user_status import get_status_history
        history = get_status_history(transitions_only=True) or []
        if not history:
            return '未知'
        stamp = history[-1].get('time')
        when = datetime.fromisoformat(str(stamp))
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return _format_elapsed((datetime.now() - when).total_seconds())
    except Exception:
        return '未知'

def _private_message_idle_text() -> str:
    """Elapsed time since the last private message, not physical absence."""
    try:
        from shared_state import get_last_private_chat_time
        last = get_last_private_chat_time(resolve_from_store=True)
        if last is None:
            return '未知'
        if last.tzinfo is not None:
            last = last.astimezone().replace(tzinfo=None)
        return _format_elapsed((datetime.now() - last).total_seconds())
    except Exception:
        return '未知'

def _short_clock(value: Any) -> str:
    """HH:MM from a runtime timestamp; omit it when the value is unknown."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).strftime('%H:%M')
        when = datetime.fromisoformat(str(value))
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return when.strftime('%H:%M')
    except (TypeError, ValueError, OSError):
        return ''

def _format_today_activity_facts(activities: Optional[List[Dict[str, Any]]]) -> str:
    """Recent local runtime outcomes, stated as historical facts only."""
    if not activities:
        return ''
    try:
        from .event_catalog import strategy_for
    except Exception:
        strategy_for = None
    parts = []
    for item in activities[-6:]:
        try:
            label = strategy_for(EventType(item.get('activity_type'))).display_name
        except Exception:
            label = str(item.get('activity_type') or '未知活动')
        state = {'completed': '完成', 'interrupted': '中断', 'aborted': '中止'}.get(
            str(item.get('state') or ''), str(item.get('state') or '')
        )
        clock = _short_clock(item.get('created_at'))
        parts.append(f'{clock} {label}（{state}）'.strip())
    return '今天真实做过的漫想活动（最近）：' + '；'.join(parts) + '。'

class WanderCreator:
    """Drive the persistent Wander runtime's start, waits, and stop."""
    DEFAULT_EVENT_INTERVAL = 5 * 60

    def __init__(self, event_interval: int=None, call_llm_func: Optional[Callable]=None, pro_llm_func: Optional[Callable]=None, share_llm_func: Optional[Callable]=None, get_memories_func: Optional[Callable]=None, tracking_service: Optional[Any]=None, web_search_func: Optional[Callable]=None, get_idle_seconds_func: Optional[Callable]=None, song_cache: Optional[Any]=None, k_self_book: Optional[Any]=None, music_mcp_client: Optional[Any]=None, on_push_to_user: Optional[Callable]=None, on_activity_event: Optional[Callable]=None, on_notification: Optional[Callable]=None, song_selection_llm_func: Optional[Callable]=None):
        """
        初始化漫想创建层

        Args:
            event_interval: 事件创建间隔（秒）
            call_llm_func: LLM调用函数
            get_memories_func: 获取记忆的函数（用于记忆抓取事件）
            tracking_service: 用户追踪服务实例
            web_search_func: 网页搜索异步函数（用于看新闻事件）
            get_idle_seconds_func: 获取用户空闲秒数的函数
            song_cache: 音乐缓存实例（用于听歌事件）
            k_self_book: AI 自我书实例（用于听歌事件品味记录）
            music_mcp_client: 音乐 MCP 客户端（用于听歌事件播放）
        """
        self.event_interval = event_interval or self.DEFAULT_EVENT_INTERVAL
        self._share_llm = share_llm_func or pro_llm_func
        self._on_push_to_user = on_push_to_user
        self._on_activity_event = on_activity_event
        self._on_notification = on_notification
        self._readings = RuntimeStateReadings(get_idle_seconds_func)
        self._create_task: Optional[asyncio.Task] = None
        self._running = False
        self._handler_factory = EventHandlerFactory(call_llm_func=call_llm_func, get_memories_func=get_memories_func, tracking_service=tracking_service, web_search_func=web_search_func, song_cache=song_cache, k_self_book=k_self_book, music_mcp_client=music_mcp_client, on_notification=on_notification, song_selection_llm_func=song_selection_llm_func)
        self._runtime_runner = None

    def _ensure_runtime_runner(self):
        """Create the persistent runner on first use."""
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
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'events', 'wander_runtime.db')
        store = WanderRuntimeStore(db_path)
        controller = WanderRuntimeController(store)
        allowed = {EventType.KEYWORD_EXPANSION, EventType.MEMORY_FETCH, EventType.BROWSE_NEWS, EventType.BROWSE_XIAOHONGSHU, EventType.PUBLISH_XIAOHONGSHU, EventType.XHS_DIRECT_MESSAGES, EventType.CHECK_XIAOHONGSHU_INTERACTIONS, EventType.BROWSE_SOCIAL_FEED, EventType.WORKSHOP_CREATE, EventType.WORKSHOP_HISTORY, EventType.WORKSHOP_CONTINUE, EventType.BROWSE_BOOKMARKS, EventType.SELF_REFLECTION, EventType.SLEEP, EventType.USER_TRACKING, EventType.LISTEN_MUSIC, EventType.PLAY_GAME}

        def music_available() -> bool:
            music_client = self._handler_factory.music_mcp_client
            return bool(music_client and music_client.is_running())
        from .cedartoy_client import configured as games_available
        from workshop.autonomy import available as workshop_available
        from workshop.wander_entries import available as workshop_entry_available
        share_adapter = None
        if self._share_llm and self._on_push_to_user:
            from behavior_scheduler.band_touch_tool import BandTouchTool
            share_adapter = RuntimeShareAdapter(store, self._share_llm, self._on_push_to_user, band_touch_tool=BandTouchTool(), schedule_checkup_enabled=True, activity_event_callback=self._on_activity_event, expression_enabled=True)
        from .checkup_execution import WanderCheckupExecutor
        self._runtime_runner = WanderRuntimeRunner(store=store, controller=controller, planner=PlanDecisionAdapter(store, allowed_event_types=allowed, max_activities=1, availability_checks={EventType.LISTEN_MUSIC: music_available, EventType.PLAY_GAME: games_available, EventType.VISIT_WORKSHOP: workshop_available, EventType.WORKSHOP_CREATE: lambda: workshop_entry_available('workshop_create'), EventType.WORKSHOP_HISTORY: lambda: workshop_entry_available('workshop_history'), EventType.WORKSHOP_CONTINUE: lambda: workshop_entry_available('workshop_continue')}), executor=NodeExecutionAdapter(store, self._handler_factory, get_idle_seconds=self._readings.idle_seconds, game_context_provider=self._runtime_context_bundle, checkup_executor=WanderCheckupExecutor(store)), reviewer=NodeReviewAdapter(store), settlement=SettlementAdapter(store), self_reflection=SelfReflectionAdapter(store), wish_commit=WishCommitAdapter(store, get_wish_store()), share_adapter=share_adapter, context_provider=self._runtime_context_bundle, on_notification=self._on_notification, default_interval_seconds=self.event_interval)
        return self._runtime_runner

    def _v3_today_counts(self) -> Dict[str, int]:
        """当前主会话活跃日已完成、有执行凭据的活动计数。"""
        try:
            runner = self._runtime_runner
            if runner is None:
                return {}
            from .event_catalog import activity_counts
            from shared_state import get_active_session_id
            from conversation_manager_v2 import resolve_active_date
            session_id = get_active_session_id()
            return activity_counts(runner.store.list_completed_activities_for_active_day(session_id, resolve_active_date(session_id)))
        except Exception:
            return {}

    def _v3_today_wander(self, counts: Optional[Dict[str, int]]=None) -> str:
        """v3 运行时今天已完成的漫想活动文本，供 planner 感知频率。"""
        try:
            if counts is None:
                counts = self._v3_today_counts()
            if not counts:
                return ''
            summary = '、'.join((f'{label} {n} 次' for label, n in sorted(counts.items(), key=lambda x: -x[1])))
            return f'当前活跃日已完成：{summary}。'
        except Exception:
            return ''

    def _v3_today_activities(self) -> List[Dict[str, Any]]:
        """Terminal activities attributed to the current local active day."""
        try:
            runner = self._runtime_runner
            if runner is None:
                return []
            from shared_state import get_active_session_id
            from conversation_manager_v2 import resolve_active_date
            session_id = get_active_session_id()
            return runner.store.list_today_activities(
                session_id, resolve_active_date(session_id)
            )
        except Exception:
            return []

    async def _runtime_context_bundle(self):
        from shared_state import get_active_session_id, get_latest_persona_prompt
        from .plan_decision_adapter import RuntimeContext
        from .runtime_decision_adapters import DecisionContext
        from .runtime_runner import RuntimeContextBundle
        from .self_reflection_adapter import SelfReflectionContext
        from .user_status import get_user_status_context
        persona = get_latest_persona_prompt() or ''
        session_id = get_active_session_id() or ''
        mood = self._readings.mood()
        user_status = get_user_status_context() or ''
        away = self._readings.away_context()
        physical_idle = _format_elapsed(self._readings.physical_idle_seconds())
        status_duration = _status_duration_text()
        private_message_idle = _private_message_idle_text()
        today_activities = self._v3_today_activities()
        today_counts = self._v3_today_counts()
        today_wander = self._v3_today_wander(today_counts)
        today_facts = _format_today_activity_facts(today_activities)
        decision = DecisionContext(persona=persona, session_id=session_id, mood=mood, user_status=user_status, away_duration=away, status_duration=status_duration, private_message_idle=private_message_idle, physical_idle=physical_idle, today_wander=today_wander)
        try:
            from .wish_history import get_wish_context_for_reflection
            wish_context = get_wish_context_for_reflection()
        except Exception:
            wish_context = ''
        try:
            from .body_knowledge import body_overview
            capability_catalog = body_overview()
        except Exception:
            capability_catalog = '本次未取得现役身体事实。'
        return RuntimeContextBundle(plan=RuntimeContext(persona=persona, session_id=session_id, mood=mood, user_status=user_status, away_duration=away, status_duration=status_duration, private_message_idle=private_message_idle, physical_idle=physical_idle, today_wander=today_wander, today_facts=today_facts, today_counts=today_counts), decision=decision, self_reflection=SelfReflectionContext(persona=persona, session_id=session_id, user_status=user_status, trigger_reason='自主倾向', capability_catalog=capability_catalog, wish_context=wish_context))

    async def _create_loop(self):
        """后台事件创建循环"""
        logger.info(f'漫想创建循环启动，事件间隔: {self.event_interval}秒')
        while self._running:
            try:
                runner = self._ensure_runtime_runner()
                result = await runner.tick()
                logger.info('漫想推进: action=%s, delay=%.1fs', result.action, result.delay_seconds)
                await runner.wait(result.delay_seconds)
            except asyncio.CancelledError:
                logger.info('漫想创建循环被取消')
                print('[WanderCreator] 漫想创建循环被取消', flush=True)
                break
            except Exception as e:
                logger.error(f'漫想创建循环异常: {e}')
                print(f'[WanderCreator] 漫想创建循环异常: {e}', flush=True)
                import traceback
                traceback.print_exc()
                await asyncio.sleep(60)

    async def start(self):
        """Initialize the runtime and start its scheduling loop."""
        if self._running:
            if self._create_task and (not self._create_task.done()):
                logger.info('漫想创建已在运行中，忽略重复启动')
                return
            else:
                logger.warning('漫想创建任务已结束，重新启动')
                self._running = False
        self._running = True
        self._ensure_runtime_runner().initialize()
        self._create_task = asyncio.create_task(self._create_loop())
        logger.info('漫想创建已启动')

    async def stop(self):
        """停止漫想创建"""
        self._running = False
        if self._runtime_runner is not None:
            self._runtime_runner.wake()
        task_to_stop = self._create_task
        self._create_task = None
        if task_to_stop:
            task_to_stop.cancel()
            try:
                await asyncio.wait_for(task_to_stop, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info('漫想创建已停止')

    @property
    def is_running(self) -> bool:
        """创建循环是否仍在运行。"""
        return self._running and self._create_task is not None and (not self._create_task.done())

_wander_creator_instance: Optional['WanderCreator'] = None

def get_wander_creator() -> WanderCreator:
    """获取全局漫想创建器实例"""
    global _wander_creator_instance
    if _wander_creator_instance is None:
        _wander_creator_instance = WanderCreator()
    return _wander_creator_instance

def init_wander_creator(event_interval: int=None, call_llm_func: Optional[Callable]=None, pro_llm_func: Optional[Callable]=None, share_llm_func: Optional[Callable]=None, get_memories_func: Optional[Callable]=None, tracking_service: Optional[Any]=None, web_search_func: Optional[Callable]=None, get_idle_seconds_func: Optional[Callable]=None, song_cache: Optional[Any]=None, k_self_book: Optional[Any]=None, music_mcp_client: Optional[Any]=None, on_push_to_user: Optional[Callable]=None, on_activity_event: Optional[Callable]=None, on_notification: Optional[Callable]=None, song_selection_llm_func: Optional[Callable]=None) -> WanderCreator:
    """初始化全局漫想创建器实例"""
    global _wander_creator_instance
    _wander_creator_instance = WanderCreator(event_interval=event_interval, call_llm_func=call_llm_func, pro_llm_func=pro_llm_func, share_llm_func=share_llm_func, get_memories_func=get_memories_func, tracking_service=tracking_service, web_search_func=web_search_func, get_idle_seconds_func=get_idle_seconds_func, song_cache=song_cache, k_self_book=k_self_book, music_mcp_client=music_mcp_client, on_push_to_user=on_push_to_user, on_activity_event=on_activity_event, on_notification=on_notification, song_selection_llm_func=song_selection_llm_func)
    return _wander_creator_instance
