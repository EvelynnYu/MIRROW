"""
Agent Tools 适配器

将 agent_tools 的设计理念集成到现有 BehaviorScheduler 架构中。

核心改进：
1. 使用强制格式输出，避免LLM无法识别工具调用意图
2. 支持多模型（DeepSeek、GLM、Ollama等）
3. 兼容现有工具架构

使用方式：
    from agent_tools_adapter import AgentToolsAdapter

    adapter = AgentToolsAdapter.for_deepseek(api_key="...")
    adapter.register_tools([weather_tool, screenshot_tool])

    result = await adapter.execute("北京天气怎么样？")
"""

import os
from typing import Dict, Any, Optional, List

from .llm_wrapper import (
    LLMWrapper,
    OpenAICompatibleWrapper,
    DeepSeekWrapper,
    GLMWrapper
)


class AgentToolsAdapter:
    """
    Agent Tools 适配器

    将强制格式输出的工具调用能力适配到现有 BehaviorScheduler 架构。

    关键特性：
    1. 强制格式输出 - 通过系统提示词强制LLM输出 TOOL_CALL: 格式
    2. 不依赖"智能识别" - 解决LLM无法识别工具调用意图的问题
    3. 兼容现有工具 - 适配现有的 BaseTool 工具类
    """

    def __init__(self, wrapper: LLMWrapper):
        self._wrapper = wrapper
        self._tools: Dict[str, Any] = {}

    @classmethod
    def for_deepseek(cls, api_key: str = None, model: str = "deepseek-v4-pro") -> "AgentToolsAdapter":
        """创建DeepSeek适配器"""
        wrapper = DeepSeekWrapper(api_key=api_key, model=model)
        return cls(wrapper)

    @classmethod
    def for_glm(cls, api_key: str = None, model: str = "glm-4") -> "AgentToolsAdapter":
        """创建GLM适配器"""
        wrapper = GLMWrapper(api_key=api_key, model=model)
        return cls(wrapper)

    @classmethod
    def for_ollama(cls, model: str = "llama3.1", base_url: str = "http://localhost:11434/v1") -> "AgentToolsAdapter":
        """创建Ollama适配器"""
        wrapper = OpenAICompatibleWrapper(
            api_key="ollama",
            base_url=base_url,
            model=model,
        )
        return cls(wrapper)

    @classmethod
    def for_openai_compatible(
        cls,
        api_key: str,
        base_url: str,
        model: str
    ) -> "AgentToolsAdapter":
        """创建通用OpenAI兼容适配器"""
        wrapper = OpenAICompatibleWrapper(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        return cls(wrapper)

    def register_tool(self, tool: Any) -> None:
        """
        注册工具（适配现有BaseTool格式）

        Args:
            tool: 必须有 name, description, parameters_schema 属性
        """
        # 创建适配后的工具对象
        adapted_tool = ToolAdapter(tool)
        self._wrapper.register_tool(adapted_tool)
        self._tools[tool.name] = tool

    def register_tools(self, tools: List[Any]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register_tool(tool)

    def get_system_prompt(self) -> str:
        """获取系统提示词（用于构建完整系统提示）"""
        return self._wrapper._create_system_prompt()

    async def execute(
        self,
        user_input: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None
    ) -> Dict[str, Any]:
        """
        执行LLM调用

        Args:
            user_input: 用户输入
            conversation_history: 对话历史
            system_prompt: 自定义系统提示词

        Returns:
            {
                "content": str,           # LLM完整响应
                "natural_language": str,  # 自然语言部分（立即推送到前端）
                "reasoning": str,         # 推理部分
                "tool_call": dict | None, # 解析出的工具调用 {"tool": str, "parameters": dict}
                "has_tool_call": bool,    # 是否有工具调用
                "error": str | None       # 错误信息
            }
        """
        result = await self._wrapper.execute(
            user_input=user_input,
            conversation_history=conversation_history,
            system_prompt=system_prompt
        )

        content = result.get("content", "")
        tool_call = result.get("tool_call")
        tool_calls = result.get("tool_calls", [])

        # 提取自然语言部分（用于立即推送到前端）
        natural_language = self._wrapper.extract_natural_language(content)

        return {
            "content": content,
            "natural_language": natural_language,
            "reasoning": result.get("reasoning", ""),
            "tool_call": tool_call,
            "tool_calls": tool_calls,
            "has_tool_call": tool_call is not None,
            "error": result.get("error"),
        }

    def get_tools_description(self) -> str:
        """获取工具描述（兼容现有接口）"""
        tools_desc = []
        for name, info in self._wrapper.tools.items():
            tools_desc.append(
                f"- {name}: {info['description']}\n"
                f"  参数: {info['schema_nl']}"
            )
        return "\n".join(tools_desc)


class ToolAdapter:
    """
    工具适配器

    将现有的 BaseTool 适配为统一的工具接口。
    """

    def __init__(self, base_tool: Any):
        self._base_tool = base_tool
        self.name = base_tool.name
        self.description = base_tool.description
        self.input_schema = getattr(base_tool, 'parameters_schema', {})
        self.visible_to_pro = getattr(base_tool, 'visible_to_pro', True)

    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具（同步包装）"""
        import asyncio

        if asyncio.iscoroutinefunction(self._base_tool.execute):
            try:
                loop = asyncio.get_running_loop()
                # 在已有事件循环中，使用 run_coroutine_threadsafe 或同步等待
                # 由于调度器本身是异步的，这里不应该被同步调用
                # 返回一个标记，让调用者知道需要异步执行
                return {
                    "type": "tool_result",
                    "content": {
                        "status": "async_required",
                        "message": "此工具需要异步执行，请使用 run_async()",
                        "params": params
                    }
                }
            except RuntimeError:
                # 没有运行中的事件循环，可以安全使用 asyncio.run
                result = asyncio.run(self._base_tool.execute(**params))
        else:
            result = self._base_tool.execute(**params)

        if hasattr(result, 'status'):
            return {
                "type": "tool_result",
                "content": {
                    "status": result.status.value if hasattr(result.status, 'value') else str(result.status),
                    "message": result.content,
                    "error": result.error,
                }
            }
        else:
            return {
                "type": "tool_result",
                "content": result
            }

    async def run_async(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具（异步）"""
        import asyncio

        if asyncio.iscoroutinefunction(self._base_tool.execute):
            result = await self._base_tool.execute(**params)
        else:
            result = self._base_tool.execute(**params)

        if hasattr(result, 'status'):
            return {
                "type": "tool_result",
                "content": {
                    "status": result.status.value if hasattr(result.status, 'value') else str(result.status),
                    "message": result.content,
                    "error": result.error,
                }
            }
        else:
            return {
                "type": "tool_result",
                "content": result
            }
