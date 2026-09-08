# 命令层 - 将LLM输出转换为规范化命令格式
# 架构：意图识别层(LLM) → 参数生成层 → JSON校验层 → 工具执行层

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable
from enum import Enum
import json
import logging
import re
import asyncio

logger = logging.getLogger(__name__)


# ==================== 数据结构定义 ====================

class CommandStatus(Enum):
    """命令状态"""
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN_TOOL = "unknown_tool"
    MISSING_PARAMS = "missing_params"
    TYPE_ERROR = "type_error"
    ENUM_ERROR = "enum_error"
    NOT_A_COMMAND = "not_a_command"


@dataclass
class Command:
    """规范化命令对象"""
    tool: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    raw_content: str = ""
    status: CommandStatus = CommandStatus.VALID
    error_message: str = ""
    confidence: float = 1.0

    def is_executable(self) -> bool:
        return self.status == CommandStatus.VALID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "parameters": self.parameters,
            "status": self.status.value,
            "error_message": self.error_message,
            "confidence": self.confidence
        }


@dataclass
class CommandParseResult:
    """命令解析结果"""
    is_command: bool
    command: Optional[Command] = None
    clean_content: str = ""

    @property
    def should_execute(self) -> bool:
        return self.is_command and self.command is not None and self.command.is_executable()


@dataclass
class IntentResult:
    """意图识别结果"""
    has_intent: bool
    tool_name: Optional[str] = None
    confidence: float = 0.0
    raw_params: Dict[str, Any] = field(default_factory=dict)
    clean_content: str = ""


@dataclass
class ValidationResult:
    """参数校验结果"""
    is_valid: bool
    parameters: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    error_type: Optional[CommandStatus] = None


# ==================== 意图识别层（LLM驱动） ====================

class IntentRecognizer:
    """
    意图识别层 - 使用LLM智能判断是否需要调用工具

    流程：
    1. 先尝试解析标准格式 [TOOL_CALL]...[/TOOL_CALL]
    2. 如果没有标准格式，调用LLM判断意图
    """

    # 标准格式正则 - 使用自定义匹配来处理嵌套JSON
    STANDARD_PATTERN = r'\[TOOL_CALL\]\s*'

    def _extract_json_object(self, content: str) -> Optional[str]:
        """提取完整的嵌套JSON对象"""
        start = content.find('{')
        if start == -1:
            return None

        brace_count = 0
        in_string = False
        escape_next = False

        for i, char in enumerate(content[start:], start):
            if escape_next:
                escape_next = False
                continue
            if char == '\\' and in_string:
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if not in_string:
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        return content[start:i+1]

        return None

    def __init__(
        self,
        available_tools: Dict[str, Any] = None,
        call_llm_func: Callable = None
    ):
        """
        初始化意图识别器

        Args:
            available_tools: 可用工具字典
            call_llm_func: LLM调用函数，签名: async (messages) -> {"content": str}
        """
        self.available_tools = available_tools or {}
        self.call_llm = call_llm_func
        self._lightweight_caller = None

    def update_tools(self, tools: Dict[str, Any]):
        self.available_tools = tools

    def set_llm_func(self, func: Callable):
        self.call_llm = func

    async def recognize_async(self, llm_output: str) -> IntentResult:
        """
        异步识别LLM输出中的工具调用意图

        Returns:
            IntentResult: 包含工具名、参数、置信度、清理后的内容
        """
        if not llm_output or not llm_output.strip():
            return IntentResult(has_intent=False, clean_content=llm_output or "")

        content = llm_output.strip()

        # 1. 先尝试解析标准格式（高置信度）
        result = self._recognize_standard_format(content)
        if result.has_intent:
            return result

        # 2. 使用LLM智能识别意图
        if self.call_llm:
            result = await self._recognize_with_llm(content)
            if result.has_intent:
                return result

        # 3. 没有识别到工具调用意图
        return IntentResult(has_intent=False, clean_content=content)

    def recognize(self, llm_output: str) -> IntentResult:
        """
        同步识别（仅支持标准格式，不支持LLM识别）
        """
        if not llm_output or not llm_output.strip():
            return IntentResult(has_intent=False, clean_content=llm_output or "")

        content = llm_output.strip()

        # 只尝试标准格式
        result = self._recognize_standard_format(content)
        if result.has_intent:
            return result

        return IntentResult(has_intent=False, clean_content=content)

    def _recognize_standard_format(self, content: str) -> IntentResult:
        """识别标准 [TOOL_CALL]...[/TOOL_CALL] 格式"""
        # 查找 [TOOL_CALL] 标记
        tool_call_start = content.find('[TOOL_CALL]')
        if tool_call_start == -1:
            return IntentResult(has_intent=False)

        # 提取 [TOOL_CALL] 之后的内容
        after_marker = content[tool_call_start + len('[TOOL_CALL]'):].strip()

        # 查找 [/TOOL_CALL] 结束标记
        tool_call_end = after_marker.find('[/TOOL_CALL]')
        if tool_call_end != -1:
            json_content = after_marker[:tool_call_end].strip()
        else:
            # 没有结束标记，尝试提取完整JSON
            json_content = self._extract_json_object(after_marker)

        if not json_content:
            return IntentResult(has_intent=False)

        try:
            data = json.loads(json_content)

            tool_name = data.get("tool", "")
            raw_params = data.get("parameters", {})

            # 移除工具调用标记，保留自然语言部分
            clean_content = content[:tool_call_start].strip()
            if tool_call_end != -1:
                remaining = after_marker[tool_call_end + len('[/TOOL_CALL]'):].strip()
                if remaining:
                    clean_content = clean_content + " " + remaining if clean_content else remaining

            return IntentResult(
                has_intent=True,
                tool_name=tool_name,
                confidence=1.0,
                raw_params=raw_params,
                clean_content=clean_content
            )
        except json.JSONDecodeError:
            return IntentResult(has_intent=False)

    async def _recognize_with_llm(self, content: str) -> IntentResult:
        """使用LLM智能识别意图（优先用小模型，失败时回退到注入的 call_llm）"""
        tools_desc = self._build_tools_description()

        intent_prompt = f"""你是一个意图识别助手。分析用户的回复，判断是否需要调用工具。

## 可用工具
{tools_desc}

## 任务
分析下面的回复内容，判断是否表达了调用工具的意图。

## 回复内容
{content}

## 输出格式
严格输出以下JSON格式，不要输出其他内容：
- 如果需要调用工具：{{"intent": "tool_call", "tool": "工具名", "parameters": {{参数}}, "reason": "简短理由"}}
- 如果不需要调用工具：{{"intent": "no_tool", "reason": "简短理由"}}

## 注意
1. 只有当回复明确表达了执行某个动作（如"我去刷小红书"、"让我查天气"）时才识别为工具调用
2. 如果只是普通对话或回答问题，不要识别为工具调用
3. 工具名必须是上述可用工具之一
4. 参数根据工具要求填写，如果不确定可以留空{{}}"""

        # 优先使用轻量级小模型，失败时回退到注入的 call_llm
        llm_func = self._get_lightweight_caller() or self.call_llm
        if not llm_func:
            return IntentResult(has_intent=False, clean_content=content)

        try:
            response = await llm_func([
                {"role": "system", "content": intent_prompt}
            ])

            llm_response = response.get("content", "").strip()

            result = self._parse_llm_intent_response(llm_response, content)
            return result

        except Exception as e:
            print(f"[IntentRecognizer] LLM意图识别失败: {e}")
            return IntentResult(has_intent=False, clean_content=content)

    def _get_lightweight_caller(self):
        """延迟初始化轻量级 LLM 调用器（DeepSeek Flash）"""
        if self._lightweight_caller is not None:
            return self._lightweight_caller
        import os
        api_key = os.getenv("INTENT_PARSER_API_KEY", "")
        if not api_key:
            return None
        from .intent_parser import create_lightweight_caller
        self._lightweight_caller = create_lightweight_caller(api_key)
        return self._lightweight_caller

    def _parse_llm_intent_response(self, llm_response: str, original_content: str) -> IntentResult:
        """解析LLM返回的意图识别结果"""
        try:
            # 尝试提取JSON
            json_match = re.search(r'\{[^{}]*\}', llm_response, re.DOTALL)
            if not json_match:
                return IntentResult(has_intent=False, clean_content=original_content)

            data = json.loads(json_match.group(0))

            intent = data.get("intent", "")

            if intent == "tool_call":
                tool_name = data.get("tool", "")
                parameters = data.get("parameters", {})

                # 验证工具是否存在
                if tool_name not in self.available_tools:
                    print(f"[IntentRecognizer] 未知工具: {tool_name}")
                    return IntentResult(has_intent=False, clean_content=original_content)

                return IntentResult(
                    has_intent=True,
                    tool_name=tool_name,
                    confidence=0.9,
                    raw_params=parameters,
                    clean_content=original_content
                )
            else:
                return IntentResult(has_intent=False, clean_content=original_content)

        except json.JSONDecodeError as e:
            print(f"[IntentRecognizer] JSON解析失败: {e}")
            return IntentResult(has_intent=False, clean_content=original_content)

    def _build_tools_description(self) -> str:
        """构建工具描述"""
        descriptions = []
        for name, tool in self.available_tools.items():
            desc = f"- {name}: {tool.description}"
            schema = getattr(tool, 'parameters_schema', {})
            if schema.get("properties"):
                props = []
                for prop_name, prop_def in schema["properties"].items():
                    prop_desc = f"{prop_name}"
                    if prop_def.get("type"):
                        prop_desc += f"({prop_def['type']})"
                    if prop_def.get("enum"):
                        prop_desc += f"[{','.join(prop_def['enum'])}]"
                    if prop_def.get("default"):
                        prop_desc += f"=默认{prop_def['default']}"
                    if prop_name in schema.get("required", []):
                        prop_desc += "*"
                    props.append(prop_desc)
                desc += f" 参数: {', '.join(props)}"
            descriptions.append(desc)
        return "\n".join(descriptions)


# ==================== 参数生成层 ====================

class ParameterGenerator:
    """
    参数生成层 - 填充默认值
    """

    def __init__(self, available_tools: Dict[str, Any] = None):
        self.available_tools = available_tools or {}

    def update_tools(self, tools: Dict[str, Any]):
        self.available_tools = tools

    def generate(
        self,
        tool_name: str,
        raw_params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成完整参数（填充默认值）
        """
        parameters = dict(raw_params)

        # 填充默认值
        defaults = self._get_defaults(tool_name)
        for key, value in defaults.items():
            if key not in parameters:
                parameters[key] = value

        return parameters

    def _get_defaults(self, tool_name: str) -> Dict[str, Any]:
        """获取工具参数默认值"""
        if tool_name not in self.available_tools:
            return {}

        tool = self.available_tools[tool_name]
        schema = getattr(tool, 'parameters_schema', {})
        properties = schema.get("properties", {})

        defaults = {}
        for param_name, param_def in properties.items():
            if "default" in param_def:
                defaults[param_name] = param_def["default"]

        return defaults


# ==================== JSON校验层 ====================

class ParameterValidator:
    """
    参数校验层 - 校验参数类型、必需性、枚举值
    """

    def __init__(self, available_tools: Dict[str, Any] = None):
        self.available_tools = available_tools or {}

    def update_tools(self, tools: Dict[str, Any]):
        self.available_tools = tools

    def validate(self, tool_name: str, parameters: Dict[str, Any]) -> ValidationResult:
        """校验参数"""
        if tool_name not in self.available_tools:
            return ValidationResult(
                is_valid=False,
                error_message=f"未知工具: {tool_name}",
                error_type=CommandStatus.UNKNOWN_TOOL
            )

        tool = self.available_tools[tool_name]
        schema = getattr(tool, 'parameters_schema', {})

        # 1. 检查必需参数
        required = schema.get("required", [])
        missing = [p for p in required if p not in parameters]
        if missing:
            return ValidationResult(
                is_valid=False,
                error_message=f"缺少必需参数: {', '.join(missing)}",
                error_type=CommandStatus.MISSING_PARAMS
            )

        # 2. 类型校验
        properties = schema.get("properties", {})
        for param_name, param_value in parameters.items():
            if param_name in properties:
                param_def = properties[param_name]
                type_error = self._validate_type(param_name, param_value, param_def)
                if type_error:
                    return ValidationResult(
                        is_valid=False,
                        error_message=type_error,
                        error_type=CommandStatus.TYPE_ERROR
                    )

        # 3. 枚举校验
        for param_name, param_value in parameters.items():
            if param_name in properties:
                param_def = properties[param_name]
                if "enum" in param_def:
                    if param_value not in param_def["enum"]:
                        return ValidationResult(
                            is_valid=False,
                            error_message=f"参数 {param_name} 值 '{param_value}' 不在允许范围内: {param_def['enum']}",
                            error_type=CommandStatus.ENUM_ERROR
                        )

        return ValidationResult(is_valid=True, parameters=parameters)

    def _validate_type(self, param_name: str, value: Any, param_def: Dict) -> Optional[str]:
        """校验参数类型"""
        expected_type = param_def.get("type")
        if not expected_type:
            return None

        type_valid = True
        if expected_type == "string":
            type_valid = isinstance(value, str)
        elif expected_type == "integer":
            type_valid = isinstance(value, int) and not isinstance(value, bool)
        elif expected_type == "number":
            type_valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif expected_type == "boolean":
            type_valid = isinstance(value, bool)
        elif expected_type == "array":
            type_valid = isinstance(value, list)
        elif expected_type == "object":
            type_valid = isinstance(value, dict)

        if not type_valid:
            return f"参数 {param_name} 类型错误: 期望 {expected_type}, 实际 {type(value).__name__}"

        return None


# ==================== 主解析器 ====================

class CommandParser:
    """
    命令解析器 - 协调意图识别、参数生成、参数校验三层
    """

    def __init__(
        self,
        available_tools: Dict[str, Any] = None,
        call_llm_func: Callable = None
    ):
        self.available_tools = available_tools or {}

        # 初始化三层
        self.intent_recognizer = IntentRecognizer(available_tools, call_llm_func)
        self.param_generator = ParameterGenerator(available_tools)
        self.validator = ParameterValidator(available_tools)

    def update_tools(self, tools: Dict[str, Any]):
        self.available_tools = tools
        self.intent_recognizer.update_tools(tools)
        self.param_generator.update_tools(tools)
        self.validator.update_tools(tools)

    def set_llm_func(self, func: Callable):
        """设置LLM调用函数"""
        self.intent_recognizer.set_llm_func(func)

    async def parse_async(self, llm_output: str) -> CommandParseResult:
        """
        异步解析LLM输出（支持LLM意图识别）
        """
        # 1. 意图识别（异步）
        intent = await self.intent_recognizer.recognize_async(llm_output)
        if not intent.has_intent:
            return CommandParseResult(
                is_command=False,
                clean_content=llm_output or ""
            )

        # 2. 参数生成
        parameters = self.param_generator.generate(
            intent.tool_name,
            intent.raw_params
        )

        # 3. 参数校验
        validation = self.validator.validate(intent.tool_name, parameters)

        # 4. 构建结果
        if validation.is_valid:
            command = Command(
                tool=intent.tool_name,
                parameters=validation.parameters,
                raw_content=llm_output,
                status=CommandStatus.VALID,
                confidence=intent.confidence
            )
        else:
            command = Command(
                tool=intent.tool_name,
                parameters=parameters,
                raw_content=llm_output,
                status=validation.error_type or CommandStatus.INVALID,
                error_message=validation.error_message,
                confidence=intent.confidence
            )

        return CommandParseResult(
            is_command=True,
            command=command,
            clean_content=intent.clean_content
        )

    def parse(self, llm_output: str) -> CommandParseResult:
        """
        同步解析（仅支持标准格式）
        """
        intent = self.intent_recognizer.recognize(llm_output)
        if not intent.has_intent:
            return CommandParseResult(
                is_command=False,
                clean_content=llm_output or ""
            )

        parameters = self.param_generator.generate(
            intent.tool_name,
            intent.raw_params
        )

        validation = self.validator.validate(intent.tool_name, parameters)

        if validation.is_valid:
            command = Command(
                tool=intent.tool_name,
                parameters=validation.parameters,
                raw_content=llm_output,
                status=CommandStatus.VALID,
                confidence=intent.confidence
            )
        else:
            command = Command(
                tool=intent.tool_name,
                parameters=parameters,
                raw_content=llm_output,
                status=validation.error_type or CommandStatus.INVALID,
                error_message=validation.error_message,
                confidence=intent.confidence
            )

        return CommandParseResult(
            is_command=True,
            command=command,
            clean_content=intent.clean_content
        )


# ==================== 命令执行器 ====================

class CommandExecutor:
    """命令执行器 - 执行规范化命令"""

    def __init__(self, tools: Dict[str, Any] = None):
        self.tools = tools or {}

    def update_tools(self, tools: Dict[str, Any]):
        self.tools = tools

    async def execute(self, command: Command) -> Dict[str, Any]:
        """执行命令"""
        if not command.is_executable():
            return {
                "success": False,
                "error": command.error_message,
                "status": command.status.value
            }

        tool_name = command.tool
        if tool_name not in self.tools:
            return {
                "success": False,
                "error": f"工具不存在: {tool_name}",
                "status": "unknown_tool"
            }

        tool = self.tools[tool_name]

        try:
            result = await tool.execute(**command.parameters)
            rv = {
                "success": result.status.value == "success",
                "content": result.content or "",
                "tool": tool_name,
                "parameters": command.parameters,
            }
            if result.error:
                rv["error"] = result.error
            if result.extra_data:
                print(f"[DEBUG-CMD] tool={tool_name} has extra_data keys={list(result.extra_data.keys())}", flush=True)
                rv["extra_data"] = result.extra_data
            return rv
        except Exception as e:
            logger.exception(f"工具执行异常: {tool_name}")
            return {
                "success": False,
                "error": f"工具执行异常: {str(e)}",
                "tool": tool_name,
                "parameters": command.parameters
            }


# ==================== 便捷函数 ====================

def create_command_parser(
    tools: Dict[str, Any] = None,
    call_llm_func: Callable = None
) -> CommandParser:
    return CommandParser(tools, call_llm_func)


def create_command_executor(tools: Dict[str, Any] = None) -> CommandExecutor:
    return CommandExecutor(tools)

