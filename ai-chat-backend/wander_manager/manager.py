"""Public Wander manager for the persistent runtime."""

from typing import Any, Callable, Dict, Optional
import asyncio
import logging

from .config import WanderConfig, init_config
from .interrupt_settlement import InterruptSettlement
from .mode_switch import WanderMode, init_mode_switch
from .user_status import (
    DEFAULT_DESCRIPTIONS, IDLE_FIXED_DESCRIPTION, STATUS_DISPLAY_NAMES,
    UserStatus, get_status_meta, get_status_presets, get_user_status,
    get_user_status_custom_text, restore_user_status, set_user_status_combined,
)
from .wander_creator import init_wander_creator

logger = logging.getLogger(__name__)


class WanderManager:
    """Coordinate the persistent Wander runtime with the idle-mode switch."""

    def __init__(
        self,
        idle_threshold: int = None,
        event_interval: int = None,
        push_threshold: float = None,
        call_llm_func: Optional[Callable] = None,
        pro_llm_func: Optional[Callable] = None,
        share_llm_func: Optional[Callable] = None,
        disturb_llm_func: Optional[Callable] = None,
        on_push_to_user: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_activity_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_status_change: Optional[Callable[[Dict[str, Any]], None]] = None,
        get_memories_func: Optional[Callable] = None,
        tracking_service: Optional[Any] = None,
        web_search_func: Optional[Callable] = None,
        config: Optional[WanderConfig] = None,
        persistence_enabled: bool = True,
        get_recent_user_messages_func: Optional[Callable] = None,
        song_cache: Optional[Any] = None,
        k_self_book: Optional[Any] = None,
        music_mcp_client: Optional[Any] = None,
        on_notification: Optional[Callable] = None,
        song_selection_llm_func: Optional[Callable] = None,
    ):
        # These arguments were accepted by the retired event pipeline. Keep
        # them harmless for existing local deployment launchers.
        del push_threshold, disturb_llm_func, persistence_enabled, get_recent_user_messages_func
        self._on_status_change = on_status_change
        self._config_manager = init_config(config=config)
        self._config = self._config_manager.config
        self._mode_switch = init_mode_switch(
            idle_threshold=idle_threshold or self._config.idle_threshold,
            on_mode_change=self._handle_mode_change,
        )
        self._wander_creator = init_wander_creator(
            event_interval=event_interval or self._config.event_interval,
            call_llm_func=call_llm_func,
            pro_llm_func=pro_llm_func,
            share_llm_func=share_llm_func,
            get_memories_func=get_memories_func,
            tracking_service=tracking_service,
            web_search_func=web_search_func,
            get_idle_seconds_func=self._mode_switch.get_idle_seconds,
            song_cache=song_cache,
            k_self_book=k_self_book,
            music_mcp_client=music_mcp_client,
            on_push_to_user=on_push_to_user,
            on_activity_event=on_activity_event,
            on_notification=on_notification,
            song_selection_llm_func=song_selection_llm_func,
        )
        self._running = False
        self._away_info: Optional[Dict[str, Any]] = None
        self._away_pending = False
        self._away_retracted = False
        self._away_cycle = 0
        self._activity_interrupt_snapshot: Optional[Dict[str, Any]] = None
        self._interrupt_settlement = InterruptSettlement()
        restore_user_status()
        self._config_manager.on_change(self._on_config_change)

    def _handle_mode_change(self, new_mode: WanderMode):
        if new_mode == WanderMode.OFFLINE:
            asyncio.create_task(self._wander_creator.stop())
        elif not self._wander_creator.is_running:
            asyncio.create_task(self._wander_creator.start())
        if self._on_status_change:
            self._on_status_change(self.get_status())

    def _on_config_change(self, changed_keys: list):
        for key in changed_keys:
            value = getattr(self._config, key, None)
            if key == "idle_threshold" and value:
                self._mode_switch.idle_threshold = value
            elif key == "event_interval" and value:
                self._wander_creator.event_interval = value
            logger.info("配置已应用: %s = %s", key, value)

    def on_user_reply(self, timestamp=None, capture_away: bool = True, *,
                      interruption_source: str = "user_message"):
        away_captured = False
        if capture_away and self._mode_switch.is_wander_mode_active:
            idle_seconds = self._mode_switch.get_idle_seconds()
            if idle_seconds >= self._config.idle_threshold:
                user_status = get_user_status()
                custom_text = get_user_status_custom_text()
                try:
                    label = get_status_meta().display_label
                except Exception:
                    label = STATUS_DISPLAY_NAMES.get(user_status, "未知")
                if user_status == UserStatus.IDLE:
                    description = IDLE_FIXED_DESCRIPTION
                elif custom_text:
                    description = custom_text
                else:
                    description = DEFAULT_DESCRIPTIONS.get(user_status, "")
                self._away_cycle += 1
                self._away_info = {
                    "away_duration_seconds": idle_seconds,
                    "away_reason": label + (f"（{description}）" if description else ""),
                    "cycle_id": self._away_cycle,
                }
                self._away_pending = True
                self._away_retracted = False
                away_captured = True

        if capture_away and not away_captured and self._away_pending:
            if self._away_retracted:
                self._away_retracted = False
            else:
                self._away_pending = False
                self._away_info = None

        self._activity_interrupt_snapshot = None
        try:
            runner = self._runtime_runner()
            if runner is None:
                runner = self._wander_creator._ensure_runtime_runner()
                runner.initialize()
            if capture_away:
                self._activity_interrupt_snapshot = runner.request_interrupt(
                    source=interruption_source
                )
        except Exception:
            logger.exception("捕获漫想中断快照失败")
        self._mode_switch.update_user_reply_time(timestamp)
        try:
            from .event_handlers import UserTrackingHandler
            UserTrackingHandler.cancel_rejudges()
        except Exception:
            pass
        if self._on_status_change:
            self._on_status_change(self.get_status())

    def get_away_info(self) -> Optional[Dict[str, Any]]:
        return self._away_info if self._away_pending else None

    def get_activity_interrupt_snapshot(self) -> Optional[Dict[str, Any]]:
        return self._activity_interrupt_snapshot

    async def settle_runtime_interrupt(self) -> Optional[Dict[str, Any]]:
        snapshot = self._activity_interrupt_snapshot
        runner = self._wander_creator._runtime_runner
        if not snapshot or runner is None:
            return snapshot
        return await self._interrupt_settlement.wait(runner, snapshot)

    def _runtime_runner(self):
        return self._wander_creator._runtime_runner

    def get_runtime_logs(self, *, limit: int = 50, pushed: Optional[bool] = None,
                         hours: int = 24, date: str = "") -> Optional[list[dict]]:
        runner = self._runtime_runner()
        return None if runner is None else runner.store.list_activity_logs(
            limit=limit, pushed=pushed, hours=hours, date=date
        )

    def get_runtime_log_stats(self, *, pushed: Optional[bool] = None, hours: int = 24,
                              date: str = "", last_viewed_at: str = "") -> Optional[dict]:
        runner = self._runtime_runner()
        return None if runner is None else runner.store.activity_log_stats(
            pushed=pushed, hours=hours, date=date, last_viewed_at=last_viewed_at
        )

    def get_runtime_schedule(self) -> Optional[dict]:
        runner = self._runtime_runner()
        return None if runner is None else runner.store.get_scheduler_wake()

    def get_runtime_status(self) -> dict[str, Any]:
        runner = self._runtime_runner()
        if runner is None:
            return {"runtime_state": "not_initialized", "active_run_id": "",
                    "active_run_state": "", "blocked_reason": "runtime_not_initialized",
                    "next_plan_at": "", "wake_reason": "", "schedule_source_run_id": "",
                    "schedule_failure_streak": 0}
        try:
            return runner.store.get_runtime_status()
        except Exception:
            logger.exception("读取漫想运行时状态失败")
            return {"runtime_state": "blocked", "active_run_id": "",
                    "active_run_state": "", "blocked_reason": "runtime_status_unavailable",
                    "next_plan_at": "", "wake_reason": "", "schedule_source_run_id": "",
                    "schedule_failure_streak": 0}

    async def browse_social_from_chat(self, *, session_id: str, activity_reason: str = "",
                                      source_turn_id: str = "", initial_request: dict | None = None) -> dict:
        try:
            runner = self._wander_creator._ensure_runtime_runner()
            runner.initialize()
            return await runner.execute_chat_social(
                session_id=session_id, activity_reason=activity_reason,
                source_turn_id=source_turn_id, initial_request=initial_request,
            )
        except Exception as exc:
            logger.error("chat social runtime unavailable: %s", type(exc).__name__)
            return {"status": "runtime_error", "error": type(exc).__name__}

    async def browse_xhs_from_chat(self, *, session_id: str, instruction: str,
                                   source_turn_id: str = "", source: str = "chat_tool") -> dict:
        if source != "chat_tool":
            return {"status": "invalid_request", "error": "chat_source_required"}
        runner = self._wander_creator._ensure_runtime_runner()
        runner.initialize()
        return await runner.execute_chat_xhs(
            session_id=session_id, instruction=instruction, source_turn_id=source_turn_id
        )

    def get_activity_interrupt_text(self) -> str:
        snapshot = self._activity_interrupt_snapshot
        if not snapshot:
            return ""
        parts = [f"人类伙伴回来前，你正在{snapshot.get('activity', '')}"]
        if snapshot.get("progress"):
            parts.append(str(snapshot["progress"]))
        recent = snapshot.get("recent_nodes") or []
        if recent:
            parts.append("最近在：" + "、".join(recent))
        elif snapshot.get("latest"):
            parts.append("最近在：" + str(snapshot["latest"]))
        if snapshot.get("summary"):
            parts.append("中断结算后的综合感受：" + str(snapshot["summary"]))
        return "，".join(parts) + "。"

    def mark_away_delivered(self, cycle_id: int):
        if cycle_id == self._away_cycle and not self._away_retracted:
            self._away_pending = False

    def retract_away_delivery(self):
        self._away_retracted = True
        self._away_pending = True

    async def start(self):
        if self._running:
            return
        self._running = True
        try:
            from .keyword_pool import init_keyword_pool
            init_keyword_pool()
        except Exception:
            logger.exception("漫想关键词池初始化失败")
        runner = self._wander_creator._ensure_runtime_runner()
        runner.initialize()
        if runner.share_adapter is not None:
            from .event_projection_recovery import recover_activity_events
            self._event_projection_task = asyncio.create_task(
                recover_activity_events(runner.share_adapter)
            )
        await self._mode_switch.start()
        from social_feed.mention_watcher import MentionWatcher
        from social_feed.human_inbox import collect
        from social_feed import get_social_feed_store
        from social_feed.remote_sites import get_remote_site_store

        async def pending_mentions():
            return await collect(get_social_feed_store(), get_remote_site_store().list(),
                                 actor="k", limit=100, with_heads=False)

        async def attend_mentions(rows, batch_id):
            if self._mode_switch.get_idle_seconds() < 30:
                return {"status": "busy"}
            return await runner.execute_idle_social_mentions(rows, batch_id)

        self._social_mention_task = asyncio.create_task(
            MentionWatcher(pending_mentions, attend_mentions).run()
        )
        from .xhs_interaction_runtime import InteractionWatcher, available as interactions_available
        if interactions_available():
            self._xhs_interaction_task = asyncio.create_task(
                InteractionWatcher(runner, idle=self._mode_switch.get_idle_seconds).run()
            )
        from .game_invitation import resume_pending
        try:
            resume_pending(self._wander_creator)
        except Exception:
            logger.exception("未能恢复游戏快捷邀请")

    async def stop(self):
        self._running = False
        for name in ("_xhs_interaction_task", "_social_mention_task", "_event_projection_task"):
            task = getattr(self, name, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, name, None)
        await self._mode_switch.stop()
        await self._wander_creator.stop()
        await self._interrupt_settlement.stop()
        from .game_invitation import stop_drivers
        await stop_drivers()
        self._config_manager.save_to_file()

    async def activate_wander_mode(self):
        self._mode_switch._set_mode(WanderMode.IDLE)
        await asyncio.sleep(0)
        if not self._wander_creator.is_running:
            await self._wander_creator.start()

    async def deactivate_wander_mode(self, timestamp=None):
        self.on_user_reply(timestamp, interruption_source="manual_stop")
        await self._wander_creator.stop()

    async def ensure_consistent_state(self):
        if self._mode_switch.mode == WanderMode.OFFLINE and self._wander_creator.is_running:
            await self._wander_creator.stop()
        elif self._mode_switch.mode != WanderMode.OFFLINE and not self._wander_creator.is_running:
            await self._wander_creator.start()

    def get_status(self) -> Dict[str, Any]:
        mode_status = self._mode_switch.get_status()
        return {**mode_status, "config": self._config.to_dict(),
                "creator_running": self._wander_creator.is_running,
                **self.get_runtime_status(),
                "user_status": get_user_status().value if get_user_status() else "idle",
                "user_status_display": STATUS_DISPLAY_NAMES.get(get_user_status(), "未知"),
                "user_status_custom_text": get_user_status_custom_text(),
                "user_status_presets": get_status_presets(),
                "status_meta": get_status_meta().to_dict() if get_status_meta() else None}

    def get_recent_logs(self, count: int = 10, pushed: bool = None, hours: int = None,
                        date: str = "") -> list:
        return self.get_runtime_logs(limit=count, pushed=pushed, hours=hours or 0, date=date) or []

    def set_idle_threshold(self, threshold: int):
        self._config_manager.set("idle_threshold", threshold)

    def get_config(self) -> WanderConfig:
        return self._config

    def update_config(self, **kwargs):
        self._config_manager.update(**kwargs)

    def save_config(self):
        self._config_manager.save_to_file()

    def set_user_status(self, status: str, custom_text: str = "", *, source: str = "explicit"):
        try:
            user_status = UserStatus(status)
        except ValueError:
            logger.warning("无效的用户状态: %s", status)
            return
        set_user_status_combined(user_status, custom_text if user_status != UserStatus.IDLE else "", source=source)
        if self._on_status_change:
            self._on_status_change(self.get_status())


_wander_manager_instance: Optional[WanderManager] = None


def get_wander_manager() -> WanderManager:
    global _wander_manager_instance
    if _wander_manager_instance is None:
        _wander_manager_instance = WanderManager()
    return _wander_manager_instance


def init_wander_manager(**kwargs) -> WanderManager:
    global _wander_manager_instance
    _wander_manager_instance = WanderManager(**kwargs)
    return _wander_manager_instance
