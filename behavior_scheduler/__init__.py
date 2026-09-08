# 行为调度器 - Pro 生成 NL → Flash 提取意图 → 标准化 → 执行工具
#
# 架构层级：
# ┌─────────────────────────────────────────────────────────┐
# │                 决策层 (Pro LLM)                         │
# │      生成自然语言回复（可能附带 TOOL_CALL 快速路径）      │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │               意图提取层 (Flash LLM)                     │
# │    Pro 未输出 TOOL_CALL 时，Flash 从 NL 提取意图+参数    │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │               标准化层 (ToolStandardizer)                │
# │    规范化 JSON → 填充默认值 → 校验 schema               │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                  执行层 (Tools)                          │
# │              具体工具的实现与执行                         │
# └─────────────────────────────────────────────────────────┘

from .scheduler import BehaviorScheduler, get_global_scheduler, init_global_scheduler
from .base_tool import BaseTool, ToolResult
from .tools import register_all_tools
from .command_layer import (
    CommandParser,
    CommandExecutor,
    Command,
    CommandStatus,
    CommandParseResult,
    create_command_parser,
    create_command_executor
)
from .tool_standardizer import ToolCallStandardizer, StandardizedToolCall

__all__ = [
    # 调度器
    "BehaviorScheduler",
    "get_global_scheduler",
    "init_global_scheduler",
    # 工具基类
    "BaseTool",
    "ToolResult",
    "register_all_tools",
    # 命令层
    "CommandParser",
    "CommandExecutor",
    "Command",
    "CommandStatus",
    "CommandParseResult",
    "create_command_parser",
    "create_command_executor",
    # 标准化层
    "ToolCallStandardizer",
    "StandardizedToolCall",
]
