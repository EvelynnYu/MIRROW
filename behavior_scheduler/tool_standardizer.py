"""
工具调用标准化层

在 agent_tools 流程中复用 command_layer 的 ParameterValidator 和 ParameterGenerator，
加一层 JSON key 归一化，确保 extract_tool_call() 提取的原始 JSON 在进入 CommandExecutor 前
经过完整的校验和补齐。

使用方式：
    standardizer = ToolCallStandardizer(tools_dict)
    result = standardizer.standardize({"tool": "get_weather", "parameters": {"city": "北京"}})
    if result.is_valid:
        # result.tool_name, result.parameters 可用
    else:
        # result.error_message 包含错误描述
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional
import logging

from .command_layer import ParameterValidator, ParameterGenerator, CommandStatus

logger = logging.getLogger(__name__)


@dataclass
class StandardizedToolCall:
    """标准化后的工具调用结果"""
    tool_name: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    is_valid: bool = True
    error_message: str = ""


class ToolCallStandardizer:
    """
    工具调用标准化器

    负责三层处理：
    1. normalize_keys — 将 LLM 可能输出的不同 key 风格映射为标准格式
    2. fill_defaults  — 复用 ParameterGenerator 填充 schema 默认值
    3. validate       — 复用 ParameterValidator 校验 required/type/enum
    """

    # key 别名映射表：常见变体 → 标准 key
    KEY_ALIASES = {
        "name": "tool",
        "function": "tool",
        "input_schema": "parameters",
        "arguments": "parameters",
        "params": "parameters",
        "args": "parameters",
    }

    def __init__(self, tools: Dict[str, Any]):
        self._generator = ParameterGenerator(tools)
        self._validator = ParameterValidator(tools)

    def update_tools(self, tools: Dict[str, Any]):
        """工具列表更新时同步内部状态"""
        self._generator.update_tools(tools)
        self._validator.update_tools(tools)

    # ----- 内部归一化方法 -----

    def normalize_keys(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        将 LLM 输出的 key 变体映射为标准格式。

        支持的输入格式举例：
            {"tool": "xxx", "parameters": {...}}         — 标准格式
            {"name": "xxx", "arguments": {...}}           — OpenAI function calling 风格
            {"tool": "xxx", "input_schema": {...}}        — agent_tools 内部格式
            {"function": "xxx", "params": {...}}          — 其他常见变体
        """
        if not isinstance(raw, dict):
            logger.warning("tool_standardizer: raw is not a dict (%s)", type(raw).__name__)
            return {"tool": "", "parameters": {}}

        normalized = {}
        for key, value in raw.items():
            std_key = self.KEY_ALIASES.get(key, key)
            # 如果同一个标准 key 出现多次，后面的覆盖前面的
            normalized[std_key] = value

        return normalized

    # ----- 委托给 command_layer -----

    def fill_defaults(self, tool_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """填充 schema 默认值"""
        return self._generator.generate(tool_name, parameters)

    def validate(self, tool_name: str, parameters: Dict[str, Any]) -> "ValidationResult":
        """参数校验（required / type / enum）"""
        return self._validator.validate(tool_name, parameters)

    # ----- 全流程标准化 -----

    def standardize(self, raw_tool_call: Optional[Dict[str, Any]]) -> StandardizedToolCall:
        """
        全流程标准化：normalize → fill_defaults → validate

        Args:
            raw_tool_call: extract_tool_call() 返回的原始 dict，或 None

        Returns:
            StandardizedToolCall
        """
        if not raw_tool_call:
            return StandardizedToolCall(
                is_valid=False,
                error_message="工具调用为空"
            )

        # 1. key 归一化
        normalized = self.normalize_keys(raw_tool_call)
        tool_name = normalized.get("tool", "")
        parameters = normalized.get("parameters", {})

        if not tool_name:
            return StandardizedToolCall(
                is_valid=False,
                error_message="工具调用中缺少 tool/name 字段"
            )

        # 2. 填充默认值
        parameters = self.fill_defaults(tool_name, parameters)

        # 3. 参数校验
        validation = self.validate(tool_name, parameters)
        if not validation.is_valid:
            return StandardizedToolCall(
                tool_name=tool_name,
                parameters=parameters,
                is_valid=False,
                error_message=validation.error_message,
            )

        return StandardizedToolCall(
            tool_name=tool_name,
            parameters=validation.parameters,
            is_valid=True,
        )
