# 工具基类定义

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any, Optional
from enum import Enum


class ToolStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    NEED_USER_INPUT = "need_user_input"


@dataclass
class ToolResult:
    """工具执行结果"""
    status: ToolStatus
    content: str  # 工具返回的内容，会作为上下文传给LLM
    error: Optional[str] = None
    need_user_action: bool = False  # 是否需要用户额外操作
    extra_data: Optional[Dict[str, Any]] = None  # 附加数据（如语音base64）
    delivery: str = "model"  # model=结果回注LLM；ui_only=只进入工具挂载条/日志


class BaseTool(ABC):
    """工具基类"""

    name: str = ""
    description: str = ""  # 给LLM看的工具描述
    flash_description: str = ""  # 给 Flash 意图提取的目录式精简描述（一句话"工具是干嘛的"）；空则回退 description
    parameters_schema: Dict[str, Any] = {}  # 参数schema
    single_use: bool = False  # True 时每轮对话只能执行一次（如 send_voice）
    visible_to_pro: bool = True  # False = 对 Pro/Flash 隐藏，仅内部使用（如 eyes 的子工具）
    ui_only_result: bool = False  # 整个工具的结果只进入挂载条
    ui_only_actions = frozenset()  # 仅指定 action 的结果只进入挂载条

    def __init_subclass__(cls, **kwargs):
        """子类化时深拷贝 parameters_schema，防止类变量共享"""
        super().__init_subclass__(**kwargs)
        if 'parameters_schema' in cls.__dict__:
            import copy
            cls.parameters_schema = copy.deepcopy(cls.parameters_schema)

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具"""
        pass

    def get_user_facing_description(self, **kwargs) -> str:
        """返回给用户看的操作描述（中文），子类可重写以提供更精确的文案"""
        return f"正在使用{self.name}"

    def get_result_delivery(self, **kwargs) -> str:
        if self.ui_only_result or kwargs.get("action") in self.ui_only_actions:
            return "ui_only"
        return "model"

    def get_tool_definition(self) -> Dict[str, Any]:
        """获取工具定义（给LLM看的）"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema
        }
