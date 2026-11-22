# 漫想管理器配置
#
# 功能：
# 1. 集中管理所有配置项
# 2. 支持从文件加载配置
# 3. 支持运行时修改配置
# 4. 配置持久化

import os
import json
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class WanderConfig:
    """
    漫想管理器配置

    包含所有可配置项及其默认值
    """

    # ==================== 模式开关层配置 ====================
    # 用户空闲阈值（秒）- 超过此时间无回复则开启漫想模式
    idle_threshold: int = 10 * 60  # 默认10分钟

    # ==================== 漫想创建层配置 ====================
    # 事件创建间隔（秒）
    event_interval: int = 15 * 60  # 默认15分钟

    # ==================== 用户追踪配置 ====================
    # 是否保存截图
    save_screenshots: bool = False

    # 截图保存目录
    screenshot_dir: str = "data/tracking_screenshots"

    # ==================== 调试配置 ====================
    # 是否启用调试模式
    debug_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WanderConfig":
        """从字典创建配置"""
        # 只使用已定义的字段
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


class ConfigManager:
    """
    配置管理器

    功能：
    1. 加载/保存配置文件
    2. 运行时修改配置
    3. 配置变更通知
    """

    DEFAULT_CONFIG_FILE = "data/wander_config.json"

    def __init__(
        self,
        config: Optional[WanderConfig] = None,
        config_file: Optional[str] = None
    ):
        """
        初始化配置管理器

        Args:
            config: 初始配置
            config_file: 配置文件路径
        """
        self._config = config or WanderConfig()
        self._config_file = config_file or self.DEFAULT_CONFIG_FILE
        self._change_callbacks: list = []

        # 尝试从文件加载配置
        self._load_from_file()

    def _load_from_file(self):
        """从文件加载配置"""
        if not os.path.exists(self._config_file):
            logger.info(f"配置文件不存在，使用默认配置: {self._config_file}")
            return

        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._config = WanderConfig.from_dict(data)
            logger.info(f"配置已从文件加载: {self._config_file}")

        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")

    def save_to_file(self):
        """保存配置到文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self._config_file), exist_ok=True)

            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(self._config.to_dict(), f, indent=2, ensure_ascii=False)

            logger.info(f"配置已保存到文件: {self._config_file}")

        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    @property
    def config(self) -> WanderConfig:
        """获取当前配置"""
        return self._config

    def update(self, **kwargs):
        """
        更新配置项

        Args:
            **kwargs: 要更新的配置项
        """
        changed_keys = []

        for key, value in kwargs.items():
            if hasattr(self._config, key):
                old_value = getattr(self._config, key)
                if old_value != value:
                    setattr(self._config, key, value)
                    changed_keys.append(key)
                    logger.info(f"配置已更新: {key} = {value}")

        if changed_keys:
            self._notify_change(changed_keys)

    def on_change(self, callback):
        """
        注册配置变更回调

        Args:
            callback: 回调函数，接收变更的键列表
        """
        self._change_callbacks.append(callback)

    def _notify_change(self, changed_keys: list):
        """通知配置变更"""
        for callback in self._change_callbacks:
            try:
                callback(changed_keys)
            except Exception as e:
                logger.error(f"配置变更回调执行失败: {e}")

    def reset_to_default(self):
        """重置为默认配置"""
        self._config = WanderConfig()
        logger.info("配置已重置为默认值")
        self._notify_change(["all"])

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取单个配置项

        Args:
            key: 配置项名称
            default: 默认值

        Returns:
            配置项值
        """
        return getattr(self._config, key, default)

    def set(self, key: str, value: Any):
        """
        设置单个配置项

        Args:
            key: 配置项名称
            value: 新值
        """
        self.update(**{key: value})


# 全局单例
_config_manager_instance: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器实例"""
    global _config_manager_instance
    if _config_manager_instance is None:
        _config_manager_instance = ConfigManager()
    return _config_manager_instance


def get_config() -> WanderConfig:
    """获取当前配置"""
    return get_config_manager().config


def init_config(
    config: Optional[WanderConfig] = None,
    config_file: Optional[str] = None
) -> ConfigManager:
    """初始化全局配置管理器"""
    global _config_manager_instance
    _config_manager_instance = ConfigManager(config=config, config_file=config_file)
    return _config_manager_instance
