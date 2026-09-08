# 思维漫想管理器协调器
#
# 功能：
# 1. 整合模式开关层、漫想创建层、漫想日志层、主动打扰判断层
# 2. 提供统一的启动/停止接口
# 3. 提供前端状态接口
# 4. 处理用户回复事件
# 5. Phase 2: 支持小红书、QQ空间事件
# 6. Phase 3: 支持用户追踪事件
# 7. Phase 4: 支持配置管理和日志持久化

from datetime import datetime
from typing import Optional, Callable, Dict, Any
import asyncio
from .host_hooks import ordinary
import inspect
import logging

from .mode_switch import ModeSwitch, WanderMode, init_mode_switch, get_mode_switch
from .event_types import WanderEvent, EventType
from .wander_creator import WanderCreator, init_wander_creator, get_wander_creator
from .wander_log import WanderLog, WanderLogEntry, init_wander_log, get_wander_log
from .disturb_judgment import DisturbJudgment, JudgmentResult, init_disturb_judgment, get_disturb_judgment
from .message_generator import ProactiveMessageGenerator, init_message_generator, get_message_generator
from .config import WanderConfig, ConfigManager, get_config, init_config
from .user_status import UserStatus, set_user_status, set_user_status_custom_text, set_user_status_combined, get_user_status, STATUS_DISPLAY_NAMES, get_user_status_custom_text, DEFAULT_DESCRIPTIONS, IDLE_FIXED_DESCRIPTION, restore_user_status, get_status_presets, check_wakeup, get_status_meta

logger = logging.getLogger(__name__)


class WanderManager:
    """
    思维漫想管理器 - 协调各层工作

    核心流程：
    1. 模式开关层检测用户空闲 -> 开启漫想模式
    2. 漫想创建层创建事件 -> 漫想日志层记录
    3. 主动打扰判断层判断 -> 决定是否推送
    4. 消息生成器生成消息 -> 推送给用户
    """

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
    ):
        """
        初始化思维漫想管理器

        Args:
            idle_threshold: 用户空闲阈值（秒）
            event_interval: 事件创建间隔（秒）
            push_threshold: 推送阈值
            call_llm_func: LLM调用函数（生成/创意任务）
            disturb_llm_func: 打扰判断 LLM 函数（轻量级分类）
            on_push_to_user: 推送给用户的回调
            on_status_change: 状态变化回调
            get_memories_func: 获取记忆的函数
            tracking_service: 用户追踪服务实例
            web_search_func: 网页搜索异步函数（用于看新闻事件）
            config: 配置对象
            persistence_enabled: 是否启用日志持久化
            get_recent_user_messages_func: 获取最近用户消息的函数
            song_cache: 音乐缓存实例（用于听歌事件）
            k_self_book: AI 自我书实例（用于听歌事件品味记录）
            music_mcp_client: 音乐 MCP 客户端（用于听歌事件播放）
        """
        self._call_llm = call_llm_func
        self._on_push_to_user = on_push_to_user
        self._on_notification = on_notification
        self._on_status_change = on_status_change

        # Phase 4: 初始化配置管理
        self._config_manager = init_config(config=config)
        self._config = self._config_manager.config

        # 使用配置或参数初始化各层
        self._mode_switch = init_mode_switch(
            idle_threshold=idle_threshold or self._config.idle_threshold,
            on_mode_change=self._handle_mode_change
        )

        self._wander_creator = init_wander_creator(
            event_interval=event_interval or self._config.event_interval,
            call_llm_func=call_llm_func,
            pro_llm_func=pro_llm_func,
            share_llm_func=share_llm_func,
            get_memories_func=get_memories_func,
            tracking_service=tracking_service,
            web_search_func=web_search_func,
            on_event_created=self._handle_event_created,
            get_recent_user_messages_func=get_recent_user_messages_func,
            get_idle_seconds_func=self._mode_switch.get_idle_seconds,
            song_cache=song_cache,
            k_self_book=k_self_book,
            music_mcp_client=music_mcp_client,
            on_push_to_user=on_push_to_user,
            on_notification=on_notification,
            activity_engine_enabled=self._config.activity_engine_enabled,
            runtime_v3_enabled=self._config.runtime_v3_enabled,
        )

        self._wander_log = init_wander_log(
            retention_hours=self._config.log_retention_hours,
        )

        self._disturb_judgment = init_disturb_judgment(
            push_threshold=push_threshold or self._config.push_threshold,
            scoring_mode=self._config.scoring_mode,
            score_weight_high_gm=self._config.score_weight_high_gm,
            score_weight_medium_gm=self._config.score_weight_medium_gm,
            score_weight_low_gm=self._config.score_weight_low_gm,
            call_llm_func=call_llm_func,
            lightweight_llm_func=disturb_llm_func,
            on_push_decision=self._handle_push_decision
        )

        # 消息生成器
        self._message_generator = init_message_generator(call_llm_func=call_llm_func)

        # 运行状态
        self._running = False

        # 当前事件和判断结果（用于推送）
        self._current_event: Optional[WanderEvent] = None
        self._current_judgment: Optional[JudgmentResult] = None

        # 跟踪所有正在进行的判断任务，用于用户回复时取消（防止推送滞后）
        self._pending_judgment_tasks: set = set()

        # 存储上一次用户离开的信息（供 context_scheduler 注入首条上下文）
        self._away_info: Optional[Dict[str, Any]] = None
        self._away_pending: bool = False
        self._away_retracted: bool = False
        self._away_cycle: int = 0

        # 活动会话中断快照（用户回来时同步取的「刚刚在做」，供 FULL_CHAT 注入）
        self._activity_interrupt_snapshot: Optional[Dict[str, Any]] = None

        # 恢复持久化的用户状态（防止重启后重置为 IDLE）
        restore_user_status()

        # 注册配置变更回调
        self._config_manager.on_change(self._on_config_change)

    def _handle_mode_change(self, new_mode: WanderMode):
        """处理模式变化。

        只在 OFFLINE ↔ non-OFFLINE 边界启停创建循环。
        IDLE ↔ BRAINSTORMING 是同一次事件内的状态切换（事件开始→BRAINSTORMING，判断完成→IDLE），
        不应重启创建循环——否则每次事件结束都会 duplicate start()。
        """
        print(f"[WANDER_MANAGER] 模式变化: {new_mode.value}", flush=True)
        logger.info(f"模式变化: {new_mode.value}")

        if new_mode == WanderMode.OFFLINE:
            print("[WANDER_MANAGER] 停止漫想创建器...", flush=True)
            asyncio.create_task(self._wander_creator.stop())
        elif not self._wander_creator.is_running:
            print("[WANDER_MANAGER] 启动漫想创建器...", flush=True)
            asyncio.create_task(self._wander_creator.start())

        # 触发状态变化回调
        if self._on_status_change:
            self._on_status_change(self.get_status())

    def _handle_event_created(self, event: WanderEvent):
        """处理事件创建"""
        print(f"[WANDER_MANAGER] 事件创建: {event.event_type.value}, process_log={event.process_log[:100] if event.process_log else '空'}", flush=True)
        logger.info(f"事件创建: {event.event_type.value}")

        # 旧节拍式自省在 handler 阶段直接写入 wish board，没有 v3 runner
        # 可以挂通知；把真实变更也送进同一 generic UI-only 通知回调。
        legacy_changes = event.details.get("wish_changes") if event.details else None
        if event.event_type == EventType.SELF_REFLECTION and legacy_changes and self._on_notification:
            async def _notify_legacy_wish_change():
                try:
                    outcome = self._on_notification("", event.event_id, legacy_changes)
                    if inspect.isawaitable(outcome):
                        await outcome
                except Exception as exc:
                    logger.warning("legacy wish-board notification failed: %s", exc)
            asyncio.create_task(_notify_legacy_wish_change())

        # 休眠事件不改变状态
        if event.event_type != EventType.SLEEP:
            self._mode_switch.set_brainstorming()

        # 检查事件是否有错误
        if event.details.get("error"):
            print(f"[WANDER_MANAGER] 事件包含错误: {event.details['error']}", flush=True)

        # 记录日志
        entry = self._wander_log.add_entry(event)

        # 判断是否推送（跟踪task用于在用户回复时取消）
        # 音乐模式中抑制漫想推送——听歌时不应被闲聊打扰
        try:
            from mirrow_core.shared_state import get_music_mode_active
            if get_music_mode_active():
                print(f"[WANDER_MANAGER] 音乐模式中，跳过漫想事件创建", flush=True)
                logger.info("音乐模式中，跳过漫想事件创建")
                return
        except Exception:
            pass

        print(f"[WANDER_MANAGER] 创建判断任务...", flush=True)
        task = asyncio.create_task(self._judge_and_handle(event, entry))
        self._pending_judgment_tasks.add(task)
        task.add_done_callback(self._pending_judgment_tasks.discard)

    @ordinary
    async def _judge_and_handle(self, event: WanderEvent, entry: WanderLogEntry):
        """判断并处理事件"""
        print(f"[WANDER_MANAGER] 开始判断事件: {event.event_type.value}", flush=True)
        try:
            result = await self._disturb_judgment.judge_and_decide(event)
            print(f"[WANDER_MANAGER] 判断完成: should_push={result.should_push}, score={result.score:.2f}, reason={result.reason}", flush=True)

            # 更新日志
            self._wander_log.update_judgment(entry, result.to_dict(), result.should_push)
        except asyncio.CancelledError:
            print(f"[WANDER_MANAGER] 判断任务被取消", flush=True)
        except Exception as e:
            print(f"[WANDER_MANAGER] 判断处理异常: {e}", flush=True)
            import traceback
            traceback.print_exc()
            logger.error(f"判断处理异常: {e}")
        finally:
            # 事件结束后恢复发呆状态（非休眠事件）
            if event.event_type != EventType.SLEEP:
                self._mode_switch.set_idle()

    @ordinary
    async def _handle_push_decision(self, result: JudgmentResult, event: WanderEvent):
        """处理推送决策"""
        if not result.should_push:
            print(f"[WANDER_MANAGER] 判断结果不推送，跳过消息生成", flush=True)
            return

        print(f"[WANDER_MANAGER] 推送决策通过，开始生成消息...", flush=True)
        logger.info(f"推送事件: {event.event_type.value}")

        # 生成消息（返回 (message, reasoning) 元组，reasoning 为 Flash 思考链）
        gen_result = await self._message_generator.generate_message(event, result)
        message, reasoning = gen_result if gen_result else (None, "")
        print(f"[WANDER_MANAGER] 消息生成完成: {message[:80] if message else 'None'}", flush=True)

        # 提取工具调用记录（用于前端显示）
        tool_calls = self._extract_tool_calls_from_event(event)

        # XHS comment handoff is the only wander result that carries a chat
        # image.  Keep the image metadata and draft separate from the LLM
        # prompt, and forward it only when the handler actually produced a
        # draft-backed attachment.
        images = []
        comment_delivery = event.details.get("comment_delivery") if isinstance(event.details, dict) else None
        if isinstance(comment_delivery, dict) and comment_delivery.get("status") == "ready_for_user":
            image = comment_delivery.get("image")
            if isinstance(image, dict):
                images.append(image)

        if message and self._on_push_to_user:
            try:
                print(f"[WANDER_MANAGER] 调用 on_push_to_user 推送消息...", flush=True)
                payload = {"message": message, "tool_calls": tool_calls, "event_type": event.event_type.value, "event_id": event.event_id, "reasoning": reasoning}
                if images:
                    payload["images"] = images
                # Pass bucket info for write-back on MEMORY_FETCH / BROWSE_BOOKMARKS
                memory_id = event.details.get("memory_id", "")
                if memory_id:
                    payload["memory_id"] = memory_id
                    payload["memory_topic"] = event.details.get("memory_topic", "")
                    payload["memory_reflection"] = event.details.get("reflection", "")[:80]
                if asyncio.iscoroutinefunction(self._on_push_to_user):
                    await self._on_push_to_user(payload)
                else:
                    self._on_push_to_user(payload)
                print(f"[WANDER_MANAGER] 消息推送成功!", flush=True)
                logger.info(f"消息已推送: {message[:50]}...")
            except Exception as e:
                print(f"[WANDER_MANAGER] 推送消息失败: {e}", flush=True)
                import traceback
                traceback.print_exc()
                logger.error(f"推送消息失败: {e}")
        else:
            print(f"[WANDER_MANAGER] 消息为空或无推送回调，推送终止", flush=True)

    def _extract_tool_calls_from_event(self, event: WanderEvent) -> list:
        """从事件中提取工具调用记录（用于前端显示，不进LLM上下文）"""
        tool_calls = []
        if event.event_type == EventType.USER_TRACKING:
            tool_calls.append({
                "tool": "屏幕截取",
                "description": "AI正在截取屏幕画面",
                "result": "截图完成"
            })
            tool_calls.append({
                "tool": "GLM-4V场景分析",
                "description": "AI正在仔细看截屏内容",
                "result": event.details.get("activity_description", f"活跃度: {event.details.get('confidence', 'N/A')}")
            })
        elif event.event_type == EventType.KEYWORD_EXPANSION:
            keyword = event.details.get("keyword", "未知")
            expansion = event.details.get("expansion", "")
            tool_calls.append({
                "tool": "关键词联想",
                "description": f"从近期对话提取关键词「{keyword}」展开联想",
                "result": expansion[:500] if expansion else "完成"
            })
        elif event.event_type == EventType.MEMORY_FETCH:
            memory_topic = event.details.get("memory_topic", "未知主题")
            memory_id = event.details.get("memory_id", "")
            reflection = event.details.get("reflection", "")
            found = event.details.get("found", False)
            if found:
                parts = [f"主题: {memory_topic}"]
                if memory_id:
                    parts.append(f"来源: {memory_id}")
                if reflection:
                    parts.append(f"回忆: {reflection[:300]}")
                result_text = "\n".join(parts)
            else:
                result_text = f"未找到相关记忆 (搜索词: {memory_topic or '随机'})"
            tool_calls.append({
                "tool": "记忆检索",
                "description": "从长期记忆中检索相关片段",
                "result": result_text
            })
        elif event.event_type == EventType.BROWSE_NEWS:
            query = event.details.get("search_query", "未知")
            status = event.details.get("status", "")
            search_results = event.details.get("search_results", "")
            if isinstance(search_results, str) and len(search_results) > 500:
                search_results = search_results[:500] + "..."
            result_text = f"搜索「{query}」\n{search_results}" if status == "success" else f"搜索失败: {event.details.get('search_error', '')}"
            tool_calls.append({
                "tool": "网上搜索",
                "description": f"搜索「{query}」",
                "result": result_text
            })
        elif event.event_type == EventType.SELF_REFLECTION:
            capabilities = event.details.get("capabilities", [])
            wishes = event.details.get("wishes", [])
            parts = []
            if capabilities:
                parts.append(f"能力: {', '.join(capabilities[:3])}")
            if wishes:
                wish_strs = [f"{w.get('feature','?')}({w.get('reason','?')})" for w in wishes[:3]]
                parts.append(f"愿望: {', '.join(wish_strs)}")
            elif isinstance(wishes, list) and len(wishes) == 0:
                parts.append("满足现状，无愿望")
            tool_calls.append({
                "tool": "自我审视",
                "description": "审视自己的大脑架构和能力",
                "result": "\n".join(parts) if parts else "自省完成"
            })
        elif event.event_type == EventType.BROWSE_SOCIAL_FEED:
            # The feed visit is one bounded outer affordance.  Its selected
            # action/evidence stays in the private feed domain and is not
            # rendered as a second tool description here.
            tool_calls.append({
                "tool": "💌 打开了朋友圈",
                "description": "打开了朋友圈",
                "result": "",
            })
        elif event.event_type == EventType.BROWSE_BOOKMARKS:
            collected_by = event.details.get("collected_by", "")
            owner = "自己" if collected_by == "k" else "用户"
            count = event.details.get("bookmark_count", 0)
            tool_calls.append({
                "tool": "翻看收藏夹",
                "description": f"翻看{owner}的收藏夹",
                "result": f"找到 {count} 条收藏" if count > 0 else "收藏夹为空"
            })
        elif event.event_type == EventType.HOST_GROUP_ACTIVITY:
            completed = event.details.get("host_activity_completed") is True
            tool_calls.append({
                "tool": "群组活动",
                "description": event.process_log or "宿主群组活动",
                "result": "宿主已确认完成" if completed else "未收到宿主完成确认",
            })
        elif event.event_type == EventType.LISTEN_MUSIC:
            song_name = event.details.get("song_name", "未知")
            artist = event.details.get("artist", "未知")
            from_cache = event.details.get("from_cache", False)
            reason = event.details.get("reason", "")
            reflection = event.details.get("reflection", "")
            source = "缓存歌单" if from_cache else "新搜索"
            tool_calls.append({
                "tool": "💭 自己听歌",
                "description": f"AI 从{source}选了《{song_name}——{artist}》来听",
                "result": f"选歌理由: {reason}\n听后感: {reflection[:200]}" if reflection else reason
            })
        elif event.event_type == EventType.BROWSE_XIAOHONGSHU:
            delivery = event.details.get("comment_delivery") if isinstance(event.details, dict) else None
            if isinstance(delivery, dict) and delivery.get("status") == "ready_for_user":
                draft = str(delivery.get("comment_draft") or "").strip()[:2000]
                tool_calls.append({
                    "tool": "小红书评论交接",
                    "description": "仅交给用户人工查看，不执行评论或发布",
                    "result": draft or "已准备首页截图，未生成评论草稿",
                })
            elif isinstance(delivery, dict) and delivery.get("status"):
                tool_calls.append({
                    "tool": "小红书评论交接",
                    "description": "未执行评论或发布",
                    "result": f"交接未完成：{str(delivery.get('status'))[:100]}",
                })
        return tool_calls

    def _on_config_change(self, changed_keys: list):
        """处理配置变更"""
        for key in changed_keys:
            value = getattr(self._config, key, None)

            if key == "idle_threshold" and value:
                self._mode_switch.idle_threshold = value

            elif key == "event_interval" and value:
                self._wander_creator.event_interval = value

            elif key == "activity_engine_enabled":
                self._wander_creator._activity_engine_enabled = bool(value)

            elif key == "runtime_v3_enabled":
                self._wander_creator._runtime_v3_enabled = bool(value)
                # The old engine may be in asyncio.sleep while v3 is enabled,
                # or v3 may be waiting on its own due event while disabled.
                # Restart the single creator task so the switch takes effect
                # immediately without ever running both engines together.
                if self._wander_creator.is_running:
                    try:
                        asyncio.get_running_loop().create_task(
                            self._restart_creator_after_runtime_switch()
                        )
                    except RuntimeError:
                        logger.warning("runtime v3 开关已更新，将在下次启动时生效")
                logger.info(f"持久化漫想运行时开关切换: {value}")

            elif key == "push_threshold" and value is not None:
                self._disturb_judgment.push_threshold = value

            elif key == "scoring_mode" and value:
                self._disturb_judgment.scoring_mode = value

            elif key == "score_weight_high_gm" and value is not None:
                self._disturb_judgment.SCORE_WEIGHTS_GM["high"] = value

            elif key == "score_weight_medium_gm" and value is not None:
                self._disturb_judgment.SCORE_WEIGHTS_GM["medium"] = value

            elif key == "score_weight_low_gm" and value is not None:
                self._disturb_judgment.SCORE_WEIGHTS_GM["low"] = value

            logger.info(f"配置已应用: {key} = {value}")

    async def _restart_creator_after_runtime_switch(self) -> None:
        await self._wander_creator.stop()
        if self._mode_switch.is_wander_mode_active:
            await self._wander_creator.start()

    def on_user_reply(self, timestamp: Optional[datetime] = None, capture_away: bool = True):
        """
        用户回复事件处理

        Args:
            timestamp: 用户回复时间
            capture_away: 是否捕获离开信息（启动恢复时应传 False）
        """
        # 在重置计时器前捕获离开信息（供上下文调度器注入首条消息）
        away_captured = False
        if capture_away and self._mode_switch.is_wander_mode_active:
            idle_seconds = self._mode_switch.get_idle_seconds()
            if idle_seconds >= self._config.idle_threshold:
                user_status = get_user_status()
                custom_text = get_user_status_custom_text()
                # 优先使用 StatusMeta 层级标签构建离开原因
                try:
                    meta = get_status_meta()
                    label = meta.display_label
                except Exception:
                    label = STATUS_DISPLAY_NAMES.get(user_status, "未知")
                if user_status == UserStatus.IDLE:
                    description = IDLE_FIXED_DESCRIPTION
                elif custom_text:
                    description = custom_text
                else:
                    description = DEFAULT_DESCRIPTIONS.get(user_status, "")
                reason = label + (f"（{description}）" if description else "")
                self._away_cycle += 1
                self._away_info = {
                    'away_duration_seconds': idle_seconds,
                    'away_reason': reason,
                    'cycle_id': self._away_cycle
                }
                self._away_pending = True
                self._away_retracted = False
                away_captured = True
                logger.info(f"用户离开信息已捕获: cycle={self._away_cycle}, {self._away_info}")

        # 清理过期的离开信息
        # 规则：用户回复时，如果本轮没捕获新离开信息，且旧离开信息不是"撤回恢复"的，
        # 则清除（防止旧离开信息泄漏到后续活跃对话中）
        if capture_away and not away_captured and self._away_pending:
            if self._away_retracted:
                # 撤回后重发：保留离开信息，但消耗"免死金牌"，下一轮正常清除
                self._away_retracted = False
                logger.info(f"撤回后重发，保留离开信息（_away_retracted 已复位）: cycle={self._away_cycle}")
            else:
                # 没有撤回保护，且不是新捕获 → 真正的过期数据，清除
                logger.info(f"用户回复时清除过期离开信息: cycle={self._away_cycle}")
                self._away_pending = False
                self._away_info = None

        # 捕获活动会话中断快照（「全自主区间式行动」：用户回来时，同步取「刚刚在做」）。
        # 必须在 cancel 会话任务之前取——cancel 会销毁状态。启动恢复只借此入口
        # 还原历史时间戳（capture_away=False），并不代表用户此刻真的回来，不能
        # 中断数据库里可续跑的活动；但仍要初始化 runner 以修复遗留 planning claim。
        self._activity_interrupt_snapshot = None
        try:
            runner = getattr(self._wander_creator, "_runtime_runner", None)
            if self._config.runtime_v3_enabled:
                if runner is None:
                    runner = self._wander_creator._ensure_runtime_runner()
                    runner.initialize()
                if capture_away:
                    self._activity_interrupt_snapshot = runner.request_interrupt()
            elif capture_away:
                engine = getattr(self._wander_creator, "_activity_engine", None)
                if engine and hasattr(engine, "on_user_interrupt"):
                    self._activity_interrupt_snapshot = engine.on_user_interrupt()
            if self._activity_interrupt_snapshot:
                logger.info(f"活动会话中断快照: {self._activity_interrupt_snapshot}")
        except Exception as e:
            logger.warning(f"捕获活动会话中断快照失败: {e}")

        logger.info("用户回复，关闭漫想模式")
        self._mode_switch.update_user_reply_time(timestamp)

        # 取消所有正在进行的判断任务，防止用户回复后仍有推送
        for task in self._pending_judgment_tasks:
            task.cancel()
        self._pending_judgment_tasks.clear()
        # 同时取消等待中的 gaming 复判任务
        try:
            from .event_handlers import UserTrackingHandler
            UserTrackingHandler.cancel_rejudges()
        except Exception:
            pass

        # 触发状态变化回调
        if self._on_status_change:
            self._on_status_change(self.get_status())

    def get_away_info(self) -> Optional[Dict[str, Any]]:
        """获取当前离开信息（仅在 pending 时返回）

        Returns:
            None 或 {'away_duration_seconds': float, 'away_reason': str, 'cycle_id': int}
        """
        if self._away_info and self._away_pending:
            return self._away_info
        return None

    def get_activity_interrupt_snapshot(self) -> Optional[Dict[str, Any]]:
        """获取活动会话中断快照（供 FULL_CHAT 上下文注入「刚刚在做」）。"""
        return self._activity_interrupt_snapshot

    async def settle_runtime_interrupt(self) -> Optional[Dict[str, Any]]:
        """在主回复构建前完成 v3 用户中断结算，并补全综合感受。"""
        snap = self._activity_interrupt_snapshot
        runner = getattr(self._wander_creator, "_runtime_runner", None)
        if not snap or not self._config.runtime_v3_enabled or runner is None:
            return snap
        try:
            activity = await runner.settle_interrupt(str(snap.get("activity_id") or ""))
            if activity and activity.get("summary"):
                snap["summary"] = activity["summary"]
            if activity:
                snap["settlement_reason"] = activity.get("settlement_reason") or ""
        except Exception:
            logger.exception("v3 用户中断结算失败")
        return snap

    def get_runtime_logs(
        self,
        *,
        limit: int = 50,
        pushed: Optional[bool] = None,
        hours: int = 24,
        date: str = "",
    ) -> Optional[list[dict]]:
        runner = getattr(self._wander_creator, "_runtime_runner", None)
        if not self._config.runtime_v3_enabled or runner is None:
            return None
        return runner.store.list_activity_logs(
            limit=limit, pushed=pushed, hours=hours, date=date
        )

    def get_runtime_log_stats(
        self,
        *,
        pushed: Optional[bool] = None,
        hours: int = 24,
        date: str = "",
        last_viewed_at: str = "",
    ) -> Optional[dict]:
        runner = getattr(self._wander_creator, "_runtime_runner", None)
        if not self._config.runtime_v3_enabled or runner is None:
            return None
        return runner.store.activity_log_stats(
            pushed=pushed, hours=hours, date=date, last_viewed_at=last_viewed_at
        )

    def get_runtime_schedule(self) -> Optional[dict]:
        """Return the persisted next-plan wake as a scheduling fact."""
        runner = getattr(self._wander_creator, "_runtime_runner", None)
        if not self._config.runtime_v3_enabled or runner is None:
            return None
        return runner.store.get_scheduler_wake()

    def get_runtime_status(self) -> dict[str, Any]:
        """Return truthful v3 scheduler health separately from creator health."""
        runner = getattr(self._wander_creator, "_runtime_runner", None)
        if not self._config.runtime_v3_enabled:
            return {
                "runtime_state": "disabled",
                "active_run_id": "",
                "active_run_state": "",
                "blocked_reason": "runtime_v3_disabled",
                "next_plan_at": "",
                "wake_reason": "",
                "schedule_source_run_id": "",
                "schedule_failure_streak": 0,
            }
        if runner is None:
            return {
                "runtime_state": "not_initialized",
                "active_run_id": "",
                "active_run_state": "",
                "blocked_reason": "runtime_not_initialized",
                "next_plan_at": "",
                "wake_reason": "",
                "schedule_source_run_id": "",
                "schedule_failure_streak": 0,
            }
        try:
            return runner.store.get_runtime_status()
        except Exception:
            logger.exception("读取 v3 漫想运行时状态失败")
            return {
                "runtime_state": "blocked",
                "active_run_id": "",
                "active_run_state": "",
                "blocked_reason": "runtime_status_unavailable",
                "next_plan_at": "",
                "wake_reason": "",
                "schedule_source_run_id": "",
                "schedule_failure_streak": 0,
            }

    async def browse_xhs_from_chat(
        self,
        *,
        session_id: str,
        intent: str,
        query: str = "",
        count: int = 3,
        target_count: Optional[int] = None,
        comment_draft: str = "",
        source: str = "chat_tool",
    ) -> dict[str, Any]:
        """Wire an explicitly granted chat request into a bounded v3 batch.

        The manager only resolves the configured runtime and delegates the
        bounded execution to :class:`WanderRuntimeRunner`; it does not expose
        the hidden handler as a chat tool and it never performs a proactive
        push for this foreground source.  ``count`` is request-local backend
        state populated by the capability gate, never a model tool argument.
        """
        if str(source or "") != "chat_tool":
            return {"status": "invalid_request", "error": "chat_source_required"}
        if not self._config.runtime_v3_enabled:
            return {"status": "runtime_disabled", "barrier": "wander_runtime_v3_disabled"}
        try:
            runner = self._wander_creator._ensure_runtime_runner()
            runner.initialize()
            runner_kwargs = {
                "session_id": session_id,
                "intent": intent,
                "query": query,
                "count": count,
                "comment_draft": comment_draft,
            }
            if target_count is not None:
                runner_kwargs["target_count"] = target_count
            return await runner.execute_chat_xhs(
                **runner_kwargs,
            )
        except Exception as exc:
            logger.error("chat XHS v3 runtime unavailable: %s", type(exc).__name__)
            return {"status": "runtime_error", "error": type(exc).__name__}

    def get_activity_interrupt_text(self) -> str:
        """把活动中断快照格式化成「刚刚在做」文本（供 FULL_CHAT 上下文注入）。空串表示无活跃会话被打断。"""
        snap = self._activity_interrupt_snapshot
        if not snap:
            return ""
        parts = [f"用户回来前，你正在{snap.get('activity', '')}"]
        if snap.get("progress"):
            parts.append(snap["progress"])
        recent = snap.get("recent_nodes") or []
        if recent:
            parts.append("最近在：" + "、".join(recent))
        elif snap.get("latest"):
            parts.append("最近在：" + str(snap["latest"]))
        if snap.get("summary"):
            parts.append("中断结算后的综合感受：" + str(snap["summary"]))
        return "，".join(parts) + "。"

    def mark_away_delivered(self, cycle_id: int):
        """LLM 回复成功后调用，标记离开信息已消费（除非本轮被撤回）"""
        if cycle_id == self._away_cycle and not self._away_retracted:
            self._away_pending = False
            logger.info(f"离开信息已标记消费: cycle={cycle_id}")

    def retract_away_delivery(self):
        """用户撤回消息后调用，恢复离开信息注入能力"""
        self._away_retracted = True
        self._away_pending = True
        logger.info(f"离开信息已恢复（撤回），cycle={self._away_cycle}")

    async def start(self):
        """启动思维漫想管理器"""
        if self._running:
            logger.warning("思维漫想管理器已在运行中")
            return

        self._running = True

        # 初始化关键词池
        try:
            from .keyword_pool import init_keyword_pool
            init_keyword_pool()
        except Exception as e:
            logger.warning(f"关键词池初始化失败: {e}")

        await self._mode_switch.start()

        # 启动日志持久化
        await self._wander_log.start_persistence()

        logger.info("思维漫想管理器已启动")

    async def stop(self):
        """停止思维漫想管理器"""
        self._running = False
        # 取消所有进行中的判断任务，防止停止后仍有推送
        for task in self._pending_judgment_tasks:
            task.cancel()
        self._pending_judgment_tasks.clear()
        await self._mode_switch.stop()
        await self._wander_creator.stop()
        await self._wander_log.stop_persistence()
        self._config_manager.save_to_file()
        logger.info("思维漫想管理器已停止")

    async def activate_wander_mode(self):
        """手动开启漫想模式，并保证创建循环与模式状态一致。"""
        self._mode_switch._set_mode(WanderMode.IDLE)
        await asyncio.sleep(0)
        if not self._wander_creator.is_running:
            await self._wander_creator.start()

    async def deactivate_wander_mode(self, timestamp: Optional[datetime] = None):
        """手动关闭漫想模式，并等待创建循环真正停止。"""
        self.on_user_reply(timestamp)
        await self._wander_creator.stop()

    async def ensure_consistent_state(self):
        """修正模式状态和后台创建循环不一致的情况。"""
        if self._mode_switch.mode == WanderMode.OFFLINE and self._wander_creator.is_running:
            logger.warning("检测到漫想模式为 offline 但创建循环仍在运行，正在停止创建循环")
            await self._wander_creator.stop()
        elif self._mode_switch.mode != WanderMode.OFFLINE and not self._wander_creator.is_running:
            logger.warning("检测到漫想模式已开启但创建循环未运行，正在启动创建循环")
            await self._wander_creator.start()

    def get_status(self) -> Dict[str, Any]:
        """
        获取当前状态（供前端显示）

        Returns:
            状态字典
        """
        mode_status = self._mode_switch.get_status()
        prob_status = self._wander_creator.get_probability_status()
        log_stats = self._wander_log.get_stats()
        runtime_status = self.get_runtime_status()

        return {
            "mode": mode_status["mode"],
            "mode_display": mode_status["mode_display"],
            "is_wander_mode_active": mode_status["is_wander_mode_active"],
            "idle_seconds": mode_status["idle_seconds"],
            "idle_threshold": mode_status["idle_threshold"],
            "last_user_reply_time": mode_status["last_user_reply_time"],
            "probability_status": prob_status,
            "log_stats": log_stats,
            "config": self._config.to_dict(),
            "creator_running": self._wander_creator.is_running,
            **runtime_status,
            "user_status": get_user_status().value if get_user_status() else "idle",
            "user_status_display": STATUS_DISPLAY_NAMES.get(get_user_status(), "未知"),
            "user_status_custom_text": get_user_status_custom_text(),
            "user_status_presets": get_status_presets(),
            "status_meta": get_status_meta().to_dict() if get_status_meta() else None,
        }

    def get_recent_logs(self, count: int = 10, pushed: bool = None,
                        hours: int = None, date: str = "") -> list:
        """
        获取最近的日志

        Args:
            count: 数量
            pushed: None=全部, True=仅已推送, False=仅已丢弃
            hours: 时间范围（小时），None=全部
            date: 日期过滤（YYYY-MM-DD），优先级高于 hours

        Returns:
            日志列表
        """
        since = None
        if date:
            try:
                since = datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                pass
        elif hours:
            from datetime import timedelta
            since = datetime.now() - timedelta(hours=hours)
        entries = self._wander_log.get_entries(
            since=since, limit=count, pushed_only=pushed)
        return [e.to_dict() for e in entries]

    def set_idle_threshold(self, threshold: int):
        """
        设置空闲阈值（前端可配置）

        Args:
            threshold: 阈值（秒）
        """
        self._config_manager.set("idle_threshold", threshold)
        logger.info(f"空闲阈值已更新: {threshold}秒")

    def set_push_threshold(self, threshold: float):
        """
        设置推送阈值

        Args:
            threshold: 阈值（0-1）
        """
        self._config_manager.set("push_threshold", threshold)
        logger.info(f"推送阈值已更新: {threshold}")

    def get_config(self) -> WanderConfig:
        """获取当前配置"""
        return self._config

    def update_config(self, **kwargs):
        """
        更新配置

        Args:
            **kwargs: 配置项
        """
        self._config_manager.update(**kwargs)

    def save_config(self):
        """保存配置到文件"""
        self._config_manager.save_to_file()

    def set_user_status(self, status: str, custom_text: str = ""):
        """
        设置用户状态（由前端API调用）

        Args:
            status: 状态字符串 (gaming/out/bathing/eating/sleeping/idle/other/coding)
            custom_text: 自定义描述文本（≤20字，IDLE 固定描述不受此影响）
        """
        try:
            user_status = UserStatus(status)
            text = custom_text if user_status != UserStatus.IDLE else ""
            # 获取旧状态（供健康追踪使用）
            old_status_data = get_user_status()
            old_status = old_status_data.value if hasattr(old_status_data, 'value') else str(old_status_data)
            set_user_status_combined(user_status, text)
            logger.info(f"用户状态已更新: {status}, custom_text={custom_text}")
            # 健康追踪
            try:
                from health_tracker.tracker import get_health_tracker
                get_health_tracker().on_status_change(old_status, status, custom_text)
            except Exception:
                pass
            # 广播到 WebSocket
            if self._on_status_change:
                self._on_status_change(self.get_status())
        except ValueError:
            logger.warning(f"无效的用户状态: {status}")


# 全局单例
_wander_manager_instance: Optional[WanderManager] = None


def get_wander_manager() -> WanderManager:
    """获取全局思维漫想管理器实例"""
    global _wander_manager_instance
    if _wander_manager_instance is None:
        _wander_manager_instance = WanderManager()
    return _wander_manager_instance


def init_wander_manager(
    idle_threshold: int = None,
    event_interval: int = None,
    push_threshold: float = None,
    call_llm_func: Optional[Callable] = None,
    pro_llm_func: Optional[Callable] = None,
    share_llm_func: Optional[Callable] = None,
    disturb_llm_func: Optional[Callable] = None,
    on_push_to_user: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_notification: Optional[Callable] = None,
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
) -> WanderManager:
    """初始化全局思维漫想管理器实例"""
    global _wander_manager_instance
    _wander_manager_instance = WanderManager(
        idle_threshold=idle_threshold,
        event_interval=event_interval,
        push_threshold=push_threshold,
        call_llm_func=call_llm_func,
        pro_llm_func=pro_llm_func,
        share_llm_func=share_llm_func,
        disturb_llm_func=disturb_llm_func,
        on_push_to_user=on_push_to_user,
        on_notification=on_notification,
        on_status_change=on_status_change,
        get_memories_func=get_memories_func,
        tracking_service=tracking_service,
        web_search_func=web_search_func,
        config=config,
        persistence_enabled=persistence_enabled,
        get_recent_user_messages_func=get_recent_user_messages_func,
        song_cache=song_cache,
        k_self_book=k_self_book,
        music_mcp_client=music_mcp_client,
    )
    return _wander_manager_instance
