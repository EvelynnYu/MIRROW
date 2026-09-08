# 模式开关层
#
# 功能：
# 1. 管理漫想模式开启/关闭状态
# 2. 记录用户上次回复时间戳
# 3. 每隔10分钟检测用户回复时间是否超过阈值（默认30分钟）
# 4. 超过阈值则开启漫想模式

from datetime import datetime, timedelta
from typing import Optional, Callable
from enum import Enum
import asyncio
import logging
import json
import os

logger = logging.getLogger(__name__)

# 持久化文件路径：用户最后回复时间戳
_LAST_REPLY_TIME_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "last_reply_time.json")


class WanderMode(Enum):
    """漫想模式状态"""
    OFFLINE = "offline"           # 漫想模式关闭，显示"镜影永远为你在线"
    IDLE = "idle"                 # 漫想模式开启，事件间隔期，显示"发呆中"
    BRAINSTORMING = "brainstorming"  # 漫想模式开启，事件执行中，显示"头脑风暴中"


class ModeSwitch:
    """
    模式开关层 - 管理漫想模式的开启与关闭

    核心逻辑：
    - 漫想模式关闭时，每隔10分钟检测用户上次回复时间
    - 若超过阈值（默认30分钟），开启漫想模式
    - 用户回复消息后，关闭漫想模式并重置时间戳
    """

    # 默认配置
    DEFAULT_CHECK_INTERVAL = 1 * 60  # 检测间隔：1分钟（秒）
    DEFAULT_IDLE_THRESHOLD = 10 * 60  # 空闲阈值：10分钟（秒）

    def __init__(
        self,
        idle_threshold: int = None,
        check_interval: int = None,
        on_mode_change: Optional[Callable[[WanderMode], None]] = None
    ):
        """
        初始化模式开关层

        Args:
            idle_threshold: 用户空闲阈值（秒），超过此时间开启漫想模式
            check_interval: 检测间隔（秒）
            on_mode_change: 模式变化时的回调函数
        """
        self.idle_threshold = idle_threshold or self.DEFAULT_IDLE_THRESHOLD
        self.check_interval = check_interval or self.DEFAULT_CHECK_INTERVAL

        # 状态
        self._mode = WanderMode.OFFLINE
        self._last_user_reply_time: datetime = self._restore_last_reply_time()
        self._last_check_time: datetime = datetime.now()

        # 回调
        self._on_mode_change = on_mode_change

        # 后台任务
        self._check_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def mode(self) -> WanderMode:
        """当前漫想模式"""
        return self._mode

    @property
    def last_user_reply_time(self) -> datetime:
        """用户上次回复时间"""
        return self._last_user_reply_time

    @property
    def is_wander_mode_active(self) -> bool:
        """漫想模式是否激活（开启状态）"""
        return self._mode != WanderMode.OFFLINE

    def get_idle_duration(self) -> timedelta:
        """获取用户空闲时长"""
        return datetime.now() - self._to_naive(self._last_user_reply_time)

    def get_idle_seconds(self) -> float:
        """获取用户空闲秒数"""
        return max(0.0, self.get_idle_duration().total_seconds())

    @staticmethod
    def _to_naive(dt: datetime) -> datetime:
        """去除时区信息，统一为 naive datetime，防止与 datetime.now() 运算时报错。"""
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
        return dt

    @staticmethod
    def _restore_last_reply_time() -> datetime:
        """从持久化文件恢复最后回复时间，失败则返回当前时间。"""
        try:
            if os.path.exists(_LAST_REPLY_TIME_FILE):
                with open(_LAST_REPLY_TIME_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ts = data.get("last_user_reply_time", "")
                if ts:
                    return ModeSwitch._to_naive(datetime.fromisoformat(ts))
        except Exception:
            pass
        return datetime.now()

    @staticmethod
    def _save_last_reply_time(timestamp: datetime):
        """持久化最后回复时间到文件。"""
        try:
            os.makedirs(os.path.dirname(_LAST_REPLY_TIME_FILE), exist_ok=True)
            with open(_LAST_REPLY_TIME_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_user_reply_time": timestamp.isoformat()}, f)
        except Exception:
            pass

    def update_user_reply_time(self, timestamp: Optional[datetime] = None):
        """
        更新用户回复时间（用户发送消息时调用）

        Args:
            timestamp: 用户回复时间，默认为当前时间
        """
        dt = timestamp or datetime.now()
        self._last_user_reply_time = self._to_naive(dt)
        self._save_last_reply_time(self._last_user_reply_time)
        logger.info(f"用户回复时间更新: {self._last_user_reply_time}")

        # 用户回复后关闭漫想模式
        if self._mode != WanderMode.OFFLINE:
            self._set_mode(WanderMode.OFFLINE)

    def _set_mode(self, new_mode: WanderMode):
        """
        设置漫想模式（内部方法）

        Args:
            new_mode: 新的模式
        """
        old_mode = self._mode
        if old_mode != new_mode:
            self._mode = new_mode
            logger.info(f"漫想模式变化: {old_mode.value} -> {new_mode.value}")

            # 触发回调
            if self._on_mode_change:
                try:
                    self._on_mode_change(new_mode)
                except Exception as e:
                    logger.error(f"模式变化回调执行失败: {e}")

    def set_brainstorming(self):
        """设置为头脑风暴状态（事件执行中）"""
        if self._mode == WanderMode.IDLE:
            self._set_mode(WanderMode.BRAINSTORMING)

    def set_idle(self):
        """设置为发呆状态（事件间隔期）"""
        if self._mode == WanderMode.BRAINSTORMING:
            self._set_mode(WanderMode.IDLE)

    def _check_and_activate(self) -> bool:
        """
        检测是否应该开启漫想模式

        Returns:
            是否开启漫想模式
        """
        if self._mode != WanderMode.OFFLINE:
            return False

        idle_seconds = self.get_idle_seconds()
        should_activate = idle_seconds >= self.idle_threshold

        if should_activate:
            logger.info(f"用户空闲 {idle_seconds:.0f}秒 >= 阈值 {self.idle_threshold}秒，开启漫想模式")
            self._set_mode(WanderMode.IDLE)
            return True

        return False

    async def _check_loop(self):
        """后台检测循环"""
        logger.info(f"模式开关检测循环启动，检测间隔: {self.check_interval}秒，空闲阈值: {self.idle_threshold}秒")

        # 启动 grace period：首次等待一个周期，避免后端刚重启就因恢复的旧时间戳立即激活漫想
        await asyncio.sleep(self.check_interval)

        while self._running:
            try:
                idle_seconds = self.get_idle_seconds()
                logger.info(f"检测用户空闲时间: {idle_seconds:.0f}秒，阈值: {self.idle_threshold}秒，当前模式: {self._mode.value}")

                # 检测是否应该开启漫想模式
                activated = self._check_and_activate()
                if activated:
                    logger.info("漫想模式已开启！")

                # 等待下一次检测
                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.info("模式开关检测循环被取消")
                break
            except Exception as e:
                logger.error(f"模式开关检测循环异常: {e}")
                await asyncio.sleep(60)  # 异常后等待1分钟再重试

    async def start(self):
        """启动模式开关检测"""
        if self._running:
            logger.warning("模式开关已在运行中")
            return

        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("模式开关已启动")

    async def stop(self):
        """停止模式开关检测"""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await asyncio.wait_for(self._check_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._check_task = None

        logger.info("模式开关已停止")

    def get_status(self) -> dict:
        """
        获取当前状态（供前端显示）

        Returns:
            状态字典
        """
        return {
            "mode": self._mode.value,
            "mode_display": self._get_mode_display(),
            "last_user_reply_time": self._last_user_reply_time.isoformat(),
            "idle_seconds": self.get_idle_seconds(),
            "idle_threshold": self.idle_threshold,
            "is_wander_mode_active": self.is_wander_mode_active
        }

    def _get_mode_display(self) -> str:
        """获取模式显示文本"""
        display_map = {
            WanderMode.OFFLINE: "镜影永远为你在线",
            WanderMode.IDLE: "发呆中",
            WanderMode.BRAINSTORMING: "头脑风暴中"
        }
        return display_map.get(self._mode, "未知状态")


# 全局单例
_mode_switch_instance: Optional[ModeSwitch] = None


def get_mode_switch() -> ModeSwitch:
    """获取全局模式开关实例"""
    global _mode_switch_instance
    if _mode_switch_instance is None:
        _mode_switch_instance = ModeSwitch()
    return _mode_switch_instance


def init_mode_switch(
    idle_threshold: int = None,
    check_interval: int = None,
    on_mode_change: Optional[Callable[[WanderMode], None]] = None
) -> ModeSwitch:
    """初始化全局模式开关实例"""
    global _mode_switch_instance
    _mode_switch_instance = ModeSwitch(
        idle_threshold=idle_threshold,
        check_interval=check_interval,
        on_mode_change=on_mode_change
    )
    return _mode_switch_instance
