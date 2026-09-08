"""
简化的 LLM Wrapper 实现

基于 agent_tools 项目设计，但移除了复杂的相对导入。
提供统一的工具调用抽象层。

核心思想：通过系统提示词强制LLM输出特定格式，而非依赖LLM"智能识别"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import json
import re
import asyncio
from mirrow_core.token_tracker import log_token_usage
import os


def _append_jsonl(path: str, entry: dict):
    """同步追加一行 JSON 到日志文件（由 asyncio.to_thread 调用）"""
    import json as _json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + "\n")


class LLMWrapper(ABC):
    """
    LLM包装器抽象基类

    核心职责：
    1. 工具注册（暴露NL schema）
    2. 系统提示词构建（强制格式输出）
    3. Provider特定的执行逻辑
    """

    def __init__(self) -> None:
        self.tools: Dict[str, Dict[str, Any]] = {}

    def _convert_schema_to_nl(self, schema: Dict[str, Any]) -> str:
        """将JSONSchema转换为自然语言描述"""
        nl_desc = []
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])

        for name, details in props.items():
            desc = details.get("description", "无描述")
            type_info = details.get("type", "any")
            req_mark = "*" if name in required else ""
            default_val = details.get("default")
            enum_vals = details.get("enum")

            line = f"- {name} ({type_info}{req_mark}): {desc}"
            if enum_vals:
                line += f" [可选值: {', '.join(map(str, enum_vals))}]"
            if default_val is not None:
                line += f" (默认: {default_val})"
            nl_desc.append(line)

        return "\n".join(nl_desc)

    def register_tool(self, tool: Any) -> None:
        """注册工具"""
        schema = getattr(tool, 'input_schema', {}) or getattr(tool, 'parameters_schema', {}) or {}
        self.tools[tool.name] = {
            "tool": tool,
            "description": tool.description,
            "schema_nl": self._convert_schema_to_nl(schema),
            "schema_raw": schema,
        }

    def register_tools(self, tools: List[Any]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register_tool(tool)

    def _create_system_prompt(self) -> str:
        """构建系统提示词：工具可用性描述 + 强制规则（行为约束）。

        工具数据从 build_tool_data_entries 统一取（与 Flash extractor 共用数据源）。
        """
        from datetime import datetime
        from context_builder.ingredients import build_tool_data_entries

        tools_desc = []
        for entry in build_tool_data_entries(self.tools, visible_only=True):
            tools_desc.append(
                f"### {entry['name']}\n"
                f"{entry['description']}\n"
                f"参数:\n{entry['schema_nl']}\n"
            )
        tools_block = "\n".join(tools_desc)

        # 注意：当前时间不放在系统提示词中（避免破坏 DeepSeek prompt caching），
        # 而是附加在 execute() 的 user_input 末尾。

        return f"""## 可用工具

以下工具供你调用，**必须使用对应的工具**，不能凭自己的知识编造答案。

{tools_block}

## 工具调用规则

1. 实时数据（天气、时间、搜索、网页内容等）：**必须**调用对应工具，不能凭记忆回答
2. 文件操作（读、写、执行代码）：**必须**调用对应工具
3. 用户明确要求"搜索""查""看""刷""浏览""打开"等：**必须**调用对应工具
4. 当你不确定是否需要工具时：优先调用工具，不要猜
5. 如果工具返回错误，根据错误信息修正参数后重试，不要放弃
6. 每次可以调用一个或多个工具；如需连续操作（如先删旧提醒再建新），每步一行 TOOL_CALL
7. 工具名必须是上述列表中存在的工具
8. 参数必须符合 schema 定义
9. 你不是被动的问答机——可以**主动**调用工具关心用户：主动 eyes 看她、主动 check_phone 查手机、主动 schedule_self_task 设查岗、主动 band 查健康。不需要等用户开口要求。

"""

    def extract_natural_language(self, content: str) -> str:
        """从LLM输出中提取自然语言部分"""
        # 格式: NATURAL_LANGUAGE:\n[内容]\n\nTOOL_CALL:...
        # 也兼容 "NATURAL LANGUAGE:" (空格) 的变体
        import re
        marker_pattern = r'NATURAL[_\s]LANGUAGE\s*:\s*\n?'
        match = re.search(marker_pattern, content)
        if match:
            try:
                # 提取 marker 之后的内容
                nl_start = match.end()
                remaining = content[nl_start:].strip()

                # 查找 TOOL_CALL 标记（支持有/无冒号），如果存在则截取之前的内容
                tool_marker = re.search(r'\n\s*TOOL_CALL\s*[:：\n]', remaining)
                if tool_marker:
                    natural_language = remaining[:tool_marker.start()].strip()
                else:
                    natural_language = remaining.strip()

                return natural_language
            except Exception:
                pass

        # 兼容旧格式：如果没有 NATURAL_LANGUAGE: 标记，使用 extract_reasoning 逻辑
        return self.extract_reasoning(content)

    def extract_tool_calls(self, content: str) -> List[Dict[str, Any]]:
        """从LLM输出中提取所有工具调用（支持多个 TOOL_CALL）"""
        def _extract_json(text: str) -> Optional[Dict[str, Any]]:
            """从文本中提取第一个完整的 JSON 对象"""
            start = text.find("{")
            if start == -1:
                return None
            brace_count = 0
            in_string = False
            escape = False
            for i, ch in enumerate(text[start:], start):
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_string:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch == "{":
                        brace_count += 1
                    elif ch == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            try:
                                return json.loads(text[start:i + 1])
                            except json.JSONDecodeError:
                                return None
            return None

        def _normalize_tool_call(raw: dict) -> dict:
            tc = dict(raw)
            if "tool_name" in tc and "tool" not in tc:
                tc["tool"] = tc.pop("tool_name")
            if "arguments" in tc and "parameters" not in tc:
                tc["parameters"] = tc.pop("arguments")
            return tc

        results = []

        # 格式1: 多个 TOOL_CALL: 块（"TOOL_CALL:"后接JSON，允许多个）
        remaining = content
        for marker in ["TOOL_CALL:", "TOOL_CALL\n", "TOOL_CALL "]:
            while marker in remaining:
                idx = remaining.find(marker)
                after_marker = remaining[idx + len(marker):].strip()
                for fence in ["```json", "```"]:
                    if after_marker.startswith(fence):
                        after_marker = after_marker[len(fence):].strip()
                if after_marker.endswith("```"):
                    after_marker = after_marker[:-3].strip()
                result = _extract_json(after_marker)
                if result:
                    results.append(_normalize_tool_call(result))
                    # 跳过已解析的部分，继续查找下一个
                    json_end = after_marker.find("}") + 1
                    remaining = after_marker[json_end:]
                else:
                    break
            if results:
                break  # 只用第一个匹配的 marker 格式

        # 格式2: [TOOL_CALL]...[/TOOL_CALL] (兼容旧格式)
        if not results:
            pattern = r'\[TOOL_CALL\]\s*(\{.*?\})\s*\[/TOOL_CALL\]'
            for match in re.finditer(pattern, content, re.DOTALL):
                try:
                    results.append(_normalize_tool_call(json.loads(match.group(1))))
                except json.JSONDecodeError:
                    pass

        # 格式3: 兜底 — 从全文提取第一个 JSON
        if not results:
            result = _extract_json(content)
            if result and ("tool" in result or "name" in result or "tool_name" in result):
                results.append(_normalize_tool_call(result))

        return results

    def extract_tool_call(self, content: str) -> Optional[Dict[str, Any]]:
        """从LLM输出中提取工具调用

        支持多种格式：
        - TOOL_CALL: 后接 JSON（带冒号）
        - TOOL_CALL\n 后接 JSON（无冒号，换行）
        - [TOOL_CALL]...[/TOOL_CALL] 旧格式
        - 纯 JSON 对象（兜底）
        """
        def _extract_json(text: str) -> Optional[Dict[str, Any]]:
            """从文本中提取第一个完整的 JSON 对象"""
            start = text.find("{")
            if start == -1:
                return None
            brace_count = 0
            in_string = False
            escape = False
            for i, ch in enumerate(text[start:], start):
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_string:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch == "{":
                        brace_count += 1
                    elif ch == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            try:
                                return json.loads(text[start:i + 1])
                            except json.JSONDecodeError:
                                return None
            return None

        def _normalize_tool_call(raw: dict) -> dict:
            """归一化 tool_name→tool, arguments→parameters"""
            tc = dict(raw)
            if "tool_name" in tc and "tool" not in tc:
                tc["tool"] = tc.pop("tool_name")
            if "arguments" in tc and "parameters" not in tc:
                tc["parameters"] = tc.pop("arguments")
            return tc

        # 格式1: TOOL_CALL 后接 JSON（支持冒号、空格、换行等分隔符）
        for marker in ["TOOL_CALL:", "TOOL_CALL\n", "TOOL_CALL "]:
            if marker in content:
                after_marker = content.split(marker, 1)[1].strip()
                # 去掉可能的代码块标记
                for fence in ["```json", "```"]:
                    if after_marker.startswith(fence):
                        after_marker = after_marker[len(fence):].strip()
                if after_marker.endswith("```"):
                    after_marker = after_marker[:-3].strip()
                result = _extract_json(after_marker)
                if result:
                    return _normalize_tool_call(result)

        # 格式2: [TOOL_CALL]...[/TOOL_CALL] (兼容旧格式)
        pattern = r'\[TOOL_CALL\]\s*(\{.*?\})\s*\[/TOOL_CALL\]'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            try:
                return _normalize_tool_call(json.loads(match.group(1)))
            except json.JSONDecodeError:
                pass

        # 格式3: 兜底 — 从全文提取第一个 JSON（模型有时直接输出 JSON 不加标记）
        result = _extract_json(content)
        if result and ("tool" in result or "name" in result or "tool_name" in result):
            return _normalize_tool_call(result)

        return None

    def extract_reasoning(self, content: str) -> str:
        """提取推理部分（工具调用之前的自然语言）"""
        # 先尝试提取 NATURAL_LANGUAGE: 之后的内容
        nl_pattern = r'NATURAL[_\s]LANGUAGE\s*:\s*\n?'
        nl_match = re.search(nl_pattern, content)
        if nl_match:
            content = content[nl_match.end():].strip()

        for marker in ["TOOL_CALL:", "TOOL_CALL\n", "TOOL_CALL "]:
            if marker in content:
                idx = content.find(marker)
                return content[:idx].strip()

        pattern = r'\[TOOL_CALL\]'
        match = re.search(pattern, content)
        if match:
            return content[:match.start()].strip()

        return content.strip()

    @abstractmethod
    async def execute(
        self,
        user_input: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None
    ) -> Dict[str, Any]:
        """执行LLM调用"""
        raise NotImplementedError("子类必须实现execute()")


class OpenAICompatibleWrapper(LLMWrapper):
    """
    OpenAI兼容的LLM包装器

    支持 DeepSeek、GLM、Ollama 等OpenAI兼容API
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__()

        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("GLM_API_KEY") or "none"
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or os.getenv("GLM_BASE_URL")
        self.model = model or os.getenv("OPENAI_MODEL") or os.getenv("DEEPSEEK_MODEL") or os.getenv("GLM_MODEL") or "deepseek-v4-pro"

        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)

        # DeepSeek V4 thinking模式配置
        self.thinking_enabled = kwargs.get("thinking_enabled", os.getenv("DEEPSEEK_THINKING_ENABLED", "true").lower() == "true")
        self.reasoning_effort = kwargs.get("reasoning_effort", os.getenv("DEEPSEEK_REASONING_EFFORT", "medium"))

        self._client = None

    def _get_client(self):
        """延迟初始化OpenAI client"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                import httpx
                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=httpx.Timeout(90.0, connect=10.0),
                    max_retries=1,
                    http_client=httpx.AsyncClient(
                        timeout=httpx.Timeout(90.0, connect=10.0),
                        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
                        proxy=None,
                        trust_env=False,
                    ),
                )
            except ImportError:
                raise ImportError("请安装openai: pip install openai")
        return self._client

    async def execute(
        self,
        user_input: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None
    ) -> Dict[str, Any]:
        """执行LLM调用"""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"OpenAICompatibleWrapper.execute() using model: {self.model}")
        try:
            client = self._get_client()

            messages = []

            base_system = self._create_system_prompt()
            if system_prompt:
                full_system = f"{system_prompt}\n\n{base_system}"
            else:
                full_system = base_system
            messages.append({"role": "system", "content": full_system})

            if conversation_history:
                # 支持多轮对话思维链：保留reasoning_content
                for msg in conversation_history:
                    msg_dict = {"role": msg.get("role"), "content": msg.get("content")}
                    # 如果有reasoning_content，添加到消息中
                    reasoning_content = msg.get("reasoning_content")
                    if reasoning_content:
                        msg_dict["reasoning_content"] = reasoning_content
                    messages.append(msg_dict)

            # 检查对话历史中是否已包含当前用户消息，避免重复添加
            # 不能只检查最后一条——工具调用后的后续轮次中，末尾是系统消息
            should_add_user_input = True
            if conversation_history and len(conversation_history) > 0:
                # 从后往前找最近一条 user 消息，检查内容是否匹配
                for msg in reversed(conversation_history):
                    if msg.get("role") == "user":
                        if msg.get("content") == user_input:
                            should_add_user_input = False
                            logger.info("Skipping duplicate user input (found in conversation history)")
                        break  # 只检查最近一条用户消息

            if should_add_user_input:
                # 将当前时间附加到用户消息（不放在 system prompt 中以避免破坏缓存）
                from datetime import datetime
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                # 无方括号——防止 AI 学到 [标签: HH:MM] 格式在回复里引用时间
                messages.append({"role": "user", "content": f"当前时间: {current_time}\n\n{user_input}"})

            # 构建请求参数
            # Flash 模型不需要高 max_tokens（响应通常很短）
            is_flash = "flash" in self.model.lower()
            actual_max_tokens = min(self.max_tokens, 1024) if is_flash else self.max_tokens
            request_params = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": actual_max_tokens,
            }

            # DeepSeek V4 thinking模式：
            # - Flash 模型用 "low"（够推理出工具调用，但不拖慢响应）
            # - Pro/非Flash 模型用配置的 reasoning_effort（默认 "medium"）
            is_deepseek_v4 = "deepseek" in self.model.lower() and ("v4" in self.model.lower() or "pro" in self.model.lower())
            is_flash_model = "flash" in self.model.lower()
            if is_deepseek_v4 and self.thinking_enabled:
                if is_flash_model:
                    request_params["reasoning_effort"] = "medium"
                    request_params["extra_body"] = {"thinking": {"type": "enabled"}}
                    logger.info("DeepSeek Flash thinking模式: reasoning_effort=medium")
                else:
                    request_params["reasoning_effort"] = self.reasoning_effort
                    request_params["extra_body"] = {"thinking": {"type": "enabled"}}
                    logger.info(f"DeepSeek thinking模式已启用: reasoning_effort={self.reasoning_effort}")

            # ── 调度器上下文日志（仅 Pro 模型，不记 Flash）──
            if not is_flash_model:
                try:
                    from datetime import datetime as _dt
                    import hashlib as _hl
                    _sys_len = len(full_system)
                    # 诊断：完整 messages 的 hash + 每条消息首尾预览（定位缓存前缀在何处断裂）
                    _msgs_hash = _hl.md5(
                        "|||".join(f"{m.get('role')}:{str(m.get('content'))[:200]}" for m in messages).encode()
                    ).hexdigest()[:12]
                    _msgs_preview = [
                        {"i": i, "role": m.get("role"), "c": str(m.get("content"))[:60]}
                        for i, m in enumerate(messages)
                    ]
                    _log = {
                        "timestamp": _dt.now().isoformat(),
                        "model": self.model,
                        "message_count": len(messages),
                        "system_prompt_preview": full_system[:3000],
                        "system_prompt_tail": full_system[-2000:] if _sys_len > 5000 else "",
                        "total_system_chars": _sys_len,
                        "messages_hash": _msgs_hash,
                        "messages_preview": _msgs_preview,
                        "thinking_enabled": is_deepseek_v4 and self.thinking_enabled,
                        "source": "behavior_scheduler",
                    }
                    _log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
                    os.makedirs(_log_dir, exist_ok=True)
                    await asyncio.to_thread(_append_jsonl, os.path.join(_log_dir, "llm_context_log.jsonl"), _log)
                except Exception:
                    pass  # 日志失败不应影响主流程

            response = await client.chat.completions.create(**request_params)

            message = response.choices[0].message
            content = message.content or ""

            # DeepSeek V4 thinking模式：reasoning_content 字段
            # 使用多种方式提取，兼容不同版本 OpenAI SDK 的 Pydantic 行为
            reasoning_content = None
            # 方法1：直接属性访问（适用于 Pydantic extra="allow"）
            try:
                rc = getattr(message, 'reasoning_content', None)
                if rc:
                    reasoning_content = rc
            except Exception:
                pass
            # 方法2：model_extra 字典（Pydantic v2 extra 字段存储）
            if not reasoning_content:
                try:
                    me = getattr(message, 'model_extra', None)
                    if me and isinstance(me, dict):
                        reasoning_content = me.get('reasoning_content')
                except Exception:
                    pass
            # 方法3：model_dump 全量导出
            if not reasoning_content:
                try:
                    dumped = message.model_dump()
                    reasoning_content = dumped.get('reasoning_content')
                except Exception:
                    pass
            # 方法4：从 raw_response 直接解析 JSON（终极兜底）
            if not reasoning_content:
                try:
                    raw = response.model_dump()
                    rc = raw.get('choices', [{}])[0].get('message', {}).get('reasoning_content')
                    if rc:
                        reasoning_content = rc
                except Exception:
                    pass

            logger.info(f"Model response: model={self.model}, has_reasoning_content={reasoning_content is not None}, reasoning_len={len(reasoning_content) if reasoning_content else 0}")

            tool_call = self.extract_tool_call(content)
            tool_calls = self.extract_tool_calls(content)

            # 推理链处理：
            # DeepSeek V4 thinking模式返回:
            #   - reasoning_content: 推理过程（思维链）
            #   - content: 最终输出（可能包含 TOOL_CALL）
            #
            # 对于前端显示:
            #   - reasoning: 推理过程（显示在折叠的思维链中）
            #   - content: 最终输出（显示在消息气泡中）
            #
            # 如果没有 reasoning_content（非 deepseek-v4-pro 模型）:
            #   - 从 content 中提取 TOOL_CALL 之前的自然语言部分作为推理
            #   - 但如果没有工具调用，则推理为空（避免重复）
            if reasoning_content:
                # DeepSeek V4 thinking模式: reasoning_content 是推理，content 是最终输出
                reasoning = reasoning_content
            else:
                # 其他模型: 从 content 中提取工具调用前的部分
                reasoning = self.extract_reasoning(content)
                # 如果没有工具调用，避免推理和内容重复
                if tool_call is None:
                    reasoning = ""

            asyncio.create_task(log_token_usage("agent_tools", self.model, response.usage if hasattr(response, 'usage') else None))
            return {
                "content": content,
                "reasoning": reasoning,
                "tool_call": tool_call,
                "tool_calls": tool_calls,
                "raw_response": response
            }

        except Exception as e:
            return {
                "content": "",
                "reasoning": "",
                "tool_call": None,
                "error": str(e),
                "raw_response": None
            }


class DeepSeekWrapper(OpenAICompatibleWrapper):
    """DeepSeek专用包装器（支持V4 thinking模式）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs
    ) -> None:
        actual_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"DeepSeekWrapper initialized with model: {actual_model}, thinking_enabled: {kwargs.get('thinking_enabled', os.getenv('DEEPSEEK_THINKING_ENABLED', 'true'))}")
        super().__init__(
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=actual_model,
            **kwargs
        )


class GLMWrapper(OpenAICompatibleWrapper):
    """智谱GLM专用包装器"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__(
            api_key=api_key or os.getenv("GLM_API_KEY"),
            base_url=os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            model=model or os.getenv("GLM_MODEL", "glm-4"),
            **kwargs
        )
