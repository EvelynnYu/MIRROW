# 工具注册

import inspect
import uuid as _uuid
from dataclasses import asdict, is_dataclass
from typing import Dict, Type, Any, Optional, Callable
from .base_tool import BaseTool, ToolResult, ToolStatus
from .weather_tool import WeatherTool
from .screenshot_tool import ScreenshotTool
from .camera_tool import CameraTool


# ============ Agent Tools 适配器 ============

class AgentToolAdapter(BaseTool):
    """
    将 agent_tools_lib 的工具适配为项目的 BaseTool 接口

    agent_tools 的 Tool 接口:
    - name: str
    - description: str
    - input_schema: Dict
    - run(input: Dict) -> Dict

    项目的 BaseTool 接口:
    - name: str
    - description: str
    - parameters_schema: Dict
    - execute(**kwargs) -> ToolResult
    """

    _user_facing_descriptions = {
        "shell": "正在执行系统命令",
        "http_request": "正在发送网络请求",
        "file": "正在操作文件",
        "web_browser": "正在浏览网页",
        "web_search": "正在搜索网络",
        "documentation_check": "正在查阅文档",
        "package_manager": "正在管理软件包",
        "advanced_file": "正在操作文件",
        "code_runner": "正在运行代码",
    }

    def __init__(self, agent_tool):
        self._agent_tool = agent_tool
        self.name = agent_tool.name
        self.description = agent_tool.description
        self.single_use = getattr(agent_tool, 'single_use', False)
        # 安全获取 input_schema，处理递归问题
        try:
            self.parameters_schema = agent_tool.input_schema
        except RecursionError:
            # 如果 input_schema 有递归问题，使用空schema
            self.parameters_schema = {"type": "object", "properties": {}, "required": []}
        except Exception as e:
            print(f"Warning: Could not get input_schema for {agent_tool.name}: {e}")
            self.parameters_schema = {"type": "object", "properties": {}, "required": []}

    def get_user_facing_description(self, **kwargs) -> str:
        return self._user_facing_descriptions.get(self.name, f"正在使用{self.name}")

    async def execute(self, **kwargs) -> ToolResult:
        """执行工具"""
        try:
            # agent_tools_lib 中大多数工具是 run(input_dict)，少数工具是
            # run(tool_call_id, **kwargs)。这里按签名兼容两类实现。
            result = self._run_agent_tool(kwargs)

            # 解析结果
            if hasattr(result, "content") and not isinstance(result, dict):
                content = getattr(result, "content", "")
                if isinstance(content, str) and content.startswith("Error:"):
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        content="",
                        error=content[6:].strip()
                    )
                is_error = getattr(result, "is_error", False)
                status = ToolStatus.ERROR if is_error else ToolStatus.SUCCESS
                return ToolResult(
                    status=status,
                    content="" if is_error else str(content),
                    error=str(content) if is_error else None
                )

            if is_dataclass(result):
                result = asdict(result)

            if isinstance(result, dict):
                content = result.get("content", "")
                if isinstance(content, dict):
                    # 处理嵌套的 content
                    if content.get("status") == "error":
                        return ToolResult(
                            status=ToolStatus.ERROR,
                            content="",
                            error=content.get("message", str(content))
                        )
                    return ToolResult(
                        status=ToolStatus.SUCCESS,
                        content=content.get("message", str(content))
                    )
                elif isinstance(content, str):
                    # 检查是否是错误
                    if content.startswith("Error:"):
                        return ToolResult(
                            status=ToolStatus.ERROR,
                            content="",
                            error=content[6:].strip()
                        )
                    return ToolResult(
                        status=ToolStatus.SUCCESS,
                        content=content
                    )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=str(result)
            )

        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"工具执行失败: {str(e)}"
            )

    def _run_agent_tool(self, kwargs: Dict) -> object:
        """兼容 agent_tools_lib 的两种 run() 调用约定。"""
        signature = inspect.signature(self._agent_tool.run)
        params = list(signature.parameters.values())

        # Bound method 的 self 不在 signature 中。形如 run(tool_call_id, **kwargs)
        # 的工具需要一个占位 ID，并把参数展开传入。
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        first_name = params[0].name if params else ""
        if accepts_kwargs or first_name in {"tool_call_id", "tool_use_id"}:
            return self._agent_tool.run(f"{self.name}_call", **kwargs)

        return self._agent_tool.run(kwargs)


def register_agent_tools() -> Dict[str, BaseTool]:
    """
    注册 agent_tools_lib 的原生工具

    Returns:
        工具名称到工具实例的映射
    """
    tools = {}

    # ShellTool
    try:
        from behavior_scheduler.agent_tools_lib.src.tools.shell_tool import ShellTool
        tools["shell"] = AgentToolAdapter(ShellTool())
    except ImportError as e:
        print(f"Warning: Could not import ShellTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.requests_tool import RequestsTool
        tools["http_request"] = AgentToolAdapter(RequestsTool())
    except ImportError as e:
        print(f"Warning: Could not import RequestsTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.file_tool import FileTool
        tools["file"] = AgentToolAdapter(FileTool())
    except ImportError as e:
        print(f"Warning: Could not import FileTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.web_browser_tool import WebBrowserTool
        tools["web_browser"] = AgentToolAdapter(WebBrowserTool())
    except ImportError as e:
        print(f"Warning: Could not import WebBrowserTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.web_search_tool import WebSearchTool
        tools["web_search"] = AgentToolAdapter(WebSearchTool())
    except ImportError as e:
        print(f"Warning: Could not import WebSearchTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.doc_check_tool import DocCheckTool
        tools["documentation_check"] = AgentToolAdapter(DocCheckTool())
    except ImportError as e:
        print(f"Warning: Could not import DocCheckTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.package_manager_tool import PackageManagerTool
        tools["package_manager"] = AgentToolAdapter(PackageManagerTool())
    except ImportError as e:
        print(f"Warning: Could not import PackageManagerTool: {e}")

    try:
        from behavior_scheduler.agent_tools_lib.src.tools.advanced_file_tool import AdvancedFileTool
        tools["advanced_file"] = AgentToolAdapter(AdvancedFileTool())
    except ImportError as e:
        print(f"Warning: Could not import AdvancedFileTool: {e}")

    # CodeRunnerTool
    try:
        from behavior_scheduler.agent_tools_lib.src.tools.code_runner_tool import CodeRunnerTool
        tools["code_runner"] = AgentToolAdapter(CodeRunnerTool())
    except ImportError as e:
        print(f"Warning: Could not import CodeRunnerTool: {e}")

    return tools


def register_all_tools(
    glm_api_key: str = "",
    glm_api_url: str = "",
    glm_model: str = "glm-4v-flash",
    chrome_user_data: str = None,
    target_qq: str = None,
    camera_device_name: str = "TIGA Device",
    include_agent_tools: bool = True
) -> Dict[str, BaseTool]:
    """
    注册所有可用工具

    Args:
        glm_api_key: GLM API密钥（保留兼容，视觉工具现用 call_vision_api 统一入口）
        glm_api_url: GLM API地址（保留兼容，未使用）
        glm_model: GLM模型名称（保留兼容，未使用）
        chrome_user_data: Chrome用户数据目录（保留兼容，未使用）
        target_qq: 保留兼容，未使用
        camera_device_name: 摄像头设备名称
        include_agent_tools: 是否包含 agent_tools_lib 的原生工具

    Returns:
        工具名称到工具实例的映射
    """
    tools = {}

    # ============ 自定义工具 ============

    # 天气工具（无需配置）
    tools["get_weather"] = WeatherTool()

    # 截屏工具和摄像头工具（使用统一视觉入口 call_vision_api，无需传 API key）
    if glm_api_key:
        tools["take_screenshot"] = ScreenshotTool()

        # 摄像头工具
        tools["capture_camera"] = CameraTool(device_name=camera_device_name)

    # ============ agent_tools_lib 原生工具 ============

    if include_agent_tools:
        agent_tools = register_agent_tools()
        tools.update(agent_tools)

    # ============ 第三态能力门卫（小红书只读，Wander v3 临时授权） ============
    # 普通工具目录只保留稳定的通用代理；真实 handler 由 Wander v3 运行时
    # 按临时授权解析，绝不作为 Pro/Flash 工具暴露。导入保持局部，避免
    # 未启用漫想时改变启动依赖。
    try:
        from wander_manager.xhs_third_state import XhsThirdStateTool
        tools[XhsThirdStateTool.name] = XhsThirdStateTool()
    except Exception as exc:
        print(f"第三态能力门卫注册失败: {type(exc).__name__}")

    return tools


# 手机端排除的工具（PC 专属硬件/平台依赖）
MOBILE_EXCLUDED_TOOLS = {"take_screenshot", "capture_camera"}

# 全局中继回调：由 scheduler 注入，用于向手机发送工具执行请求
_mobile_relay_callback: Optional[Callable] = None  # 保留：tools.py 内部引用


def set_mobile_relay_callback(cb: Optional[Callable]):
    """设置手机工具中继回调（由 BehaviorScheduler 调用）"""
    global _mobile_relay_callback
    _mobile_relay_callback = cb
    # 同步到 shared_state——外部模块从 shared_state 读取，不再 depend on tools
    try:
        from mirrow_core.shared_state import set_mobile_relay_callback as _set_shared
        _set_shared(cb)
    except ImportError:
        pass


async def push_mobile_notification(title: str, body: str) -> bool:
    """🔥 自动推送通知到用户手机。fire-and-forget，失败静默跳过。

    Args:
        title: 通知标题
        body: 通知内容（会自动截断到120字符）

    Returns:
        True 如果推送成功，False 如果手机未连接或推送失败
    """
    import asyncio as _asyncio_mobile_push
    import logging as _logging_mobile_push
    _logger_mpn = _logging_mobile_push.getLogger(__name__)
    if not _mobile_relay_callback:
        _logger_mpn.debug("push_mobile_notification: 无 mobile relay callback，跳过")
        return False
    try:
        import uuid as _uuid_mobile
        truncated_body = body[:120] + ("…" if len(body) > 120 else "")
        result = await _asyncio_mobile_push.wait_for(
            _mobile_relay_callback(
                str(_uuid_mobile.uuid4()), "mobile_notify",
                {"title": title, "body": truncated_body}
            ),
            timeout=15.0
        )
        return result.get("success", False) if isinstance(result, dict) else False
    except Exception as e:
        _logger_mpn.warning(f"push_mobile_notification 失败 (title={title}): {e}")
        return False


class MobileRemoteTool(BaseTool):
    """通过 WebSocket 在手机端远程执行的工具基类。

    不本地执行，而是将请求通过 WebSocket 发送到手机，
    手机 Capacitor 插件执行后将结果回传。
    """
    name: str = ""
    description: str = ""
    parameters_schema: Dict[str, Any] = {}

    async def execute(self, **kwargs) -> ToolResult:
        if not _mobile_relay_callback:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error="手机未连接（Tailscale 可能断开）"
            )
        request_id = str(_uuid.uuid4())
        try:
            result_data = await _mobile_relay_callback(request_id, self.name, kwargs)
            # Extract image and health data from mobile result
            extra = result_data.get("extra_data") or {}
            if result_data.get("image_base64"):
                extra["image_base64"] = result_data["image_base64"]
            if result_data.get("data"):
                extra["data"] = result_data["data"]
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=result_data.get("content", ""),
                extra_data=extra if extra else None,
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=str(e),  # 保留完整诊断信息，供上层日志和前端展示
                error=str(e),
            )


def get_tools_description(tools: Dict[str, BaseTool]) -> str:
    """获取所有工具的描述（用于LLM提示词）"""
    descriptions = []
    for name, tool in tools.items():
        desc = f"- {name}: {tool.description}"
        if tool.parameters_schema.get("properties"):
            params = list(tool.parameters_schema["properties"].keys())
            desc += f" (参数: {', '.join(params)})"
        descriptions.append(desc)
    return "\n".join(descriptions)


def get_visible_tool_capabilities(
    *,
    glm_api_key: Optional[str] = None,
    include_agent_tools: bool = True,
) -> list[dict[str, Any]]:
    """Return the current visible tool facts without executing anything.

    The brain-document generator must not construct a second, hand-maintained
    tool list.  The normal registration function is therefore used as the
    authority, but only its metadata is read here.  Constructors in this
    registry are deliberately lazy with respect to cameras, Bluetooth,
    network calls and device actions; none of those actions happen until
    ``BaseTool.execute`` is called.

    ``glm_api_key`` is only a registration feature flag.  Its value is never
    returned.  When omitted, the already-loaded process environment decides
    whether the visual tools are part of the real registry.
    """

    import os

    registration_key = glm_api_key
    if registration_key is None:
        registration_key = os.getenv("GLM_API_KEY", "")

    try:
        registered = register_all_tools(
            glm_api_key=registration_key,
            include_agent_tools=include_agent_tools,
        )
    except Exception:
        # A missing optional dependency must not make self-reflection claim
        # that no capabilities exist.  The normal registry may still be
        # queried in a fully configured process; this path is a safe empty
        # snapshot for the documentation helper.
        return []

    # 开发者工具库（agent_tools_lib）的通用工具属于宿主内部能力，
    # 不进入 AI 可见的能力文档；web_search 保留（对外搜索能力）。
    _HIDDEN_TOOLS = {
        "shell", "http_request", "file", "web_browser",
        "documentation_check", "package_manager", "advanced_file", "code_runner",
    }

    capabilities: list[dict[str, Any]] = []
    for name, tool in registered.items():
        if name in _HIDDEN_TOOLS:
            continue
        if not getattr(tool, "visible_to_pro", True):
            continue
        description = (
            getattr(tool, "flash_description", "")
            or getattr(tool, "description", "")
            or "已注册行动能力"
        )
        capabilities.append(
            {
                "name": str(name),
                "description": " ".join(str(description).split())[:180],
                "source": "behavior_scheduler registration",
                "freshness": "current registration",
            }
        )
    return capabilities
