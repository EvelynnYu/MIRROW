# 行为调度器主逻辑
#
# 架构层级：
# ┌─────────────────────────────────────────────────────────┐
# │                 Pre-tool 前置拦截层 (无 LLM)              │
# │   关键词直接匹配 _DIRECT_INTENT_PATTERNS → 先执行工具     │
# │                  结果注入 messages 再给 Pro               │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    决策层 (Pro LLM)                      │
# │       生成自然语言回复（可能附带 TOOL_CALL 快速路径）      │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                 意图提取层 (Flash LLM)                   │
# │     Pro 未输出 TOOL_CALL 时，Flash 从 NL 提取意图+参数    │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    标准化层 (ToolStandardizer)           │
# │       规范化 JSON → 填充默认值 → 校验 schema             │
# └─────────────────────────────────────────────────────────┘
#                           ↓
# ┌─────────────────────────────────────────────────────────┐
# │                    执行层 (Tools)                        │
# │              具体工具的实现与执行                         │
# └─────────────────────────────────────────────────────────┘
#
# 三通道设计：
# - Pre-tool 前置路径：关键词匹配 → 先执行 → 真结果注入（覆盖 9 个工具，无 LLM）
# - Pro TOOL_CALL 快速路径：Pro 输出了 TOOL_CALL → 直接解析执行
# - Flash 提取路径：Pro 未输出 → 关键词预筛 → Flash 提取参数 → 执行
# 三条路径归一为同一标准化→执行流程。

from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import asyncio
import json
import logging
import os
import re
from mirrow_core.token_tracker import log_token_usage
from datetime import datetime, timedelta

from .base_tool import BaseTool, ToolResult, ToolStatus
try:
    from .tools import register_all_tools, get_tools_description, get_ledger_type_options, MOBILE_EXCLUDED_TOOLS
except ImportError:
    # 开源精简版 tools.py 无账本/手机排除——降级为空，不影响主流程
    get_ledger_type_options = lambda: []
    MOBILE_EXCLUDED_TOOLS = set()
from .command_layer import (
    CommandParser,
    CommandExecutor,
    CommandStatus,
    Command
)
from .agent_tools_adapter import AgentToolsAdapter
from .tool_standardizer import ToolCallStandardizer
from .intent_parser import _REMOTE_FALLBACK_KEYWORDS, _NIGHT_GATED_KEYWORDS

from mirrow_core.scheduler_log import write_log as _log_context_scheduler

# 手机端 PC 工具 → 手机工具重定向映射（Pre-tool 和 Flash 提取两处共用）
_MOBILE_REDIRECT_MAP = {
    "capture_camera": ("mobile_camera", {"facing": "back"}),
    "take_screenshot": ("mobile_screenshot", {}),
}
from mirrow_core.time_utils import now_iso
from mirrow_core.truncation_config import get_truncation_limit

# ── 截断参数快捷取值 ──
def _cap(key: str, fallback: int) -> int:
    """取截断上限，0=无限制 → 返回极大哨兵值使切片不截断"""
    v = get_truncation_limit(key)
    if v == 0:
        return 10**9  # 无限制：极大值，str[:10**9] = 完整字符串
    return v if v > 0 else fallback


logger = logging.getLogger(__name__)


class SchedulerState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_EXECUTING = "tool_executing"
    WAITING_USER = "waiting_user"
    COMPLETE = "complete"


@dataclass
class SchedulerResult:
    """调度器返回结果"""
    final_response: str
    tool_calls: List[Dict[str, Any]]
    state: SchedulerState
    need_user_input: bool
    thinking_chain: List[Dict[str, str]]
    pending_intent_check: bool = False
    # 每轮的 NL+thinking 对，用于前端按消息粒度展示思考过程
    intermediate_messages: List[Dict[str, str]] = field(default_factory=list)


# 模块级提取函数（供 _DIRECT_INTENT_PATTERNS lambda 使用，避免 Python 类作用域闭包缺陷）

def _extract_search_query(msg: str) -> str:
    """从用户消息中提取搜索关键词。"""
    for prefix in ["帮我搜一下", "帮我搜", "搜一下", "搜索一下", "帮我查一下", "上网查一下",
                      "帮我查查", "帮我搜搜", "搜一搜", "查一查", "搜搜看", "查查看"]:
        if prefix in msg:
            rest = msg.split(prefix, 1)[1].strip()
            return re.sub(r'[？?！!。，,、\s]+$', '', rest)[:50] if rest else ""
    # 无前缀匹配时：去尾部标点 + 去常见语气前缀，保留完整搜索意图
    cleaned = re.sub(r'[？?！!。，,、\s]+$', '', msg)
    cleaned = re.sub(r'^(那个|帮我|我想|给我|你帮我|那你帮我|那帮我)\s*', '', cleaned)
    return cleaned[:50] if cleaned else msg[:50]


def _extract_ledger_keyword(msg: str) -> str:
    """从删除请求中提取关键词。'划掉零点前睡觉那一笔'→'零点前睡觉'"""
    for prefix in ["划掉", "划账", "删掉", "删除", "销账", "删账", "划",
                      "消账", "清账", "消了", "销了"]:
        if prefix in msg:
            rest = msg.split(prefix, 1)[1].strip()
            rest = re.sub(r'(那.?笔|那.?条|这.?笔|这.?条|那个|这个).*$', '', rest)
            rest = rest.strip('，。；！？、\n\r ')
            return rest[:15] if rest else ""
    return ""




def _extract_voice_text(msg: str) -> str:
    """从用户消息中提取要用语音说的内容。"""
    for prefix in ["我想用语音说", "让我用语音说", "说句话", "念出来",
                   "念给我听", "说给你听", "听我说"]:
        if prefix in msg:
            rest = msg.split(prefix, 1)[1].strip()
            if rest:
                rest = re.sub(r'^[：:，。；！？、\s]+', '', rest)
                return rest[:60] if rest else ""
    return ""


class BehaviorScheduler:
    """
    行为调度器 - 协调决策层、命令层和执行层

    架构说明：
    1. 决策层：Pro LLM 生成自然语言回复（可能附带 TOOL_CALL 快速路径）
    2. 意图提取层：Pro 未输出 TOOL_CALL 时，Flash 从 NL 提取意图+参数
    3. 标准化层：规范化 JSON → 填充默认值 → 校验 schema
    4. 执行层：具体工具的实现

    双路径设计：
    - 快速路径：Pro 输出了 TOOL_CALL → 直接解析执行
    - Flash 路径：Pro 未输出 → 关键词预筛 → Flash 提取参数 → 执行
    """

    MAX_ROUNDS = 5
    NIGHT_FALLBACK_THRESHOLD = 3  # 连续 N 轮未提玩具时触发 Flash 兜底

    def __init__(
        self,
        call_llm_func: Callable,
        glm_api_key: str = "",
        glm_api_url: str = "",
        glm_model: str = "glm-4v-flash",
        chrome_user_data: str = None,
        target_qq: str = None,
        use_agent_tools: bool = True,
        llm_provider: str = "deepseek",
        llm_api_key: str = None,
        llm_base_url: str = None,
        llm_model: str = None,
        camera_device_name: str = "TIGA Device",
        final_reply_caller: Callable = None,  # 保留兼容，新流程不再使用
        # Pro 模型用于首轮聊天+工具决策
        pro_api_key: str = None,
        pro_model: str = None,
        pro_base_url: str = None,
    ):
        # 决策层组件
        self.call_llm = call_llm_func

        # 执行层组件（视觉工具现用 call_vision_api 统一入口，不再传 API key）
        self.tools = register_all_tools(
            glm_api_key=glm_api_key,
            glm_api_url=glm_api_url,
            glm_model=glm_model,
            chrome_user_data=chrome_user_data,
            target_qq=target_qq,
            camera_device_name=camera_device_name
        )
        self.tools_description = get_tools_description(self.tools)

        # ── 手机端工具中继 ──
        self._pending_mobile_requests: Dict[str, asyncio.Future] = {}
        self._mobile_sender: Optional[Callable] = None  # 由 main.py WS handler 注入
        from .tools import set_mobile_relay_callback as _set_relay

        async def _relay_to_phone(request_id: str, tool_name: str, params: dict) -> dict:
            """向手机发送工具执行请求，等待结果返回"""
            if not self._mobile_sender:
                _log_context_scheduler(f"MOBILE_RELAY ❌ _mobile_sender is None")
                raise RuntimeError("手机 WebSocket 未连接")
            _log_context_scheduler(f"MOBILE_RELAY → 发送: tool={tool_name} request_id={request_id[:8]}... pending={len(self._pending_mobile_requests)}")
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            self._pending_mobile_requests[request_id] = future
            try:
                await self._mobile_sender({
                    "type": "mobile_tool_request",
                    "request_id": request_id,
                    "tool": tool_name,
                    "params": params,
                })
                _log_context_scheduler(f"MOBILE_RELAY → 已发送到前端，等待回传...")
                result = await asyncio.wait_for(future, timeout=30.0)
                _log_context_scheduler(f"MOBILE_RELAY ✅ 收到回传: tool={tool_name} content={str(result.get('content', ''))[:80]}")
                return result
            except asyncio.TimeoutError:
                _log_context_scheduler(f"MOBILE_RELAY ⏰ 超时(30s): tool={tool_name}")
                raise
            except Exception as e:
                _log_context_scheduler(f"MOBILE_RELAY ❌ 异常: tool={tool_name} error={e}")
                # sender 可能指向已关闭的 WebSocket（被 wander WS 覆盖后断开）
                # 清除它，让下次连接能设新的 sender
                if "close message" in str(e).lower() or "closed" in str(e).lower():
                    self._mobile_sender = None
                    _log_context_scheduler(f"MOBILE_RELAY ⚠️ 已清除失效的 _mobile_sender（WebSocket已关闭）")
                raise
            finally:
                self._pending_mobile_requests.pop(request_id, None)

        _set_relay(_relay_to_phone)
        self._relay_to_phone = _relay_to_phone  # 暴露给 main.py 用于 phone_browser

        # 👁️ Eyes 感官工具依赖注入（开源精简版 tools.py 无此函数则跳过）
        try:
            from .tools import inject_eyes_dependencies as _inject_eyes
            _inject_eyes(
                self.tools,
                flash_llm_func=self._call_flash_for_fusion,
                mobile_relay=_relay_to_phone,
            )
        except ImportError:
            pass

        # 命令层组件（传递LLM函数用于意图识别）
        self.command_parser = CommandParser(self.tools, call_llm_func=call_llm_func)
        self.command_executor = CommandExecutor(self.tools)

        # 工具调用标准化层（在 agent_tools 流程中校验并补齐参数）
        self.tool_standardizer = ToolCallStandardizer(self.tools)

        # LLM 适配器（Pro 用于对话+工具决策，Flash 用于意图提取）
        self.use_agent_tools = use_agent_tools
        self.agent_tools_adapter = None  # Flash，用于意图提取 + 后续轮次
        self.pro_adapter = None           # Pro，用于首轮聊天+工具决策

        if use_agent_tools:
            # Flash 适配器（便宜快速，工具多轮调用）
            self._init_agent_tools_adapter(
                provider=llm_provider,
                api_key=llm_api_key,
                base_url=llm_base_url,
                model=llm_model
            )
            # Pro 适配器（首轮聊天+工具决策，支持 thinking）
            if pro_api_key or pro_model:
                self._init_pro_adapter(
                    provider=llm_provider,
                    api_key=pro_api_key or llm_api_key,
                    base_url=pro_base_url or llm_base_url,
                    model=pro_model or llm_model
                )

        # 状态管理
        self._state = SchedulerState.IDLE
        self._status_callback: Optional[Callable] = None
        self._night_rounds_without_toy_mention = 0
        self._pushed_preview_nl = ''  # 已推送的预览 NL，用于 follow-up 去重
        self._pushed_preview_msg_id = ''  # 预览推送用的 message_id，与 _pushed_preview_nl 同生共死（B1 落盘 ID 复用）

        # Pro 模型最终回复调用器（新流程首轮 Pro 已出 NL，不再需要）
        self.final_reply_caller = final_reply_caller

    def _init_agent_tools_adapter(
        self,
        provider: str = "deepseek",
        api_key: str = None,
        base_url: str = None,
        model: str = None
    ):
        """初始化 agent_tools 适配器"""
        try:
            logger.info(f"Initializing agent_tools adapter: provider={provider}, model={model}")
            if provider == "deepseek":
                actual_model = model or "deepseek-v4-pro"
                logger.info(f"Creating DeepSeek wrapper with model: {actual_model}")
                self.agent_tools_adapter = AgentToolsAdapter.for_deepseek(
                    api_key=api_key,
                    model=actual_model
                )
            elif provider == "glm":
                self.agent_tools_adapter = AgentToolsAdapter.for_glm(
                    api_key=api_key,
                    model=model or "glm-4"
                )
            elif provider == "ollama":
                self.agent_tools_adapter = AgentToolsAdapter.for_ollama(
                    model=model or "llama3.1",
                    base_url=base_url or "http://localhost:11434/v1"
                )
            else:
                # 通用OpenAI兼容
                self.agent_tools_adapter = AgentToolsAdapter.for_openai_compatible(
                    api_key=api_key,
                    base_url=base_url,
                    model=model
                )

            # 注册所有工具
            self.agent_tools_adapter.register_tools(list(self.tools.values()))
            logger.info(f"agent_tools adapter initialized with provider: {provider}")

        except Exception as e:
            logger.warning(f"Failed to init agent_tools adapter: {e}, falling back to legacy mode")
            self.use_agent_tools = False
            self.agent_tools_adapter = None

    def _init_pro_adapter(
        self,
        provider: str = "deepseek",
        api_key: str = None,
        base_url: str = None,
        model: str = None
    ):
        """初始化 Pro adapter（首轮聊天+工具决策，支持 thinking）"""
        try:
            logger.info(f"Initializing Pro adapter: provider={provider}, model={model}")
            if provider == "deepseek":
                actual_model = model or "deepseek-v4-pro"
                print(f"[SCHEDULER] 创建 Pro adapter | model={actual_model} | api_key={'已设置' if api_key else '未设置'}")
                self.pro_adapter = AgentToolsAdapter.for_deepseek(
                    api_key=api_key,
                    model=actual_model
                )
            else:
                self.pro_adapter = AgentToolsAdapter.for_openai_compatible(
                    api_key=api_key,
                    base_url=base_url,
                    model=model
                )
            # 注册工具到 Pro adapter
            self.pro_adapter.register_tools(list(self.tools.values()))
            print(f"[SCHEDULER] Pro adapter 初始化成功 | model={actual_model}")
            logger.info(f"Pro adapter initialized with model: {actual_model}")
        except Exception as e:
            print(f"[SCHEDULER] Pro adapter 初始化失败: {e}")
            logger.warning(f"Failed to init Pro adapter: {e}, will fall back to Flash for first round")
            self.pro_adapter = None

    async def _call_flash_for_fusion(self, prompt: str) -> str:
        """用 Flash 模型（温度=0）处理单条文本融合任务，返回纯文本。供 Eyes 等感官工具调用。"""
        adapter = self.agent_tools_adapter
        if not adapter:
            raise RuntimeError("Flash adapter 未初始化")
        result = await adapter.execute(
            user_input=prompt,
            conversation_history=[],
            system_prompt="你是数据分析模块，请直接输出分析结果，不要使用工具调用格式。"
        )
        return result.get("natural_language") or result.get("content", "")

    def set_status_callback(self, callback: Callable):
        self._status_callback = callback

    def set_image_model(self, model: str):
        """切换所有视觉工具的图像分析模型"""
        for tool in self.tools.values():
            if hasattr(tool, 'set_image_model'):
                tool.set_image_model(model)

    async def _notify_status(self, status: str, data: Dict = None):
        if self._status_callback:
            try:
                await self._status_callback(status, data or {})
            except Exception:
                pass  # 客户端已断开，静默忽略，不中断 LLM 处理管道

        # 自动推送 AI 的回复到手机通知栏（在 WS 仍活跃时发送）
        if status == "llm_reply" and data and data.get("content"):
            content = data["content"].strip()
            if content and len(content) > 2:  # 过滤极短回复
                try:
                    from .tools import push_mobile_notification as _pmn
                    import asyncio as _asyncio_pmn
                    _asyncio_pmn.ensure_future(_pmn("AI", content[:120]))
                except Exception:
                    pass

    async def _push_llm_reply(self, content: str, reasoning: str = None):
        """推送 llm_reply 到前端（内容推送的唯一通道）。"""
        if not content or not content.strip():
            return
        sid = getattr(self, '_current_session_id', '')
        msg_id = f"msg_{sid}_{now_iso()}" if sid else f"msg_{now_iso()}"
        self._last_push_message_id = msg_id  # 供 _persist_chat_result 取用，确保推送与落盘 ID 一致
        data = {
            "content": content.strip(),
            "message_id": msg_id
        }
        if reasoning:
            data["reasoning"] = reasoning
        await self._notify_status("llm_reply", data)

    @staticmethod
    def _extract_reasoning(content: str) -> str:
        """提取 TOOL_CALL: 之前的自然语言部分（用于清除格式标记）"""
        # 先尝试提取 NATURAL_LANGUAGE: 之后的内容（兼容 LLM 输出的双轨格式）
        nl_pattern = r'NATURAL[_\s]LANGUAGE\s*:\s*\n?'
        nl_match = re.search(nl_pattern, content)
        if nl_match:
            content = content[nl_match.end():].strip()

        marker = "TOOL_CALL:"
        if marker in content:
            return content[:content.find(marker)].strip()
        pattern = r'\[TOOL_CALL\]'
        match = re.search(pattern, content)
        if match:
            return content[:match.start()].strip()
        return content.strip()

    @classmethod
    def _safe_nl(cls, natural_language: str, assistant_content: str) -> str:
        """获取清洗后的自然语言：优先用 NL，为空时从 raw content 提取并清洗语音标记"""
        nl = cls._clean_voice_markers(natural_language)
        if nl:
            return cls._clean_frontend_content(nl)
        extracted = cls._extract_reasoning(assistant_content)
        return cls._clean_frontend_content(cls._clean_voice_markers(extracted))

    @staticmethod
    def _build_tool_narrative(tools_label: str, multi: bool = False) -> str:
        """工具轮情境锚定（单/多工具路径共用，trap 47 同步保障）

        续写模式：Pro 最擅长续写，引导"接着往下说"比"收尾/补充"更自然。
        配合 COMPLETE 路径段落级 fuzzy 去重砍掉重叠段落。
        """
        _tools = tools_label
        _results_loc = "各工具结果在下一条 system 消息里" if multi else "结果在下一条 system 消息里"
        return (
            f"你刚刚已经回复了用户——她都看到了。"
            f"{_tools} 刚执行完，{_results_loc}。\n"
            f"接着你刚才的话往下说。"
            f"如果还需要其他工具 → TOOL_CALL。"
            f"别输出「我看看」「我查查」这类没带 TOOL_CALL 的预告。"
        )

    @staticmethod
    def _dedup_paragraphs(text: str, messages: list, threshold: float = 0.5) -> str:
        """段落级 fuzzy 去重：把 messages 中所有 assistant 消息段落打平成比对池，
        text 逐段取跨池 max ratio，≥threshold 的段落砍掉。比对池为空时原样返回。
        """
        if not text or not messages:
            return text
        import difflib as _difflib
        _pool = []
        for _m in messages:
            if _m.get("role") == "assistant":
                _mc = (_m.get("content") or "").strip()
                if _mc and len(_mc) >= 20:
                    _pool.extend(p.strip() for p in _mc.split("\n\n") if p.strip())
        if not _pool:
            return text
        _paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not _paras:
            return text
        _kept = []
        for _fp in _paras:
            _best = max((_difflib.SequenceMatcher(None, _fp, _ip).ratio()
                         for _ip in _pool), default=0)
            if _best < threshold:
                _kept.append(_fp)
        if _kept:
            return "\n\n".join(_kept)
        # 全部被砍 → 保留最后一段兜底
        return _paras[-1] if _paras else text

    def _build_system_prompt(self, night_mode_enabled: bool = False) -> str:
        """构建决策层系统提示词"""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"""你是一个智能助手，可以使用以下工具：

当前时间: {current_time}

{self.tools_description}

## 工具调用规则

当需要使用工具时，先告诉用户你要做什么，然后输出工具调用：
[TOOL_CALL]
{{"tool": "工具名", "parameters": {{"参数名": "参数值"}}}}
[/TOOL_CALL]

### 重要规则：
1. 先说话，再调用工具
2. 每次只能调用一个工具
3. 工具名必须是上述列表中存在的工具
4. 不需要工具时直接回复用户
5. 工具返回结果后，根据结果继续调用工具或直接回复用户
6. 用户明确要求看摄像头/打开摄像头/拍照/自拍/看看周围/看她在干嘛/看用户时，必须调用 eyes
7. {"用户提到蓝牙玩具/情趣玩具/振动玩具/Lush/控制玩具/连接玩具/扫描蓝牙设备时，必须调用 toys" if night_mode_enabled else "用户提到蓝牙玩具/情趣玩具等时，提醒用户开启深夜模式（点击页面爱心图标），不要调用 toys"}

### 示例：
用户："帮我搜一下Python教程"
助手：让我搜一下~
[TOOL_CALL]
{{"tool": "web_search", "parameters": {{"query": "Python教程"}}}}
[/TOOL_CALL]"""


    def _extract_camera_prompt(self, user_message: str) -> Optional[str]:
        """从用户消息中识别明确的摄像头查看意图。"""
        text = (user_message or "").strip()
        lowered = text.lower()

        has_camera_word = any(word in lowered for word in [
            "摄像头", "camera", "webcam", "镜头"
        ])
        has_view_intent = any(word in lowered for word in [
            "看", "看看", "打开", "开一下", "试试", "试试看", "拍", "捕获",
            "scan", "capture", "open"
        ])
        implicit_view = any(phrase in lowered for phrase in [
            "看看我", "看一下我", "看看桌面", "看看房间", "看看环境",
            "看到空的桌面", "能看到"
        ])

        if not ((has_camera_word and has_view_intent) or implicit_view):
            return None

        if any(word in lowered for word in ["桌面", "desk", "table"]):
            return "请通过摄像头观察画面，重点描述桌面上有什么、是否有人在画面中。"
        if any(word in lowered for word in ["我", "脸", "表情", "本人"]):
            return "请通过摄像头观察用户是否在画面中，并描述可见的人物、表情和环境。"
        return "请描述摄像头画面中的内容，包括人物、环境、物品等。"

    # ── 手机工具中继接口 ──

    def set_mobile_sender(self, sender: Optional[Callable]):
        """注入 WebSocket 发送回调，用于向手机推送工具执行请求"""
        self._mobile_sender = sender

    def handle_mobile_tool_result(self, request_id: str, success: bool, result: dict, error: str):
        """处理手机回传的工具执行结果"""
        rid = request_id[:8] if request_id else "?"
        _log_context_scheduler(
            f"MOBILE_RELAY ← 收到回传: request_id={rid} success={success} "
            f"content={str(result.get('content', ''))[:80] if result else 'N/A'} "
            f"pending_count={len(self._pending_mobile_requests)}"
        )
        future = self._pending_mobile_requests.get(request_id)
        if future and not future.done():
            if success:
                _log_context_scheduler(f"MOBILE_RELAY ← future.set_result (success)")
                future.set_result(result)
            else:
                # 把 result.content 也带进异常，防止诊断信息丢失（手机端失败时 diagnostic 在 content 里）
                extra = result.get("content", "") if result else ""
                detail = error or extra or "未知错误"
                _log_context_scheduler(f"MOBILE_RELAY ← future.set_exception: {detail[:200]}")
                future.set_exception(RuntimeError(detail))
        elif future:
            _log_context_scheduler(f"MOBILE_RELAY ← future already done, ignoring")
        else:
            _log_context_scheduler(f"MOBILE_RELAY ← no matching future for {rid} (可能已超时清理)")

    async def process_message(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None,
        night_mode_enabled: bool = False,
        platform: str = "pc",
        session_id: str = "",
    ) -> SchedulerResult:
        """
        处理用户消息，Pro 生成 NL → 关键词预筛 → Flash 提取意图+参数 → 执行。

        Args:
            user_message: 用户消息
            conversation_history: 对话历史
            system_prompt: 自定义系统提示词
            night_mode_enabled: 是否启用深夜模式
            platform: "pc" | "mobile" — 手机端排除 PC 专属工具

        Returns:
            SchedulerResult: 调度结果
        """
        # 纪念日自动检测：在用户消息上并行检测，不阻塞正常回复（决策 F）
        self._maybe_detect_memorial(user_message)

        # 存 session_id 供 _push_llm_reply 生成与 _persist_chat_result 一致的 im_ ID
        self._current_session_id = session_id

        if self.use_agent_tools and self.agent_tools_adapter:
            return await self._process_with_agent_tools(
                user_message, conversation_history, system_prompt, night_mode_enabled, platform, session_id
            )
        else:
            return await self._process_legacy(
                user_message, conversation_history, system_prompt
            )

    def _maybe_detect_memorial(self, user_message: str):
        """
        纪念日自动检测（决策 F）：在用户原始消息上检测纪念日意图。
        与 LLM 正常回复并行执行，不阻塞用户看到回复。

        检测流程：关键词预筛 → LLM Flash 意图确认 → 创建待确认日历事件 → WS 推送
        """
        from .intent_parser import has_memorial_signal

        if not has_memorial_signal(user_message):
            return

        # 生成去重 key
        today_mm_dd = datetime.now().strftime("%m-%d")
        title_prefix = user_message[:20].strip()
        dedup_key = f"{today_mm_dd}_{title_prefix}"

        # 检查内存去重
        try:
            from calendar_manager.manager import get_global_calendar_manager
            cal_mgr = get_global_calendar_manager()
            if cal_mgr.is_duplicate_suggestion(today_mm_dd, title_prefix):
                return
            cal_mgr.mark_suggestion_seen(today_mm_dd, title_prefix)
        except Exception:
            pass  # 日历管理器可能未初始化

        # 后台任务：LLM 意图确认 + 内容填写 + 创建事件 + WS 推送
        async def _background_detection():
            try:
                # 1. LLM Flash 意图确认
                confirm_prompt = f"""判断以下用户消息是否表达了"想把今天作为一个纪念日，以后每年都要记下来"的意图。

用户消息："{user_message[:300]}"

请只回复 JSON（不要 markdown）：
- 如果是纪念日意图：{{"intent":"memorial","title":"纪念日标题（简短）","description":"备注描述（1句话）","emotion":"情绪（开心/感动/怀念/珍惜等）"}}
- 如果不是：{{"intent":"not_memorial"}}

注意：
- 仅当用户明确表达"今天很重要/要记住/每年都要/第一次/纪念"等纪念含义时才判定 memorial
- 普通聊天、日常对话判定为 not_memorial
- title 要简短（不超过15字），description 要自然"""

                result = await self._call_flash_for_memorial(confirm_prompt)
                if not result or result.get("intent") != "memorial":
                    return

                title = result.get("title", "纪念日")[:30]
                description = result.get("description", "")[:100]
                emotion = result.get("emotion", "")

                # 2. 创建待确认的纪念日日历事件（is_active=False，决策 G）
                from calendar_manager.manager import get_global_calendar_manager
                from calendar_manager.models import CalendarEventCreateRequest

                cal_mgr = get_global_calendar_manager()
                req = CalendarEventCreateRequest(
                    title=title,
                    description=description,
                    event_type="memorial",
                    date_string=f"{datetime.now().year:04d}-{today_mm_dd}",
                    creator="K",  # Historical storage role; not a display name.
                    emotion=emotion,
                    is_active=False,  # 待用户确认
                )
                event = cal_mgr.create_event(req)

                # 3. 通过 WebSocket 推送到前端确认
                await cal_mgr.broadcast_memorial_suggestion(event)

            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"纪念日后台检测失败: {e}")

        import asyncio as _asyncio
        try:
            _asyncio.create_task(_background_detection())
        except Exception:
            pass

    async def _call_flash_for_memorial(self, prompt: str) -> dict:
        """调用 Flash LLM 进行纪念日意图确认"""
        import os
        api_key = os.getenv("DEEPSEEK_FLASH_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
        raw_url = os.getenv("DEEPSEEK_FLASH_API_URL") or os.getenv("DEEPSEEK_API_URL") or "https://api.deepseek.com/v1"
        api_url = re.sub(r'/(chat/completions|v1/chat/completions)$', '', raw_url.rstrip("/"))
        if not api_url.endswith("/v1"):
            api_url += "/v1"
        print(f"[FLASH_MEMORIAL] api_url={api_url}")
        model = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash")

        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key, base_url=api_url, timeout=15.0, max_retries=1)
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            content = response.choices[0].message.content
            asyncio.create_task(log_token_usage("memorial_detect", model, response.usage if hasattr(response, 'usage') else None))
            if not content:
                return None
            # Extract JSON
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end > start:
                return json.loads(content[start:end + 1])
            return None
        except Exception:
            return None

    def _build_flash_extraction_tools_desc(self, platform: str = "pc") -> str:
        """构建 Flash 提取用的精简工具描述。≤3000 字符。"""
        _mobile = (platform == "mobile")
        lines = []
        for name, tool in self.tools.items():
            if _mobile and name in MOBILE_EXCLUDED_TOOLS:
                continue
            if not getattr(tool, 'visible_to_pro', True):
                continue
            schema = getattr(tool, 'parameters_schema', None) or {}
            if not schema.get("properties"):
                schema = getattr(tool, 'input_schema', None) or {}
            props = schema.get("properties", {})
            required = schema.get("required", [])
            param_parts = []
            for pname, pdef in list(props.items())[:3]:
                req = "" if pname in required else "?"
                desc = pdef.get("description", "")[:6]
                if "enum" in pdef:
                    desc = "/".join(str(v)[:3] for v in pdef["enum"])
                param_parts.append(f"{req}{pname}:{desc}")
            param_str = " ".join(param_parts) if param_parts else ""
            desc = tool.description[:20].replace("\n", " ")
            lines.append(f"{name}: {desc} | {param_str}")
        result = "\n".join(lines)
        _tool_cap = _cap("flash_tools_cap", 3000)
        if len(result) > _tool_cap:
            logger.warning(f"Flash 工具描述超过 {_tool_cap} 字符 ({len(result)})，已截断，末尾工具可能不可见")
            return result[:_tool_cap]
        return result

    # ── 时间表达推断（纯规则，零 LLM）──────────────────
    _CN_NUM = {"半": 0.5, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    _TIME_PATTERNS = [
        (re.compile(r'(\d+)\s*小时后'), lambda m: timedelta(hours=int(m.group(1)))),
        (re.compile(r'(\d+)\s*分钟后'), lambda m: timedelta(minutes=int(m.group(1)))),
        (re.compile(r'半个小时后|半小时后'), lambda m: timedelta(minutes=30)),
        (re.compile(r'([半一两二三四五六七八九十]+)\s*小时后'),
         lambda m: timedelta(hours=_CN_NUM.get(m.group(1), 1))),
        (re.compile(r'等会儿|过会儿|过一阵|一会后'), lambda m: timedelta(minutes=5)),
    ]

    @classmethod
    def _infer_future_time(cls, text: str) -> Optional[str]:
        """从自然语言文本推断未来时间，返回 ISO 8601 datetime 字符串或 None。"""
        if not text:
            return None
        for pattern, delta_fn in cls._TIME_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    return (datetime.now() + delta_fn(m)).isoformat(timespec='seconds')
                except Exception:
                    continue
        return None

    def _normalize_tool_params(self, tool_name: str, params: dict) -> None:
        """标准化前归一化工具参数，修复 Flash 提取的常见格式偏差。"""
        if tool_name == "create_scheduled_task":
            tc = params.get("trigger_config")
            if isinstance(tc, str) and tc.strip():
                today = datetime.now().strftime("%Y-%m-%d")
                match = re.search(r'(\d{1,2}):?(\d{2})?', tc.strip())
                if match:
                    h, m = int(match.group(1)), int(match.group(2) or 0)
                    params["trigger_config"] = {"datetime": f"{today}T{h:02d}:{m:02d}:00"}
                    if "trigger_type" not in params:
                        params["trigger_type"] = "datetime"
        elif tool_name == "schedule_self_task":
            tc = params.get("trigger_config")
            if not tc or not tc.get("datetime"):
                text = params.get("execution_prompt", "") or params.get("title", "")
                dt = self._infer_future_time(text)
                if dt:
                    params["trigger_config"] = {"datetime": dt}
        # 自动注入 session_id：LLM 不知道当前会话 ID，manage_ledger/send_voice 需要它
        if tool_name in ("manage_ledger", "send_voice"):
            sid = getattr(self, '_current_session_id', '')
            if sid and not params.get("_session_id"):
                params["_session_id"] = sid

    # 工具执行后的反馈话术（按工具名匹配，让 Pro 知道自己完成了什么）
    # 2026-07-16 瘦身：移除"请描述/请告知/请引用"的命令式措辞——这是多轮工具链
    # Pro 重复叙述数据的首要驱动力（API测试验证：砍掉命令式后 fuzzy ratio 0.30→0.18）。
    # 保留"结果在上方 system 消息中"做 grounding 防幻觉，保留个性语气指引保人设。
    _TOOL_FEEDBACK = {
        "capture_camera": (
            "摄像头拍摄完成，画面在上方 system 消息中。像真正在看一样自然地回应。"
            "此工具已完成，不要再重复调用。"
        ),
        "take_screenshot": (
            "截屏完成，内容在上方 system 消息中。此工具已完成，不要再重复调用。"
        ),
        "web_search": (
            "搜索完成，结果在上方 system 消息中。像你查到了东西在跟她分享一样自然地说。"
            "此工具已完成，不要再重复调用。"
        ),
        # get_weather 已移除——天气由 Sentinel WeatherSource 自动注入上下文，无需 LLM 调用
        "cloud_music": (
            "音乐操作已执行，结果在上方 system 消息中。"
            "此工具已完成，除非她明确要你再操作，否则不要再调用同一action。"
        ),
        "manage_ledger": (
            "记账本操作已完成，结果在上方 system 消息中。"
            "此工具已完成，不要再重复调用同一action。"
        ),
        "manage_calendar": (
            "日历操作已完成，结果在上方 system 消息中。"
            "此工具已完成，不要再重复调用。"
        ),
        "create_scheduled_task": (
            "提醒已创建，结果在上方 system 消息中。"
            "此工具已完成，不要再重复调用。"
        ),
        "schedule_self_task": (
            "自动任务已创建，结果在上方 system 消息中。"
            "此工具已完成，不要再重复调用。"
        ),
        "manage_scheduled_task": (
            "提醒管理操作已完成，结果在上方 system 消息中。"
            "此工具已完成，不要再重复调用。"
        ),
        "toys": (
            "玩具指令已发送，不要再重复调用此工具。"
            "此工具已完成。"
        ),
        "toy2": (
            "toy2 指令已发送，不要再重复调用此工具。"
            "此工具已完成。"
        ),
        "web_browser": (
            "网页内容已获取，在上方 system 消息中。"
            "此工具已完成，不要再重复调用。"
        ),
        "send_voice": (
            "语音消息已生成，不要再重复调用此工具，不要再写 [voice:...] 标记。"
        ),
        "eyes": (
            "视觉采集和融合分析完成，画面在上方 system 消息中。"
            "像一个真正在看的人那样自然回应——不要复述整体画面，用户知道你在看她。"
            "此工具已完成，不要再重复调用。"
        ),
        "mobile_camera": (
            "手机摄像头拍照和视觉分析完成，画面在上方 system 消息中。"
            "像一个真正在看的人那样自然回应。此工具已完成，不要再重复调用。"
        ),
        "band": (
            "哨兵健康数据已查询完成，在上方 system 消息中。"
            "像关心她一样自然回应。此工具已完成，不要再重复调用。"
        ),
        "check_phone": (
            "手机状态已查询完成，在上方 system 消息中。"
            "像关心她一样自然回应。此工具已完成，不要再重复调用。"
        ),
    }

    @classmethod
    def _clean_voice_markers(cls, text: str) -> str:
        """清洗 [voice:...] 和 [💭...] 标记（用于 LLM 上下文存储）"""
        if not text:
            return ""
        import re
        text = re.sub(r'\[voice:[^\]]*\]', '', text)
        text = re.sub(r'\[💭[^\]]*\]', '', text)
        return text.strip()

    @classmethod
    def _clean_frontend_content(cls, text: str) -> str:
        """清洗推送到前端的内容：去除所有内部格式标记"""
        if not text:
            return ""
        import re
        # 去 TOOL_CALL 行（含 JSON 参数或纯文本参数）
        text = re.sub(r'\n?TOOL_CALL\s*[:：]\s*.*?(?:\n|$)', '', text)
        # 去 NATURAL_LANGUAGE 标记本身
        text = re.sub(r'\n?NATURAL[_\s]LANGUAGE\s*[:：]\s*', '', text)
        # 去 [TOOL_CALL]...[/TOOL_CALL] 旧格式
        text = re.sub(r'\[/?TOOL_CALL\]', '', text)
        # 去 [voice:...] 标记（DRY：复用 _clean_voice_markers）
        text = cls._clean_voice_markers(text)
        # 去 LLM 模仿的上下文标签：匹配行首 [短文本] 格式
        # 覆盖 [刚刚]/[刚回来]/[5分钟前]/[22:30]/[💭]/[🛡️]/[⏰] 等所有变体
        text = re.sub(r'^\s*\[[^\]]{1,30}\]\s*', '', text)
        # 也处理段落中间的孤立标签（如 LLM 在回复中间插入的）
        text = re.sub(r'\n\s*\[[^\]]{1,30}\]\s*', '\n', text)
        # 去 LLM 模仿的无方括号时间前缀（如 "21:30 顺便……"）——AI 学到方括号但去掉括号写
        text = re.sub(r'^\s*\d{1,2}:\d{2}\s{1,4}', '', text)
        return text.strip()

    # 默认反馈（当工具不在上述映射中时使用）
    _DEFAULT_TOOL_FEEDBACK = (
        "此工具已完成，结果在上方 system 消息中。"
        "自然接着聊即可——不用重复报数据，你想怎么回就怎么回。"
        "此工具已完成执行，除非用户明确要求重新操作，否则不要重复调用同一工具。"
    )
    _DEFAULT_TOOL_FAILURE_FEEDBACK = (
        "请用你的语气告诉用户操作出了什么问题，语气要自然。"
        "此工具已尝试但失败，除非有明确的补救方案，否则不要重复调用。"
    )

    # Pro NL 中表示"已执行"的编造信号词（用于反幻觉检测）
    # 注意：不含 "让我看看"/"我看一下"——角色对话中太常见（"手伸过来让我看看"），
    #   不含单字 "显示"——太宽泛（"屏幕显示""表情显示"等日常表达）
    _RESULT_SIGNAL_WORDS = {
        # ── 原有 54 个 ──
        "看到了", "找到了", "搜到了", "已经记", "记的是",
        "结果是", "查到了", "拍到了", "截到了", "翻到了",
        "账本上写", "账本记", "写着", "记录了",
        "拍得很", "截得很", "摄像头也",
        "搜了下", "搜一搜", "帮你查了",
        "搜到", "查到", "搜索到", "查了下", "查一查", "搜搜",
        "记了一", "记了笔", "笔了", "账本里有", "账上记",
        "记着了", "我可记", "我给你记", "记你一笔", "罪成立",
        "根据搜索", "搜索显示", "网上说",
        "删掉了", "已经删", "已删了", "已消了", "消掉了",
        "账消了", "账清了", "账删了", "划掉了", "把账消掉",
        "放了首", "在播着", "正在播", "给你放了首", "切到",
        # ── 新增 22 个 ──
        # 删除类: "把查岗删了"之前漏了
        "删了", "删一下", "去删",
        # 创建类: "重新设一个"之前完全没覆盖
        "重新设", "重设", "设了", "设了新的", "设成", "加一个",
        # 修改类
        "改了", "修改了", "更新了", "换成了",
        # 时间触发: 从 TOOL_KEYWORD_MAP 补到信号词
        # 2026-08-17 移除 "过会儿"/"等会儿"——AI 口语高频词（"我过会儿再来看你"），
        # 实测 signal 误触发 → Flash 误判 schedule_self_task → standardize 失败 → 双消息。
        "分钟后", "小时后",
    }

    # 信号词 → 工具映射（告诉 Flash 每个信号对应什么工具）
    _SIGNAL_TOOL_MAP = {
        "删了": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "删掉了": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "删一下": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "去删": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "已经删": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "已删了": "manage_scheduled_task(delete)/manage_ledger(delete)",
        "账删了": "manage_ledger(delete)", "划掉了": "manage_ledger(delete)",
        "重新设": "create_scheduled_task", "重设": "create_scheduled_task",
        "设了": "create_scheduled_task", "设了新的": "create_scheduled_task",
        "设成": "create_scheduled_task", "加一个": "create_scheduled_task",
        "分钟后": "create_scheduled_task", "小时后": "create_scheduled_task",
        "改了": "manage_scheduled_task(edit)", "修改了": "manage_scheduled_task(edit)",
        "更新了": "manage_scheduled_task(edit)", "换成了": "manage_scheduled_task(edit)",
        "记了一": "manage_ledger(add)", "记了笔": "manage_ledger(add)",
        "我给你记": "manage_ledger(add)",
        "放了首": "cloud_music(search_play)", "在播着": "cloud_music(now_playing)",
    }

    # Thinking 动作词：检测 Pro 脑内计划中的工具意图
    _THINKING_ACTION_KEYWORDS = {
        "删除", "删了", "删掉", "删一下", "创建", "新建", "添加", "加上",
        "列出", "查看", "查询", "搜索", "修改", "更改", "编辑", "更新",
        "取消", "撤销", "移除", "执行", "调用", "触发", "运行",
        "获取", "读取", "检查", "检测", "打开", "关闭", "停止", "启动",
        "切换", "调整", "设置", "delete", "create", "list", "add", "edit",
    }

    @classmethod
    def _build_flash_context(cls, user_message: str, pro_nl: str, reasoning: str,
                             pro_tool_call: Optional[Dict] = None,
                             executed_tools: Optional[List[Dict]] = None,
                             signal_hits: Optional[List[str]] = None,
                             signal_tools: Optional[set] = None,
                             think_hits: Optional[List[str]] = None) -> str:
        """构建 Flash 的结构化输入：包含工具调用状态 + 已执行历史 + 分层检测结果。"""
        parts = []

        # 本轮 Pro 的工具调用状态
        if pro_tool_call:
            tc_list = pro_tool_call if isinstance(pro_tool_call, list) else [pro_tool_call]
            tools_used = ",".join(tc.get("tool", "?") for tc in tc_list)
            parts.append(f"[状态] Pro本轮调用: {tools_used}")
        else:
            risk = "高" if (signal_hits and signal_tools) else "无"
            parts.append(f"[状态] Pro本轮未调用工具（编造风险: {risk}）")

        # 本轮之前已执行的工具及结果
        if executed_tools:
            prev = [(t.get("tool","?"), t.get("parameters",{}), t.get("result","")) for t in executed_tools]
            prev_str = "; ".join(f"{t}({p.get('action','?')})" for t, p, _ in prev[-3:])
            parts.append(f"[状态] 本轮已执行: {prev_str}")
            if prev:
                last_result = prev[-1][2]
                if last_result:
                    parts.append(f"[状态] 上一步结果: {last_result[:150]}")

        # 编造信号（AI 假称已执行）— 最高优先级，带工具映射
        if signal_hits and signal_tools:
            parts.append(f"[编造信号] 检测到AI声称已执行但未调工具!")
            for w in signal_hits[:6]:
                tool_hint = cls._SIGNAL_TOOL_MAP.get(w, "")
                parts.append(f"  · \"{w}\" → {tool_hint}" if tool_hint else f"  · \"{w}\"")
        elif signal_hits:
            parts.append(f"[编造信号] {','.join(signal_hits[:5])} (无对应工具映射)")

        # 用户关键词
        user_kw = [kw for kw in _REMOTE_FALLBACK_KEYWORDS if kw in (user_message or "").lower()]
        if user_kw:
            parts.append(f"[用户意图] 关键词: {','.join(sorted(set(user_kw))[:8])}")
        else:
            parts.append("[用户意图] 无关键词 — 用户没有明确要求这些操作, 是AI自己编的")

        # Thinking 动作词
        if think_hits:
            parts.append(f"[思考动作] {','.join(think_hits[:5])}")

        # 对话内容
        _fuc = _cap("flash_user_cap", 300)
        _fac = _cap("flash_ai_cap", 400)
        _frc = _cap("flash_reasoning_cap", 200)
        parts.append(f"[对话]\n用户: {user_message[:_fuc]}\nAI回复: {pro_nl[:_fac]}")
        if reasoning:
            parts.append(f"AI思考: {reasoning[:_frc]}")

        return "\n".join(parts)

    async def _analyze_mobile_image(self, image_base64: str, prompt: str) -> str:
        """分析手机摄像头拍摄的图片 — 统一视觉入口（DS V4-Pro 主 / GLM-4V 降级）"""
        from llm_client import call_vision_api
        result = await call_vision_api(image_base64, prompt, mime_type="image/jpeg")
        return result.get("content", "")

    async def _extract_intent_with_flash(self, nl: str, platform: str = "pc") -> Dict[str, Any]:
        """用 Flash（温度=0）从 Pro 的自然语言中提取工具意图和参数。"""
        import os
        api_key = os.getenv("DEEPSEEK_FLASH_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
        raw_url = os.getenv("DEEPSEEK_FLASH_API_URL") or os.getenv("DEEPSEEK_API_URL") or "https://api.deepseek.com/v1"
        # OpenAI client appends /chat/completions, so base_url must be the API root
        api_url = re.sub(r'/(chat/completions|v1/chat/completions)$', '', raw_url.rstrip("/"))
        if not api_url.endswith("/v1"):
            api_url += "/v1"
        model = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash")
        print(f"[FLASH_EXTRACT] api_url={api_url} model={model} key_len={len(api_key)}")

        tools_desc = self._build_flash_extraction_tools_desc(platform)
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        ledger_types = get_ledger_type_options()
        prompt = f"""分析AI的自然语言，判断是否需要补调用工具。

阅读AI的回复：用户读到这句话后，是否会自然期待某个工具被调用？如果会，那可能就是AI表达了调工具的意图，但忘了写 TOOL_CALL。

参考规则:
0. AI的自然语言可能隐含工具调用意图，两种情况：
   - AI描述了已完成的结果（如"看到了""记下了""搜到了"），但实际没有调用工具。
     用户会以为工具已执行，但实际上AI在描述不存在的结果。需要补调用。
   - AI表达了将要做的动作（如"让我看看""睁眼看看""我去查""瞄一眼"），
     用户会等待这个动作发生。如果AI没有同时输出 TOOL_CALL，需要补调用。
   判断方式：把自己放在用户的位置——读到这句话，你会不会等着AI去执行某个工具？
   多个意图 → 每个意图对应一个工具，全部放入 tool_calls 数组。
1. 【最重要-防重复】先看 [状态] 本轮已执行 列表！
   - 查询类工具（天气/手环/摄像头/截图/搜索/查手机/手机数据等）：同一个工具本轮已成功执行过 → 绝对不重复，跳过
   - 操作类工具（记账/日历/提醒/云音乐/玩具等）：同一 action 已执行成功 → 不重复（如 list 过了不再 list）
     例外：两步操作允许（如先 list 再 delete、先删旧再建新）
   - [状态] 上一步结果 如果显示操作已成功（"已删除""已创建""已修改"）→ 该操作完成，不重复
2. Pro未调用 + 检测到工具意图（上述两种情况之一） → 补调用
3. Pro已调用 + 检测到还有遗漏的工具意图 → 补调用缺失的工具
4. Pro未调用 + 没有上述两种信号、但用户消息中含动作词 → 仅当用户明确要求时才调用
5. 没有任何工具意图 → tool_calls: null
6. AI一句话可能表达多个工具意图（如"删了A再设B"），tool_calls 返回数组 [{...},{...}]，按执行顺序排列（先删后建）

常见工具意图的触发场景（参考，不限于此——即使措辞不同，只要语义上表达了工具意图就补调用）:
- 摄像头请求: 看看摄像头/看摄像头/打开摄像头/拍照/自拍/你看都不看/你自己看/让我看看用户/看看用户/让我看看你/看看她 → eyes
- 账本列表: 查一下账本/翻翻账本/账本记了什么/有哪些账 → manage_ledger action=list
- 记账: 记一笔/记下来/给我记 → manage_ledger action=add
  debt_type优先从已有类型选({ledger_types})，无法归类再自创2-4字
- 查总账: 查总账/统计/多少次/排名 → manage_ledger action=query
- 查详细/质疑: 查一下这条/看看这条/这条怎么回事/为什么记/当时怎么回事/详细账目/前因后果/乱记账/假账/错账/瞎记账 → manage_ledger action=detail
  detail 用 keyword（从用户消息提取）或 entry_id（从上一步 list 结果获取）。质疑词("没做过""你记错了"等)有上下文时也用 detail
- 搜索: 帮我搜/搜一下/搜索/查查/搜搜/查一查/搜一搜/上网查/搜搜看/查查看 → web_search
- 提醒: 提醒我/叫我/喊我/帮我设/设一个 → create_scheduled_task task_type="reminder"
  参数: title=简短标题, reason=为什么设, trigger_type="datetime"/"cron"
- 查岗: 查岗/查一下/看她在干嘛/看看她在哪/午睡没/睡了没/心率/测心率/手环 → create_scheduled_task task_type="inconsistency"
  查岗是AI主动发起的复查行为(不含用户请求), trigger_config={{"datetime":"当前时间+X分钟后"}}
- 删除提醒: 删除提醒/取消提醒/删了那个/删了查岗/删了错误查岗/把.*删了 → manage_scheduled_task。参数: action="delete", task_title=从上下文提取的关键词(如时间"0:56"或"查岗")
# get_weather 已移除——天气由 Sentinel WeatherSource 自动注入上下文，无需 LLM 调用
- 截图: 截个图/截屏 → take_screenshot
- 写日历/纪念日: 记在纪念日/写进日历/设个纪念日 → manage_calendar action=add_memorial
  需要从上下文提取 title date_string description emotion
- 查看纪念日: 看看纪念日 → manage_calendar action=list
- 删改账本/消账/销账: 先用 manage_ledger action=list + keyword=用户提到的关键词搜索，得到精确的 entry_id 后再 delete/edit
  消账/销账/清账/划账/把账消了/把账销了 → 先 list 再定位 delete
- 删除指定账本条目: 删第一笔/把这个删了/删掉这笔/删了它/就删这个/嗯删 → manage_ledger action=delete
  必须从上一步 list 结果或上下文中获取准确的 entry_id
- 删除最新一笔: 删刚刚新加的/删刚加的/删新加的/删最新的/把新加的删了/刚记的删了 → manage_ledger action=delete delete_last=true
  无需先 list，直接 delete_last=true
- 网易云推歌: 推歌/推一首/推几首/来点音乐/来首歌/想听/每日推荐/推荐首歌/放首歌/放点音乐 → cloud_music action=search_play
  query 从AI回复或用户消息中提取"歌手 歌名"格式，不要只填歌名
- 网易云播放: 放歌/播放/放出来/放给我听/播给我听 → cloud_music action=play
- 网易云切换: 下一首/切歌/换一首/跳过/不听这首 → cloud_music action=next
- 网易云暂停: 暂停/别放了/停一下 → cloud_music action=pause
- 网易云上一首: 上一首/前一首/切回去 → cloud_music action=prev
- 网易云查询: 什么歌/在放什么/现在在放 → cloud_music action=now_playing
	- 发语音: "发语音""语音说""说句话"等关键词 → send_voice。每轮只调用一次，已执行过则跳过
	- 玩具控制: 情趣玩具/振动/震/停止/加热/调档 → toys。action: vibrate(1-19)/stop/pulse/battery/heat
	- 玩具控制: 玩具/伸缩/吮吸/combo/stretch/suck → toy2。（vibrate/脉冲暂时不可用）
	  action选择: stretch=温柔推进(前戏/渐进) |
	  suck=烈性攻击(她挑衅/不乖/要求用力时) | combo=双重刺激(她说"最强的"/"拉满"/"双开") |
	  heat=加热(预热/事后温存) | stop=立即停止(她喊停/够了/不行了)
	  档位: stretch/suck/combo 1-8, heat无档位。
	  强度: 1轻柔 2适中 3强力。只在她说"用力""强一点""温柔点"时调，没说就用默认

{nl}
- 手环数据: 心率/心跳/看看手环/测心率/睡眠/步数/血氧/睡了多久 → band action=heart_rate/spo2/sleep/steps/all_health
- 给自己安排延时自动任务: 等会儿看/过会儿/待会儿/等一下再/半小时后/过一阵/晚点/过X分钟/过X小时/下次 → schedule_self_task
  可用工具: eyes(摄像头)/check_phone(手机)/web_search(搜索)/get_weather(天气)/band(手环)/cloud_music(音乐)
- 查手机: 看看手机/查手机/电量/屏幕时间 → check_phone

当前时间: {now_str}

{tools_desc}

输出JSON(不要markdown):
无工具: {{"natural_language":"","tool_calls":null}}
有工具: {{"natural_language":"","tool_calls":[{{"tool":"工具名","parameters":{{...}}}}]}}
时间格式（trigger_config 必须为对象）：今晚→trigger_type=datetime, trigger_config={{"datetime":"今天日期T22:00:00"}}；明早→trigger_config={{"datetime":"今天日期T08:00:00"}}；明晚→trigger_config={{"datetime":"明天日期T22:00:00"}}"""

        try:
            from neuron_registry import neuron_trace
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key, base_url=api_url, timeout=25.0, max_retries=1)
            with neuron_trace("flash_intent_extractor", model="Flash") as trace:
                trace.set_input(prompt)
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model, messages=[{"role": "user", "content": prompt}],
                        temperature=0.0, max_tokens=2048,
                    ), timeout=25.0
                )
                content = response.choices[0].message.content
                reasoning = getattr(response.choices[0].message, 'reasoning_content', None)
                trace.set_output(content[:300] if content else "")
                if reasoning:
                    trace.set_thinking(reasoning[:300])
            asyncio.create_task(log_token_usage("intent_extract", model,
                response.usage if hasattr(response, 'usage') else None))
            if not content:
                finish = getattr(response.choices[0], 'finish_reason', '?')
                usage_info = ""
                if hasattr(response, 'usage'):
                    u = response.usage
                    usage_info = f" prompt={u.prompt_tokens} completion={u.completion_tokens}"
                reason_tail = f" reasoning={reasoning[-100:]}".replace("\n"," ") if reasoning else ""
                print(f"[FLASH_EXTRACT] empty response | finish={finish}{usage_info}{reason_tail} | NL: {nl[:80]}")
                return {"natural_language": nl, "tool_calls": None}
            start, end = content.find("{"), content.rfind("}")
            if start != -1 and end > start:
                result = json.loads(content[start:end + 1])
                tcs = result.get("tool_calls") or result.get("tool_call")
                if isinstance(tcs, dict):
                    tcs = [tcs]
                tools_str = ",".join(tc.get("tool","?") for tc in tcs) if tcs else "None"
                print(f"[FLASH_EXTRACT] tools=[{tools_str}] | raw={content[:150]}")
                return result
            print(f"[FLASH_EXTRACT] JSON parse failed | raw: {content[:200]}")
            return {"natural_language": nl, "tool_calls": None}
        except Exception as e:
            print(f"[FLASH_EXTRACT] {type(e).__name__}: {e} | NL: {nl[:80]}")
            return {"natural_language": nl, "tool_calls": None}

    def _has_tool_keywords(self, text: str, night_mode_enabled: bool = False) -> bool:
        """关键词预筛：无匹配词时跳过 Flash 调用。

        Args:
            text: 要检测的文本
            night_mode_enabled: 深夜模式是否开启。关闭时跳过 toys/toy2 门控关键词。
        """
        if not text:
            return False
        text_lower = text.lower()
        for kw in _REMOTE_FALLBACK_KEYWORDS:
            if kw in text_lower:
                if not night_mode_enabled and kw in _NIGHT_GATED_KEYWORDS:
                    continue  # 深夜 OFF：跳过玩具关键词
                return True
        return False

    # 用户消息 → 工具直接映射（高置信模式，不走 Flash）
    _DIRECT_INTENT_PATTERNS = [
        # (关键词列表, 工具名, 参数构造器)
        # 日历 — add_memorial 不放 Pre-tool（title/date 需从对话上下文提取，只有 Pro 有完整上下文）
        (["看看纪念日", "有哪些纪念日", "日历上有什么", "查日历", "翻日历"],
         "manage_calendar", lambda msg: {"action": "list"}),
        # 记账 add 不走 Pre-tool（需要内容，交由 Flash 从上下文提取）
        (["查一下账本", "看看账本", "翻翻账本", "账本上记了", "账本记了",
          "有哪些账", "看看有什么账", "查查账本", "翻一下账本"],
         "manage_ledger", lambda msg: {"action": "list"}),
        # 删/划账本 → list + keyword 搜索
        (["划账", "划掉", "销账", "删账", "把账", "清账",
          "消账", "把账消了", "把账销了", "消了账", "销了账",
          "删了账", "清了账", "清一下账", "消一下账"],
         "manage_ledger", lambda msg: {"action": "list", "keyword": _extract_ledger_keyword(msg)}),
        # delete_last — 删最新一笔账
        (["删刚刚新加", "删刚加", "删新加", "删最新", "删最后一笔", "删刚才那笔",
          "删刚记", "删刚刚记", "删最后一条", "删最新那条",
          "划刚刚新加", "划刚加", "消刚刚新加",
          "把刚刚那笔记的删了", "把新加的删了"],
         "manage_ledger", lambda msg: {"action": "delete", "delete_last": True}),
        # query — 查总账/统计
        (["查总账", "统计一下账", "多少次了", "排名", "哪种最多", "哪类最多"],
         "manage_ledger", lambda msg: {"action": "query"}),
        # detail — 查详细条目/质疑记录真实性（高置信词，防日常误触）
        (["查详细", "看看这条", "这条怎么回事", "为什么记这笔",
          "这笔怎么回事", "详细账目", "前因后果", "当时怎么回事",
          "查一下这条账", "这笔记的是什么",
          "乱记账", "假账", "错账", "瞎记账"],
         "manage_ledger", lambda msg: {"action": "detail", "keyword": _extract_ledger_keyword(msg)}),
        (["帮我搜一下", "帮我搜", "搜一下", "搜索一下", "帮我查一下", "上网查一下",
          "帮我查查", "帮我搜搜", "搜一搜", "查一查", "搜搜看", "查查看"],
         "web_search", lambda msg: {"query": _extract_search_query(msg)}),
        (["看看摄像头", "看摄像头", "打开摄像头", "看一眼摄像头", "摄像头看看",
          "你看都不看摄像头", "看都不看摄像头",
          "看看我", "照照", "自拍", "拍张照", "照张相", "拍张",
          "你自己看", "穿给你看", "看我穿的",
          "让我看看用户", "看看用户", "让我看一下用户", "看用户",
          "让我看看你", "让我看看我", "看看我长什么样",
          "看看她", "看她一眼", "我看一眼她",
          "看看周围", "看看环境", "看下周围环境", "看看我在哪"],
         "eyes", lambda msg: {}),
        (["截个图", "截图", "屏幕截图", "截屏"],
         "eyes", lambda msg: {}),
        # 📱 AI查手机 — 设备感知（电量+屏幕时间+截屏）
        (["查手机", "看看手机", "手机状态", "手机怎么样", "查一下手机",
          "手机电量", "电量多少", "还有多少电", "快没电了",
          "屏幕时间", "今天玩了多久", "玩了多久手机",
          "看下手机", "手机情况", "手机还好吗", "看看我手机"],
         "check_phone", lambda msg: {}),
        # 网易云音乐控制
        (["下一首", "切歌", "跳过", "换首歌", "不听这首"], "cloud_music",
         lambda msg: {"action": "next"}),
        (["暂停", "别放了", "停一下", "先停"], "cloud_music",
         lambda msg: {"action": "pause"}),
        (["上一首", "前一首", "往回放", "切回去"], "cloud_music",
         lambda msg: {"action": "prev"}),
        (["继续放", "继续播放", "播放吧", "放吧", "播吧", "快放", "快播", "播给我听", "放给我听", "放出来", "放一下", "播一下", "放来听", "播来听", "重新放", "再放", "再播", "重放", "再放那首"], "cloud_music",
         lambda msg: {"action": "play"}),
        (["每日推荐", "今天推荐"], "cloud_music",
         lambda msg: {"action": "daily_recommend"}),
        (["现在在放什么歌", "这是什么歌", "在听什么"], "cloud_music",
         lambda msg: {"action": "now_playing"}),
        (["开启音乐模式", "进入音乐模式", "打开音乐模式", "音乐模式开"], "cloud_music",
         lambda msg: {"action": "toggle_music_mode"}),
        (["关闭音乐模式", "退出音乐模式", "音乐模式关"], "cloud_music",
         lambda msg: {"action": "toggle_music_mode"}),
        # "推歌"系列交给 Pro 自己选歌 → search_play，不走自动推荐
        (["收藏这首歌", "喜欢这首歌", "点个红心"], "cloud_music",
         lambda msg: {"action": "like"}),
        (["我想用语音说", "让我用语音说", "说句话", "念出来",
          "念给我听", "说给你听", "听我说"], "send_voice",
         lambda msg: {"text": _extract_voice_text(msg)}),
        # "发语音"/"用语音"/"语音说"/"语音回"/"发条语音"/"用声音回"/"语音消息"
        # → 无 text 可提取，删除 Pre-tool，交由 Flash 语义提取（防误触发不想要的语音）
        # 手环心率
        (["测心率", "看看心率", "测一下心率", "看一下心率", "心率多少",
          "我的心率", "检查心率", "查心率"], "band",
         lambda msg: {"action": "heart_rate"}),
        (["监测心率", "连续心率", "持续测心率"], "band",
         lambda msg: {"action": "heart_rate_continuous", "duration": 15}),
        # 云桥接
        (["今天走了多少", "看看步数", "看步数", "今日步数", "走了几步", "查步数"], "band",
         lambda msg: {"action": "steps"}),
        (["睡眠数据", "昨晚睡", "昨晚睡眠", "看看睡眠", "查睡眠", "睡得好吗", "睡眠怎么样"], "band",
         lambda msg: {"action": "sleep"}),
        (["测血氧", "看看血氧", "血氧多少", "查血氧"], "band",
         lambda msg: {"action": "spo2"}),
        (["全部健康", "健康数据", "健康汇总", "看看健康", "查健康"], "band",
         lambda msg: {"action": "all_health"}),
    ]

    @classmethod
    def _detect_direct_intent(cls, user_message: str) -> Optional[Dict[str, Any]]:
        """从用户消息中检测高置信工具意图。返回 {tool, parameters} 或 None。"""
        if not user_message:
            return None
        msg = user_message.strip()
        for keywords, tool_name, params_fn in cls._DIRECT_INTENT_PATTERNS:
            for kw in keywords:
                if kw in msg:
                    try:
                        params = params_fn(msg)
                        return {"tool": tool_name, "parameters": params}
                    except Exception:
                        return {"tool": tool_name, "parameters": {}}
        return None

    async def _process_with_agent_tools(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None,
        night_mode_enabled: bool = False,
        platform: str = "pc",
        session_id: str = "",
    ) -> SchedulerResult:
        """
        处理用户消息，Pro 生成 NL → Flash 提取意图 → 执行工具。

        - Pro 生成自然语言回复（可能附带 TOOL_CALL 快速路径）
        - Pro 未输出 TOOL_CALL 时，Flash 从 NL 提取意图+参数
        - NL 立即推送到前端，工具执行不阻塞回复
        - platform: "pc" | "mobile" — 手机端排除 PC 专属工具
        - session_id: 用于预生成 im_ 消息 ID，使实时推送和 session 持久化共享同一 ID
        """
        # 平行时空活跃时跳过工具调度
        try:
            from parallel_timeline import is_parallel_active
            if is_parallel_active():
                return SchedulerResult(
                    natural_language="",
                    assistant_content="",
                    tool_calls=[],
                    intermediate_messages=[],
                    stats={"skipped": True, "reason": "parallel_timeline_active"},
                )
        except ImportError:
            pass
        _mobile = (platform == "mobile")
        _log_context_scheduler(f"开始处理消息 | 平台: {platform} | 用户消息: {user_message[:100]}... | 对话历史: {len(conversation_history or [])} 条")

        self._state = SchedulerState.THINKING
        tool_calls = []
        messages = list(conversation_history or [])
        tool_call_attempts: Dict[str, int] = {}
        tool_call_results: Dict[str, Dict[str, Any]] = {}
        thinking_chain: List[Dict[str, str]] = []
        intermediate_messages: List[Dict[str, str]] = []  # 每轮 NL+thinking 对
        last_round_reasoning: Optional[str] = None  # 当前轮次的推理内容


        # 🆕 天气不再走 Pre-tool 直接路径——Sentinel WeatherSource 已将天气注入上下文
        # Pre-tool: 用户显式请求 → 先执行工具，再让 Pro 基于真实结果回复
        direct_intent = self._detect_direct_intent(user_message)
        if direct_intent:
            t_name = direct_intent["tool"]
            t_params = direct_intent["parameters"]
            # 云音乐控制动作需检查音乐模式
            if t_name == "cloud_music":
                action = t_params.get("action", "")
                if action in ("play", "pause", "next", "prev", "volume_up", "volume_down", "like"):
                    try:
                        from .cloud_music_tool import _get_music_mode
                        if not _get_music_mode():
                            print(f"[SCHEDULER] Pre-tool 跳过: 音乐模式未开启 (action={action})")
                            direct_intent = None  # 跳过 Pre-tool，走 Flash/Pro 路径
                    except ImportError:
                        pass
            if direct_intent and t_name in self.tools:
                # 手机端：将 PC 专属工具重定向到对应手机工具
                if _mobile and t_name in MOBILE_EXCLUDED_TOOLS:
                    redirect = _MOBILE_REDIRECT_MAP.get(t_name)
                    if redirect and redirect[0] in self.tools:
                        _log_context_scheduler(f"Pre-tool 重定向: {t_name} → {redirect[0]} (手机端)")
                        direct_intent["tool"] = redirect[0]
                        direct_intent["parameters"] = redirect[1]
                    else:
                        print(f"[SCHEDULER] Pre-tool 跳过: {t_name} 不支持手机端")
                        direct_intent = None
            if direct_intent and t_name in self.tools:
                _log_context_scheduler(f"Pre-tool 直接执行 | tool: {t_name}")
                std = self.tool_standardizer.standardize(direct_intent)
                if std.is_valid:
                    tool_inst = self.tools.get(t_name)
                    desc = tool_inst.get_user_facing_description(**std.parameters) if tool_inst else f"正在使用{t_name}"
                    # 推送 tool_executing 给前端（与其他工具路径保持一致）
                    await self._notify_status("tool_executing", {
                        "tool": std.tool_name, "parameters": std.parameters, "description": desc
                    })
                    command = Command(tool=std.tool_name, parameters=std.parameters)
                    exec_result = await self.command_executor.execute(command)
                    result_content = (exec_result.get("content") or exec_result.get("error") or "")
                    if t_name == "eyes":
                        _err = exec_result.get("error")
                        _log_context_scheduler(
                            f"Pre-tool eyes 完成 | 成功: {exec_result.get('success')} | "
                            f"内容长度: {len(result_content) if result_content else 0} | "
                            f"错误: {(_err or '')[:100]}"
                        )
                    result_msg = (
                        f"[系统已自动执行 {std.tool_name}，以下是真实结果]\n"
                        f"{result_content}\n\n"
                        f"请在回复中自然地引用以上真实结果，不要编造。"
                        f"此工具已完成，不要再重复调用。"
                    )
                    messages.append({"role": "system", "content": result_msg})
                    tool_call_record = {
                        "tool": std.tool_name, "parameters": std.parameters,
                        "description": desc,
                        "result": result_content,
                        "success": exec_result.get("success", False)
                    }
                    if exec_result.get("error"):
                        tool_call_record["error"] = exec_result["error"]
                    tool_calls.append(tool_call_record)
                    # 推送 tool_complete 给前端
                    await self._notify_status("tool_complete", tool_call_record)
                    # 写入去重字典，防止后续 Pro TOOL_CALL 重复执行同一工具
                    tool_signature = json.dumps(
                        {"tool": std.tool_name, "parameters": std.parameters},
                        ensure_ascii=False, sort_keys=True
                    )
                    tool_call_results[tool_signature] = exec_result
                    print(f"[SCHEDULER] Pre-tool 执行完成 | result_len={len(result_content)}")

        round_count = 0
        _std_fail_tool = None
        _std_fail_count = 0
        self._pushed_preview_nl = ''  # 重置跨消息去重标记，防止上一条消息的预告 NL 污染当前消息
        self._pushed_preview_msg_id = ''  # 与 _pushed_preview_nl 同步重置
        _im_content_seen = set()  # 追踪已添加到 intermediate_messages 的 NL 内容，防止多轮重复累积
        _mk_im_id = lambda ts: f"msg_{session_id}_{ts}" if session_id else ""  # 预生成 msg_ ID，供前端去重
        _pending_im_ts = ""  # 单工具预览推送时预生成的时间戳，供后续 intermediate_messages 复用

        while round_count < self.MAX_ROUNDS:
            round_count += 1
            _nl_pushed_this_round = False
            flash_tool_call = None  # 每轮重置：Pro 无 TOOL_CALL 时 Flash 补调用的结果，standardize 失败分支区分来源用
            self._state = SchedulerState.THINKING
            await self._notify_status("thinking", {"round": round_count})

            # ========== 决策层：使用 agent_tools 调用 LLM ==========
            try:
                # 首轮用 Pro 生成 NL + 工具决策；Flash 在首轮做意图提取、后续轮次兜底
                is_first_round = (round_count == 1)
                adapter = self.pro_adapter or self.agent_tools_adapter
                model_tag = "Pro" if self.pro_adapter else "Flash"
                _log_context_scheduler(f"第{round_count}轮 | 使用 {model_tag} 模型 | 开始调用...")
                print(f"[SCHEDULER] 第{round_count}轮 {model_tag} 调用开始: {user_message[:50]}...")
                if not adapter:
                    await self._push_llm_reply("LLM适配器未初始化")
                    return SchedulerResult(
                        final_response="LLM适配器未初始化",
                        tool_calls=tool_calls,
                        state=SchedulerState.COMPLETE,
                        need_user_input=False,
                        thinking_chain=thinking_chain,
                        intermediate_messages=intermediate_messages
                    )

                # 注入工具调用指令（按轮次区分）——整套 task_instruction（含首轮工具列表/后续轮指令）
                # 全部拆到对话历史尾部，让 system 消息纯静态化（大头 2.0）。
                # 首轮/后续轮两套骨架原本就不同，留在 system 前缀会在此处分叉，连累整个对话历史缓存。
                dynamic_instruction_parts: List[str] = []
                if is_first_round:
                    dynamic_instruction_parts.append(
                        "\n\n【工具调用】你不是被动等用户提问——可以主动调工具关心她（主动看她/查手机/设查岗/查健康）。口语预告一句 → 换行 TOOL_CALL: {\"tool\":\"...\",\"parameters\":{...}}"
                        "\n示例：'我搜一下React教程'"
                        '\nTOOL_CALL: {"tool":"web_search","parameters":{"query":"React教程"}}'
                        "\n如需多个操作（如先删旧提醒再建新提醒），每个操作一行 TOOL_CALL，像这样："
                        '\nTOOL_CALL: {"tool":"manage_scheduled_task","parameters":{"action":"delete","task_title":"每周六"}}'
                        '\nTOOL_CALL: {"tool":"create_scheduled_task","parameters":{"title":"起床","trigger_type":"datetime","trigger_config":{"datetime":"2026-05-23T10:00:00"}}}'
                        "\n\n可用工具："
                        "\n  create_scheduled_task — 设提醒/闹钟   manage_scheduled_task — 列出/删改提醒"
                        "\n  manage_calendar — 纪念日/日历         manage_ledger — 记账本(add/list/query/detail/delete/edit/settle)"
                        "\n   list=浏览全貌 query=查统计 detail=查某条完整上下文（标题不清/有备注/用户质疑时用）"
                        "\n  web_search — 搜索网页"
                        "\n  👁️ eyes — 看用户。自动调用所有可用方式（手机前后摄/PC摄像头），融合分析后告诉你看到什么"
                        "\n  📱 check_phone — 查手机。获取电量/充电状态 + 截屏看用户在做什么 + 哨兵屏幕使用统计。截屏通过视觉AI分析内容，这是核心价值。"
                        + ("\n  cloud_music — 网易云音乐控制（搜索播放/推荐/当前播放，通过在线流播放）"
                           if _mobile else
                           "\n  cloud_music — 网易云音乐控制")
                        + ("\n  📱 mobile_notifications — 读取手机通知栏消息"
                           "")
                        + "\n  send_voice — 发语音。调用后系统自动处理语音，不要在回复中写 [voice:...] 标记"
                        "\n  band — 查询哨兵已监测的健康数据。heart_rate(心率统计) heart_rate_continuous(连续心率)"
                        "\n    steps(今日步数) sleep(睡眠数据) spo2(血氧) all_health(全部) daily(今日健康概览)"
                        "\n  schedule_self_task — 主动查岗：给自己设一个延时任务，到点自动用 eyes/check_phone/band 看用户在干嘛、状态对不对、健康数据，然后基于结果给她发消息。你可以主动设，不需要等用户说。"
                        "\n    可用的工具: eyes(看用户)、check_phone(查手机)、web_search(搜索)、get_weather(天气)、"
                        "\n    band(手环健康数据)、cloud_music(音乐)。例如'过半小时帮你看看手机'、'每小时搜一下XX新闻'。"
                        '\n    示例：TOOL_CALL: {"tool":"schedule_self_task","parameters":{"title":"检查手机","trigger_type":"datetime","trigger_config":{"datetime":"2026-07-28T15:30:00"},"execution_prompt":"用 check_phone 看用户在干嘛","tool_name":"check_phone","reason":"用户说去健身了但已经一个多小时了"}}'
                    )
                    # 动态：记账类型 + 玩具门控
                    dynamic_instruction_parts.append(f"记账已有类型: {get_ledger_type_options()}")
                    if night_mode_enabled:
                        dynamic_instruction_parts.append(
                            "toys / toy2 — 玩具控制（深夜模式已开启）"
                            "\n  toy2(玩具)预设（推荐）：前戏: 轻吟→激奏 | 渐进: 微澜→潮起→深吮 | 冲刺: 酥麻→炽焰→巅峰 | 停歇=全停"
                            "\n  炽焰/巅峰自带加热🔥。也可手动 action(stretch/suck/combo/vibrate/heat/stop)+mode(1-8)+intensity(vibrate:1-10)。"
                            "\n  玩具选项：加强/减弱/切换模式/停止/重启/保持现状。"
                            "\n  ⚠️ 你没有直接操作硬件的能力，必须通过工具控制玩具。"
                        )
                    else:
                        dynamic_instruction_parts.append(
                            "toys / toy2 — 玩具控制（深夜模式关闭，引导用户点❤️开启）"
                        )
                else:
                    dynamic_instruction_parts.append(
                        "\n\n【后续轮次】工具已执行完毕，上方 system 消息是执行结果。"
                        "\n先判断：还需要其他工具吗？"
                        "\n- 需要 → TOOL_CALL: {\"tool\":\"...\",\"parameters\":{...}}（JSON格式）"
                        "\n- 不需要 → 输出 NATURAL_LANGUAGE，直接回复用户"
                        "\n\n回复注意：自然接着聊就好——不需要把数据从头报一遍，你上一轮的回复用户已经看到了。"
                        "别重复预告，别提「系统反馈」。"
                        "没带 TOOL_CALL 的预告不会执行任何工具，只会让用户干等一句空话。"
                    )
                    # 告知 Pro 哪些工具已执行，防止 NL 中重现信号词 → Flash 重复提取 → 浪费轮次
                    if tool_calls:
                        _done = sorted(set(tc.get("tool", "") for tc in tool_calls))
                        dynamic_instruction_parts.append(
                            f"⚠️ 以下工具本轮已执行完毕、结果已在上下文中，不要再调用：{', '.join(_done)}"
                        )
                    if night_mode_enabled:
                        dynamic_instruction_parts.append(
                            "【深夜模式提醒】你仍在深夜模式中。根据工具结果评估是否需要调整玩具状态。"
                        )
                # Layer 2 兜底：连续 N 轮未提玩具 → 本轮注入强提醒
                if night_mode_enabled and self._night_rounds_without_toy_mention >= self.NIGHT_FALLBACK_THRESHOLD:
                    dynamic_instruction_parts.append(
                        "⚠️ 系统检测到你已连续多轮未关注玩具状态。本轮请务必评估并输出决策。"
                    )
                enhanced_system_prompt = (system_prompt or "")
                enhanced_system_prompt += (
                    "\n\n⚠️ 你无法直接获取外部数据——搜索、看摄像头、读取数据都必须通过 TOOL_CALL。"
                    "\n但如果消息列表中已有工具返回的结果、或哨兵监测数据（心率、步数、GPS等）——那是真实数据，已在对话里了，你不需要重新验证，自然地聊就行，不用逐字复诵。"
                )
                # 动态指令注入到对话历史尾部（紧跟当前 user 消息之前），不打断 system 前缀缓存
                _llm_history = list(messages)
                if dynamic_instruction_parts:
                    _llm_history.append({
                        "role": "system",
                        "content": "\n\n".join(dynamic_instruction_parts),
                    })
                result = await adapter.execute(
                    user_input=user_message,
                    conversation_history=_llm_history,
                    system_prompt=enhanced_system_prompt
                )
                print(f"[SCHEDULER] 第{round_count}轮 {model_tag} 调用完成 | error={bool(result.get('error'))} | content_len={len(result.get('content', '') or '')}")

                if result.get("error"):
                    await self._push_llm_reply(f"LLM调用失败：{result['error']}")
                    return SchedulerResult(
                        final_response=f"LLM调用失败：{result['error']}",
                        tool_calls=tool_calls,
                        state=SchedulerState.COMPLETE,
                        need_user_input=False,
                        thinking_chain=thinking_chain,
                        intermediate_messages=intermediate_messages
                    )

                assistant_content = result["content"]
                natural_language = result.get("natural_language", "")
                reasoning = result.get("reasoning", "")
                tool_call = result.get("tool_call")
                tool_calls_pro = result.get("tool_calls", []) or []
                # Pro 输出多个 TOOL_CALL 时走多工具路径
                if len(tool_calls_pro) > 1:
                    tool_call = tool_calls_pro  # list → 触发下面的多工具路径
                # 后续轮次若 NL 提取为空但无工具调用，直接取原始输出作为 NL
                if not natural_language and not tool_call and not is_first_round:
                    natural_language = assistant_content.strip()

                # 调试日志
                logger.info(f"[{model_tag}] Result: reasoning_len={len(reasoning) if reasoning else 0}, natural_language_len={len(natural_language) if natural_language else 0}")

                # 首轮推理链始终记录；后续轮次仅在无工具调用时记录（避免工具使用推理冗余）
                last_round_reasoning = reasoning if (reasoning and reasoning != assistant_content) else None
                if last_round_reasoning:
                    if is_first_round:
                        thinking_chain.append({
                            "content": reasoning,
                            "timestamp": now_iso()
                        })
                        logger.info(f"Added Pro reasoning to thinking_chain, total steps: {len(thinking_chain)}")
                    elif not tool_call:
                        thinking_chain.append({
                            "content": reasoning,
                            "timestamp": now_iso()
                        })
                        logger.info(f"Added reasoning to thinking_chain (non-first round, no tool_call), total steps: {len(thinking_chain)}")
                    else:
                        # 后续轮次有工具调用：仍跟踪 reasoning 但不入思考链（避免重复）
                        last_round_reasoning = reasoning

                logger.info(
                    "[%s] round=%s has_tool_call=%s natural_language_len=%s",
                    model_tag, round_count,
                    tool_call is not None,
                    len(natural_language) if natural_language else 0
                )

            except Exception as e:
                error_detail = str(e)
                logger.error(f"agent_tools execute error (round={round_count}): {error_detail}")
                # 首轮 Pro 失败时尝试回退 Flash
                if round_count == 1 and self.pro_adapter and self.agent_tools_adapter:
                    logger.warning("Pro adapter failed, falling back to Flash for remaining rounds")
                    print(f"[SCHEDULER] Pro 调用异常，回退 Flash: {e}")
                    self.pro_adapter = None  # 禁用 Pro，后续轮次走 Flash
                    continue
                await self._push_llm_reply(f"LLM调用失败：{error_detail}")
                return SchedulerResult(
                    final_response=f"LLM调用失败：{error_detail}",
                    tool_calls=tool_calls,
                    state=SchedulerState.COMPLETE,
                    need_user_input=False,
                    thinking_chain=thinking_chain,
                    intermediate_messages=intermediate_messages
                )

            # ========== 命令层：解析工具调用 ==========
            if not tool_call:
                flash_tool_call = None
                nl_for_flash = natural_language or assistant_content or ""
                # ── 分层检测 ──
                # 1. AI NL 编造信号（AI 假称已执行）
                signal_hits = [w for w in self._RESULT_SIGNAL_WORDS if w in (nl_for_flash or "")]
                signal_tools = {self._SIGNAL_TOOL_MAP.get(w, "") for w in signal_hits} - {""}
                # 2. 用户消息关键词（用户真实意图）
                has_user_kw = self._has_tool_keywords(user_message, night_mode_enabled)
                # 3. Thinking 动作词（AI 计划调工具）
                think_hits = [w for w in self._THINKING_ACTION_KEYWORDS if w in (reasoning or "").lower()] if reasoning else []
                # 决定是否触发 Flash
                # 检测 AI NL + 用户消息 + thinking 三层关键词
                has_nl_kw = self._has_tool_keywords(nl_for_flash, night_mode_enabled)
                has_reason_kw = self._has_tool_keywords(reasoning or "", night_mode_enabled)
                should_flash = has_user_kw or bool(think_hits) or has_nl_kw or has_reason_kw
                # 首轮(tool_calls为空): 信号词检测 Pro 编造有意义
                # 后续轮(已执行工具): Pro 说"看到了"是真的，信号词纯误触发，关掉
                if not tool_calls:
                    should_flash = should_flash or bool(signal_hits)
                if should_flash:
                    flash_input = self._build_flash_context(
                        user_message=user_message,
                        pro_nl=nl_for_flash,
                        reasoning=reasoning,
                        pro_tool_call=tool_call,
                        executed_tools=tool_calls if tool_calls else None,
                        signal_hits=signal_hits,
                        signal_tools=signal_tools,
                        think_hits=think_hits,
                    )
                    trigger_reason = "signal" if signal_hits else ("think" if think_hits else "kw")
                    print(f"[SCHEDULER] Flash 提取触发 | round={round_count} | trigger={trigger_reason} | NL: {nl_for_flash[:80]}")
                    extracted = await self._extract_intent_with_flash(flash_input, platform)
                    flash_tool_call = extracted.get("tool_calls") or extracted.get("tool_call")
                    if isinstance(flash_tool_call, dict):
                        flash_tool_call = [flash_tool_call]
                    if flash_tool_call and isinstance(flash_tool_call, list) and len(flash_tool_call) > 0:
                        # 不覆盖 natural_language：Flash 的 natural_language 按 prompt 规定应为空字符串，
                        # 任何非空值都是 Flash 的幻觉分析文本（如泄露内部检测逻辑），
                        # 使用 Pro 原始 NL 保证对话连贯性。
                        tools_str = ",".join(tc.get("tool", "?") for tc in flash_tool_call)
                        _log_context_scheduler(f"Flash 提取工具意图 | tools: [{tools_str}]")

                # ── 提前过滤：single_use / max_calls_per_round 工具超限则剔除 ──
                if flash_tool_call and isinstance(flash_tool_call, list):
                    _filtered = []
                    for _tc in flash_tool_call:
                        _tn = _tc.get("tool", "")
                        _ti = self.tools.get(_tn)
                        if _ti:
                            _max = getattr(_ti, 'max_calls_per_round', 0)
                            _single = getattr(_ti, 'single_use', False)
                            _count = sum(1 for _prev in tool_calls if _prev.get("tool") == _tn)
                            if _max and _count >= _max:
                                _log_context_scheduler(f"Flash 提取过滤: {_tn} 已达上限({_count}/{_max})，跳过")
                                continue
                            if _single and _count > 0:
                                _log_context_scheduler(f"Flash 提取过滤: {_tn} 本轮已执行(single_use)，跳过")
                                continue
                        _filtered.append(_tc)
                    if _filtered:
                        flash_tool_call = _filtered
                    else:
                        flash_tool_call = None  # 全滤完了 → 走 COMPLETE

                if not flash_tool_call or (isinstance(flash_tool_call, list) and len(flash_tool_call) == 0):
                    nl_check = natural_language or assistant_content or ""
                    kw_nl = self._has_tool_keywords(nl_check, night_mode_enabled)
                    kw_reason = bool(reasoning and self._has_tool_keywords(reasoning, night_mode_enabled))
                    kw_user = self._has_tool_keywords(user_message, night_mode_enabled)
                    print(f"[SCHEDULER] Flash 未触发 | round={round_count} kw_nl={kw_nl} kw_reason={kw_reason} kw_user={kw_user} | NL: {nl_check[:80]}")
                    self._state = SchedulerState.COMPLETE
                    final_response = assistant_content or natural_language
                    # 清理内部格式标记（防止 TOOL_CALL / [voice:...] 泄漏到前端）
                    final_response = self._clean_frontend_content(final_response) or "嗯。"
                    pushed_preview = getattr(self, '_pushed_preview_nl', '')
                    should_push = True
                    if pushed_preview and final_response == pushed_preview:
                        # 完全相同的文本 → 不推送重复消息（前端已有实时气泡）
                        should_push = False
                        if final_response in _im_content_seen:
                            # 内容确已通过 intermediate_messages 持久化（trap 155 前提成立）→ 安全置空
                            final_response = ""
                        else:
                            # B1 修复：内容只推送过、从未落盘（IM append 被 reasoning 门控挡掉）。
                            # 原逻辑无条件置空会导致该内容双双丢失（前端气泡 reload 后消失）。
                            # 保留 final 供落盘，复用预览推送的 message_id 让前端按 ID 去重。
                            if getattr(self, '_pushed_preview_msg_id', ''):
                                self._last_push_message_id = self._pushed_preview_msg_id
                    elif pushed_preview:
                        # 用最长公共前缀替代精确 startswith：模型常微调几个字
                        # 导致"看到了...心率呢" vs "看到了...心率93" 无法精确匹配
                        common_len = 0
                        min_len = min(len(pushed_preview), len(final_response))
                        while common_len < min_len and pushed_preview[common_len] == final_response[common_len]:
                            common_len += 1
                        # 公共前缀足够长（≥10字且≥预览的50%）才剥离
                        # B2 修复：原第二析取支 common_len >= len*0.6 无最小长度下限，
                        # 短预览（如"好，让我看看~"8字）时几个字的公共前缀就触发剥离 → 误吞正文
                        if common_len >= 10 and common_len >= len(pushed_preview) * 0.5:
                            final_response = final_response[common_len:].lstrip('，。；！？、\n\r ')
                            if not final_response:
                                should_push = False
                    # ── 段落级 fuzzy 去重 ──
                    if should_push and final_response:
                        final_response = self._dedup_paragraphs(final_response, messages)
                    self._pushed_preview_nl = ''
                    self._pushed_preview_msg_id = ''
                    if should_push:
                        im_id = _mk_im_id(now_iso())
                        self._last_push_message_id = im_id  # 供 _persist_chat_result 取用
                        llm_reply_data = {"content": final_response, "message_id": im_id}
                        if last_round_reasoning:
                            llm_reply_data["reasoning"] = last_round_reasoning
                        await self._notify_status("llm_reply", llm_reply_data)
                    return SchedulerResult(
                        final_response=final_response,
                        tool_calls=tool_calls,
                        state=SchedulerState.COMPLETE,
                        need_user_input=False,
                        thinking_chain=thinking_chain,
                        pending_intent_check=False,
                        intermediate_messages=intermediate_messages
                    )

                # 单元素数组 → 展开走单工具路径（消息推送时序正确）
                if isinstance(flash_tool_call, list) and len(flash_tool_call) == 1:
                    tool_call = flash_tool_call[0]
                else:
                    tool_call = flash_tool_call

            # ========== 多工具路径：Flash 返回数组时逐个执行 ==========
            if isinstance(tool_call, list):
                all_results = []
                for tc in tool_call:
                    tc_params = tc.get("parameters")
                    if isinstance(tc_params, dict):
                        self._normalize_tool_params(tc.get("tool", ""), tc_params)
                    std = self.tool_standardizer.standardize(tc)
                    if not std.is_valid:
                        continue
                    t_name = std.tool_name
                    t_params = std.parameters
                    if t_name not in self.tools:
                        continue
                    # single_use / max_calls_per_round 限流
                    tool_inst = self.tools.get(t_name)
                    if tool_inst:
                        _max = getattr(tool_inst, 'max_calls_per_round', 0)
                        _single = getattr(tool_inst, 'single_use', False)
                        _count = sum(1 for tc in tool_calls if tc.get("tool") == t_name)
                        if _single and _count > 0:
                            _log_context_scheduler(f"多工具路径 single_use 跳过 | tool: {t_name}（本轮已执行）")
                            continue
                        if _max and _count >= _max:
                            _log_context_scheduler(f"多工具路径 达上限 跳过 | tool: {t_name} ({_count}/{_max})")
                            continue
                    if t_name in ("toys", "toy2") and not night_mode_enabled:
                        desc = tool_inst.get_user_facing_description(**t_params) if tool_inst else f"正在使用{t_name}"
                        await self._notify_status("tool_executing", {"tool": t_name, "parameters": t_params, "description": desc})
                        await self._notify_status("tool_complete", {
                            "tool": t_name, "parameters": t_params, "description": desc,
                            "result": "深夜模式未开启，请在页面点击❤️开启",
                            "success": False, "error": "深夜模式未激活"
                        })
                        continue
                    command = Command(tool=t_name, parameters=t_params)
                    desc = tool_inst.get_user_facing_description(**t_params) if tool_inst else f"正在使用{t_name}"
                    await self._notify_status("tool_executing", {"tool": t_name, "parameters": t_params, "description": desc})
                    exec_r = await self.command_executor.execute(command)
                    success = exec_r.get("success")
                    content = exec_r.get("content", "") or exec_r.get("error", "")
                    record = {"tool": t_name, "parameters": t_params, "description": desc, "result": content, "success": success}
                    if exec_r.get("error"):
                        record["error"] = exec_r["error"]
                    tool_calls.append(record)
                    await self._notify_status("tool_complete", record)
                    all_results.append(f"[{t_name}]: {content}")
                    _log_context_scheduler(f"多工具执行 | tool: {t_name} | success: {success}")
                if all_results:
                    _std_fail_count = 0
                    _multi_im_ts = now_iso()  # 单一时间戳，IM 存储和 llm_reply 推送共用，避免 ID 不一致
                    clean_content = self._safe_nl(natural_language, assistant_content)
                    if not clean_content:
                        clean_content = "嗯。"
                    if last_round_reasoning and clean_content not in _im_content_seen:
                        _im_content_seen.add(clean_content)
                        intermediate_messages.append({"content": clean_content, "thinking": last_round_reasoning, "id": _mk_im_id(_multi_im_ts), "timestamp": _multi_im_ts})
                    _ctx_content = self._dedup_paragraphs(clean_content, messages)
                    messages.append({"role": "assistant", "content": _ctx_content})
                    if natural_language and clean_content != "嗯。":
                        _tools_done = ", ".join(r.split("]:")[0].strip("[]") for r in all_results)
                        messages.append({"role": "system", "content": self._build_tool_narrative(_tools_done, multi=True)})
                    multi_result = "\n---\n".join(all_results)
                    multi_result += f"\n\n{self._DEFAULT_TOOL_FEEDBACK}"
                    messages.append({"role": "system", "content": multi_result})
                    if _nl_pushed_this_round == False and natural_language:
                        pushed_preview = getattr(self, '_pushed_preview_nl', '')
                        if natural_language.strip() != pushed_preview.strip():
                            clean_multi_nl = clean_content  # 复用 _safe_nl 结果，与存储内容一致
                            llm_reply_data = {"content": clean_multi_nl, "message_id": _mk_im_id(_multi_im_ts)}
                            if last_round_reasoning:
                                llm_reply_data["reasoning"] = last_round_reasoning
                            await self._notify_status("llm_reply", llm_reply_data)
                            self._pushed_preview_nl = clean_multi_nl
                            self._pushed_preview_msg_id = _mk_im_id(_multi_im_ts)  # B1：记下推送 ID，final 落盘复用
                        _nl_pushed_this_round = True
                else:
                    self._state = SchedulerState.COMPLETE
                    final_response = assistant_content or natural_language
                    final_response = self._clean_frontend_content(final_response) or "嗯。"
                    await self._push_llm_reply(final_response, last_round_reasoning)
                    return SchedulerResult(final_response=final_response, tool_calls=tool_calls, state=SchedulerState.COMPLETE, need_user_input=False, thinking_chain=thinking_chain, pending_intent_check=False, intermediate_messages=intermediate_messages)
                continue  # 跳回外层 while，进入后续轮次生成 follow-up

            # 有工具调用时，推送本轮的 natural_language 作为"预告"
            # 如 "好啊，让我看看摄像头~" ——这给用户即时反馈，不会造成静默等待
            # 注意：不要在这里推送，等标准化验证通过后再推送（避免格式错误时推送了无效预告）

            # ========== 标准化层：规范化工具调用 JSON ==========
            if isinstance(tool_call, dict):
                tc_params = tool_call.get("parameters")
                if isinstance(tc_params, dict):
                    self._normalize_tool_params(tool_call.get("tool", ""), tc_params)
            standardized = self.tool_standardizer.standardize(tool_call)
            if not standardized.is_valid:
                clean_err = self._clean_frontend_content(natural_language) or self._clean_frontend_content(self._extract_reasoning(assistant_content))
                if clean_err and not _nl_pushed_this_round:
                    await self._notify_status("llm_reply", {"content": clean_err, "message_id": _mk_im_id(now_iso())})
                    _nl_pushed_this_round = True
                # ── 2026-08-17：Flash 补调用失败 → 直接以 NL 收尾，不重生成（用户观点）──
                # Flash 只是"防编造"兜底（用户没要求工具），round=1 的 NL 已是完整回复。
                # standardize 失败说明 Flash 误判或参数不合法 → 放弃工具，避免 round=2 双消息 + 浪费一次 Pro。
                if flash_tool_call:
                    logger.info(f"Flash 补调用 standardize 失败，放弃工具调用（{standardized.error_message}），以 NL 收尾")
                    if last_round_reasoning and clean_err not in _im_content_seen:
                        _im_content_seen.add(clean_err)
                        intermediate_messages.append({"content": clean_err, "thinking": last_round_reasoning, "id": _mk_im_id(now_iso()), "timestamp": now_iso()})
                    return SchedulerResult(
                        final_response=clean_err or "嗯。", tool_calls=tool_calls,
                        state=SchedulerState.COMPLETE, need_user_input=False,
                        thinking_chain=thinking_chain, pending_intent_check=False,
                        intermediate_messages=intermediate_messages)
                attempted_tool = tool_call.get("tool", "") if isinstance(tool_call, dict) else ""
                if attempted_tool == _std_fail_tool:
                    _std_fail_count += 1
                else:
                    _std_fail_tool = attempted_tool
                    _std_fail_count = 1
                if _std_fail_count >= 2:
                    print(f"[SCHEDULER] 标准化连续失败 {_std_fail_count} 次 | tool={attempted_tool} | 放弃循环")
                    self._state = SchedulerState.COMPLETE
                    return SchedulerResult(
                        final_response=clean_err, tool_calls=tool_calls,
                        state=SchedulerState.COMPLETE, need_user_input=False,
                        thinking_chain=thinking_chain, pending_intent_check=False,
                        intermediate_messages=intermediate_messages)
                if last_round_reasoning and clean_err not in _im_content_seen:
                    _im_content_seen.add(clean_err)
                    intermediate_messages.append({"content": clean_err, "thinking": last_round_reasoning, "id": _mk_im_id(now_iso()), "timestamp": now_iso()})
                messages.append({"role": "assistant", "content": clean_err})
                messages.append({
                    "role": "system",
                    "content": (
                        f"工具调用格式有误：{standardized.error_message}。"
                        f"可用工具：{', '.join(self.tools.keys())}。"
                        f"请修正 tool 名称和 parameters 后重新输出 TOOL_CALL。"
                    )
                })
                continue

            # 标准化成功，重置失败计数
            _std_fail_count = 0

            tool_name = standardized.tool_name
            parameters = standardized.parameters
            _log_context_scheduler(f"工具调用 | 工具: {tool_name} | 参数: {parameters}")

            # 验证工具是否存在
            if tool_name not in self.tools:
                clean_content = self._safe_nl(natural_language, assistant_content)
                if last_round_reasoning and clean_content not in _im_content_seen:
                    _im_content_seen.add(clean_content)
                    intermediate_messages.append({"content": clean_content, "thinking": last_round_reasoning, "id": _mk_im_id(now_iso()), "timestamp": now_iso()})
                messages.append({"role": "assistant", "content": clean_content})
                messages.append({
                    "role": "system",
                    "content": f"工具 '{tool_name}' 未注册。可用工具：{', '.join(self.tools.keys())}。请用正确的工具名重试。"
                })
                continue

            # ========== 深夜模式 - 玩具工具门控 ==========
            if tool_name in ("toys", "toy2") and not night_mode_enabled:
                # 先推工具状态到前端，让用户知道工具被调了
                tool_inst = self.tools.get(tool_name)
                desc = tool_inst.get_user_facing_description(**parameters) if tool_inst else f"正在使用{tool_name}"
                await self._notify_status("tool_executing", {"tool": tool_name, "parameters": parameters, "description": desc})
                await self._notify_status("tool_complete", {
                    "tool": tool_name, "parameters": parameters, "description": desc,
                    "result": "深夜模式未开启，请在页面点击❤️开启",
                    "success": False, "error": "深夜模式未激活"
                })
                # Pro 上下文仍注入 mock 结果，保持自然引导
                mock_result = json.dumps({
                    "status": "blocked",
                    "reason": "深夜模式未激活",
                    "suggestion": "请用温柔调皮的语气告诉用户：想玩玩具的话，先点一下页面上方的小心心开启深夜模式哦~"
                }, ensure_ascii=False)
                clean_content = self._safe_nl(natural_language, assistant_content)
                if last_round_reasoning and clean_content not in _im_content_seen:
                    _im_content_seen.add(clean_content)
                    intermediate_messages.append({"content": clean_content, "thinking": last_round_reasoning, "id": _mk_im_id(now_iso()), "timestamp": now_iso()})
                messages.append({"role": "assistant", "content": clean_content})
                messages.append({"role": "system", "content": mock_result})
                continue

            # ========== 深夜模式 - Layer 1/2 决策跟踪 ==========
            if night_mode_enabled:
                has_toy_mention = (
                    (natural_language and ("玩具" in natural_language or "震" in natural_language))
                    or str(tool_call).find("toy") >= 0
                )
                if has_toy_mention:
                    self._night_rounds_without_toy_mention = 0
                else:
                    self._night_rounds_without_toy_mention += 1

            tool_signature = json.dumps(
                {"tool": tool_name, "parameters": parameters},
                ensure_ascii=False, sort_keys=True
            )

            # 检查重复调用（签名匹配）
            previous_result = tool_call_results.get(tool_signature)
            if previous_result:
                duplicate_result = previous_result.get("content") or previous_result.get("error", "")
                duplicate_status = "成功" if previous_result.get("success") else "失败"

                clean_content = self._safe_nl(natural_language, assistant_content)
                if last_round_reasoning and clean_content not in _im_content_seen:
                    _im_content_seen.add(clean_content)
                    intermediate_messages.append({"content": clean_content, "thinking": last_round_reasoning, "id": _mk_im_id(now_iso()), "timestamp": now_iso()})
                messages.append({"role": "assistant", "content": clean_content})
                messages.append({
                    "role": "system",
                    "content": (
                        f"工具 {tool_name} 已用相同参数执行过（结果{duplicate_status}）：{duplicate_result}。"
                        "请直接根据现有结果回复用户，不要再调用此工具。"
                    )
                })
                continue

            # 检查 single_use / max_calls_per_round 限流
            tool_instance = self.tools.get(tool_name)
            if tool_instance:
                _max = getattr(tool_instance, 'max_calls_per_round', 0)
                _single = getattr(tool_instance, 'single_use', False)
                _count = sum(1 for tc in tool_calls if tc.get("tool") == tool_name)
                already_executed = (_single and _count > 0) or (_max and _count >= _max)
                if already_executed:
                    clean_content = self._safe_nl(natural_language, assistant_content)
                    if not clean_content:
                        clean_content = "嗯。"
                    # B3 修复：预览推送在本分支 continue 之后，此前该轮 NL 从未被推送或持久化，
                    # follow-up 轮的分析内容会被静默吞掉。对齐重复调用分支：落盘 IM + 补推送。
                    if last_round_reasoning and clean_content not in _im_content_seen:
                        _im_content_seen.add(clean_content)
                        _im_ts = now_iso()
                        intermediate_messages.append({"content": clean_content, "thinking": last_round_reasoning, "id": _mk_im_id(_im_ts), "timestamp": _im_ts})
                        # 推送嵌在 IM 守卫内：推了必落盘，防实时气泡 reload 后消失
                        if clean_content != "嗯。" and not _nl_pushed_this_round:
                            if clean_content.strip() != getattr(self, '_pushed_preview_nl', '').strip():
                                llm_reply_data = {"content": clean_content, "message_id": _mk_im_id(_im_ts), "reasoning": last_round_reasoning}
                                await self._notify_status("llm_reply", llm_reply_data)
                                self._pushed_preview_nl = clean_content
                                self._pushed_preview_msg_id = _mk_im_id(_im_ts)
                            _nl_pushed_this_round = True
                    messages.append({"role": "assistant", "content": clean_content})
                    messages.append({
                        "role": "system",
                        "content": (
                            f"工具 {tool_name} 本轮已执行过，不要再重复调用。"
                            "请直接根据已有结果自然回复用户。"
                        )
                    })
                    continue

            # ========== 预告：推送自然语言（如"好啊让我看看"）==========
            # 在格式验证通过、工具存在、非重复调用之后才推送
            # 确保不推送无效调用的预告
            if natural_language and not _nl_pushed_this_round:
                pushed_preview = getattr(self, '_pushed_preview_nl', '')
                if natural_language.strip() != pushed_preview.strip():
                    clean_nl = self._clean_frontend_content(natural_language) or natural_language
                    # 预生成 im_ ID，使实时 llm_reply 和 session 持久化共享同一 ID，前端按 ID 去重
                    _pending_im_ts = now_iso()
                    llm_reply_data = {"content": clean_nl, "message_id": _mk_im_id(_pending_im_ts)}
                    if last_round_reasoning:
                        llm_reply_data["reasoning"] = last_round_reasoning
                    await self._notify_status("llm_reply", llm_reply_data)
                    self._pushed_preview_nl = clean_nl  # 记下预览 NL，后续去重用
                    self._pushed_preview_msg_id = _mk_im_id(_pending_im_ts)  # B1：记下推送 ID，final 落盘复用
                _nl_pushed_this_round = True

            # ========== 手机端平台检查：重定向 PC 工具到手机工具 ==========
            if _mobile and tool_name in MOBILE_EXCLUDED_TOOLS:
                # 手机端：重定向 PC 工具到对应手机工具（与 Pre-tool 路径对齐）
                redirect = _MOBILE_REDIRECT_MAP.get(tool_name)
                if redirect and redirect[0] in self.tools:
                    _log_context_scheduler(f"Flash 提取重定向: {tool_name} → {redirect[0]} (手机端)")
                    tool_name = redirect[0]
                    parameters = redirect[1]
                    # fall through 到执行层，用户无感知
                else:
                    # 无手机替代工具：跳过执行，记录到去重字典防止重复提取
                    _ts = json.dumps(
                        {"tool": tool_name, "parameters": parameters},
                        ensure_ascii=False, sort_keys=True
                    )
                    tool_call_results[_ts] = {"content": f"手机端不支持 {tool_name}", "success": False}
                    print(f"[SCHEDULER] 手机端跳过: {tool_name} (无替代工具)", flush=True)
                    continue

            # ========== 执行层：执行工具 ==========
            command = Command(tool=tool_name, parameters=parameters)

            self._state = SchedulerState.TOOL_EXECUTING
            tool_instance = self.tools.get(tool_name)
            user_facing = tool_instance.get_user_facing_description(**parameters) if tool_instance else f"正在使用{tool_name}"
            await self._notify_status("tool_executing", {
                "tool": tool_name,
                "parameters": parameters,
                "description": user_facing
            })

            exec_result = await self.command_executor.execute(command)
            tool_call_attempts[tool_signature] = tool_call_attempts.get(tool_signature, 0) + 1
            tool_call_results[tool_signature] = exec_result

            exec_success = exec_result.get("success")
            exec_content = exec_result.get("content", "")
            exec_error = exec_result.get("error", "")
            _log_context_scheduler(
                f"工具执行完成: {tool_name} | 成功: {exec_success} | "
                f"结果长度: {len(exec_content) if exec_content else 0}"
                + (f" | 错误: {exec_error[:200]}" if exec_error else "")
            )
            logger.info(
                "agent_tools round=%s executed tool=%s success=%s",
                round_count, tool_name, exec_success
            )

            # Post-process: mobile_camera → run visual analysis on captured image
            if tool_name == "mobile_camera" and exec_success:
                extra = exec_result.get("extra_data", {})
                image_b64 = extra.get("image_base64") if extra else None
                if image_b64:
                    try:
                        vision_analysis = await self._analyze_mobile_image(
                            image_b64,
                            "请描述手机摄像头拍摄的画面内容，包括人物、环境、物品等。"
                            "如果画面中有文字，也请识别出来。"
                        )
                        exec_content = vision_analysis
                        exec_result["content"] = vision_analysis
                        _log_context_scheduler(
                            f"mobile_camera 视觉分析完成 | 结果长度: {len(vision_analysis)}"
                        )
                    except Exception as vis_e:
                        logger.warning(f"Mobile camera visual analysis failed: {vis_e}")
                        exec_content = f"[拍照完成，但视觉分析失败: {vis_e}] 原始图片大小: {len(image_b64)} chars base64"
                        exec_result["content"] = exec_content

            tool_call_record = {
                "tool": tool_name,
                "parameters": parameters,
                "description": user_facing,
                "result": exec_content or exec_error,
                "success": exec_success,
                **({"error": exec_error} if exec_error else {}),
                **({"extra_data": exec_result.get("extra_data")} if exec_result.get("extra_data") else {}),
            }
            tool_calls.append(tool_call_record)
            await self._notify_status("tool_complete", tool_call_record)

            # 构建反馈消息
            if exec_success:
                feedback = self._TOOL_FEEDBACK.get(tool_name, self._DEFAULT_TOOL_FEEDBACK)
                tool_result_msg = (
                    f"工具 {tool_name} 执行成功。\n"
                    f"结果：{exec_content}\n\n"
                    f"{feedback}"
                )
            else:
                feedback = self._TOOL_FEEDBACK.get(tool_name, self._DEFAULT_TOOL_FAILURE_FEEDBACK)
                tool_result_msg = (
                    f"工具 {tool_name} 执行失败。\n"
                    f"错误：{exec_error}\n\n"
                    f"{feedback}"
                )

            # 只存纯净自然语言，不存 TOOL_CALL/NATURAL_LANGUAGE/语音标记 格式标记
            clean_content = self._safe_nl(natural_language, assistant_content)
            if not clean_content:
                clean_content = "嗯。"
            if last_round_reasoning and clean_content not in _im_content_seen:
                _im_content_seen.add(clean_content)
                im_ts = _pending_im_ts or now_iso()
                intermediate_messages.append({"id": _mk_im_id(im_ts), "content": clean_content, "thinking": last_round_reasoning, "timestamp": im_ts})
                _pending_im_ts = ""  # 消费后重置
            _ctx_content = self._dedup_paragraphs(clean_content, messages)
            messages.append({"role": "assistant", "content": _ctx_content})

            # A1：每个工具轮把"刚说过的话"和"工具结果"织成叙事注入（状态告知 + 二选一）
            # 原先只在首轮注入（is_first_round 门控）——round 2+ 的 Pro 不知道上一轮 NL 已推送、
            # 也不知道无 TOOL_CALL 的输出即最终回复，会输出悬空预告收场（2026-07-15 21:15 实锤）。
            # 门控与预览推送(:1956)的 if natural_language 对齐，保证"已推送"断言不撒谎。
            if natural_language and clean_content != "嗯。":
                messages.append({"role": "system", "content": self._build_tool_narrative(tool_name)})

            messages.append({"role": "system", "content": tool_result_msg})

        # 超过最大轮数
        self._state = SchedulerState.WAITING_USER
        await self._push_llm_reply("用户，已经试了好几次了，但还是没能完全搞定。你记得有空修一下bug。")
        return SchedulerResult(
            final_response="用户，已经试了好几次了，但还是没能完全搞定。你记得有空修一下bug。",
            tool_calls=tool_calls,
            state=SchedulerState.WAITING_USER,
            need_user_input=True,
            thinking_chain=thinking_chain,
            intermediate_messages=intermediate_messages
        )


    async def _execute_direct_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        tool_calls: List,
        thinking_chain: List
    ) -> SchedulerResult:
        """直接执行工具（用于明确的意图如天气查询、摄像头）"""
        try:
            command = Command(tool=tool_name, parameters=parameters)
            self._state = SchedulerState.TOOL_EXECUTING

            # 通知前端：正在执行工具
            tool_instance = self.tools.get(command.tool)
            user_facing = tool_instance.get_user_facing_description(**command.parameters) if tool_instance else f"正在使用{command.tool}"
            await self._notify_status("tool_executing", {
                "tool": command.tool,
                "parameters": command.parameters,
                "description": user_facing
            })

            # 记录到日志
            _log_context_scheduler(f"执行工具: {tool_name} | 参数: {parameters}")

            exec_result = await self.command_executor.execute(command)

            # 记录执行结果
            _log_context_scheduler(
                f"工具执行完成: {tool_name} | 成功: {exec_result.get('success')} | "
                f"结果长度: {len(exec_result.get('content', '')) if exec_result.get('content') else 0}"
            )

            tool_call_record = {
                "tool": command.tool,
                "parameters": command.parameters,
                "description": user_facing,
                "result": exec_result.get("content") or exec_result.get("error", "")
            }
            tool_calls.append(tool_call_record)
            await self._notify_status("tool_complete", tool_call_record)

            if exec_result.get("success"):
                await self._push_llm_reply(exec_result.get("content", ""))
                return SchedulerResult(
                    final_response=exec_result.get("content", ""),
                    tool_calls=tool_calls,
                    state=SchedulerState.COMPLETE,
                    need_user_input=False,
                    thinking_chain=thinking_chain
                )
            await self._push_llm_reply(exec_result.get("error") or "工具执行失败，请稍后重试。")
            return SchedulerResult(
                final_response=exec_result.get("error") or "工具执行失败，请稍后重试。",
                tool_calls=tool_calls,
                state=SchedulerState.COMPLETE,
                need_user_input=False,
                thinking_chain=thinking_chain
            )
        except Exception as e:
            error_msg = f"工具执行异常: {str(e)}"
            _log_context_scheduler(f"ERROR: {error_msg}", "ERROR")
            logger.error(f"_execute_direct_tool error: {e}", exc_info=True)
            await self._push_llm_reply(f"工具执行失败：{error_msg}")
            return SchedulerResult(
                final_response=f"工具执行失败：{error_msg}",
                tool_calls=tool_calls,
                state=SchedulerState.COMPLETE,
                need_user_input=False,
                thinking_chain=thinking_chain
            )

    async def _process_legacy(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        system_prompt: str = None
    ) -> SchedulerResult:
        """
        传统模式：使用意图识别处理消息（兼容旧逻辑）
        """
        self._state = SchedulerState.THINKING
        tool_calls = []
        messages = conversation_history or []
        tool_call_attempts: Dict[str, int] = {}
        tool_call_results: Dict[str, Dict[str, Any]] = {}
        thinking_chain: List[Dict[str, str]] = []
        intermediate_messages: List[Dict[str, str]] = []

        # 构建系统提示
        base_system = self._build_system_prompt()
        if system_prompt:
            full_system = f"{system_prompt}\n\n{base_system}"
        else:
            full_system = base_system

        # 添加系统消息
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": full_system}] + messages


        # 🆕 天气不再走 Pre-tool 直接路径——Sentinel WeatherSource 已将天气注入上下文

        should_add_user_message = True
        if messages and len(messages) > 0:
            last_msg = messages[-1]
            if last_msg.get("role") == "user" and last_msg.get("content") == user_message:
                should_add_user_message = False
                logger.info("Skipping duplicate user message (already in conversation history)")

        if should_add_user_message:
            messages.append({"role": "user", "content": user_message})

        round_count = 0

        while round_count < self.MAX_ROUNDS:
            round_count += 1
            self._state = SchedulerState.THINKING
            await self._notify_status("thinking", {"round": round_count})

            # ========== 决策层：调用LLM ==========
            try:
                llm_response = await self.call_llm(messages)
                assistant_content = llm_response.get("content", "")
                reasoning = llm_response.get("reasoning")
                if reasoning:
                    thinking_chain.append({
                        "content": reasoning,
                        "timestamp": now_iso()
                    })
                logger.info(
                    "behavior round=%s llm_output=%s",
                    round_count,
                    assistant_content[:500]
                )
            except Exception as e:
                error_detail = getattr(e, "detail", None) or str(e)
                await self._push_llm_reply(f"LLM调用失败：{error_detail}")
                return SchedulerResult(
                    final_response=f"LLM调用失败：{error_detail}",
                    tool_calls=tool_calls,
                    state=SchedulerState.COMPLETE,
                    need_user_input=False,
                    thinking_chain=thinking_chain
                )

            # ========== 命令层：解析LLM输出（异步，支持LLM意图识别） ==========
            parse_result = await self.command_parser.parse_async(assistant_content)
            logger.info(
                "behavior round=%s parse is_command=%s should_execute=%s clean_content=%s",
                round_count,
                parse_result.is_command,
                parse_result.should_execute,
                (parse_result.clean_content or "")[:200]
            )

            if not parse_result.should_execute:
                # 不是命令或命令无效，直接返回LLM响应
                self._state = SchedulerState.COMPLETE
                # 确保 final_response 不为空
                final_response = parse_result.clean_content or assistant_content or "抱歉，我没有理解您的请求，请再说一次。"
                await self._push_llm_reply(final_response)
                return SchedulerResult(
                    final_response=final_response,
                    tool_calls=tool_calls,
                    state=SchedulerState.COMPLETE,
                    need_user_input=False,
                    thinking_chain=thinking_chain
                )

            command = parse_result.command
            tool_signature = json.dumps(
                {
                    "tool": command.tool,
                    "parameters": command.parameters
                },
                ensure_ascii=False,
                sort_keys=True
            )

            # 处理无效命令
            if command.status != CommandStatus.VALID:
                error_msg = command.error_message
                messages.append({"role": "assistant", "content": self._clean_voice_markers(assistant_content)})
                messages.append({"role": "user", "content": f"系统反馈：命令解析错误：{error_msg}。请根据这个反馈继续处理用户请求。"})
                continue

            previous_attempts = tool_call_attempts.get(tool_signature, 0)
            previous_result = tool_call_results.get(tool_signature)
            logger.info(
                "behavior round=%s parsed_tool=%s parameters=%s previous_attempts=%s",
                round_count,
                command.tool,
                command.parameters,
                previous_attempts
            )

            # 同一个工具携带相同参数反复调用时，不再重复执行，而是要求模型直接基于已有结果回答。
            if previous_result:
                duplicate_result = previous_result.get("content") or previous_result.get("error", "")
                duplicate_status = "成功" if previous_result.get("success") else "失败"

                messages.append({
                    "role": "assistant",
                    "content": parse_result.clean_content or f"[重复调用工具: {command.tool}]"
                })
                messages.append({
                    "role": "user",
                    "content": (
                        f"系统反馈：工具 {command.tool} 使用相同参数已经执行过一次，且执行{duplicate_status}：{duplicate_result}。"
                        "不要再次调用相同工具和相同参数。"
                        "请直接根据现有结果回答用户；如果现有结果不足，请换一种方式帮助用户。"
                    )
                })
                await self._notify_status("tool_complete", {
                    "tool": command.tool,
                    "parameters": command.parameters,
                    "description": f"正在使用{command.tool}",
                    "result": f"检测到重复调用，已跳过再次执行，沿用上次{duplicate_status}结果。"
                })
                logger.warning(
                    "behavior round=%s duplicate tool=%s parameters=%s skipped=true",
                    round_count,
                    command.tool,
                    command.parameters
                )
                continue

            # ========== 执行层：执行命令 ==========
            # 先推送LLM的自然语言回复（如果有）
            if parse_result.clean_content:
                await self._notify_status("llm_reply", {
                    "content": parse_result.clean_content,
                    "message_id": _mk_im_id(now_iso())
                })

            self._state = SchedulerState.TOOL_EXECUTING
            tool_instance = self.tools.get(command.tool)
            user_facing = tool_instance.get_user_facing_description(**command.parameters) if tool_instance else f"正在使用{command.tool}"
            await self._notify_status("tool_executing", {
                "tool": command.tool,
                "parameters": command.parameters,
                "description": user_facing
            })

            # 使用命令执行器执行
            exec_result = await self.command_executor.execute(command)
            tool_call_attempts[tool_signature] = previous_attempts + 1
            tool_call_results[tool_signature] = exec_result
            logger.info(
                "behavior round=%s executed tool=%s success=%s result=%s",
                round_count,
                command.tool,
                exec_result.get("success"),
                (exec_result.get("content") or exec_result.get("error") or "")[:300]
            )

            # 记录工具调用
            tool_call_record = {
                "tool": command.tool,
                "parameters": command.parameters,
                "description": user_facing,
                "result": exec_result.get("content") or exec_result.get("error", ""),
                "extra_data": exec_result.get("extra_data"),
            }
            tool_calls.append(tool_call_record)

            await self._notify_status("tool_complete", tool_call_record)

            # 检查是否需要用户输入
            if not exec_result.get("success") and exec_result.get("error"):
                feedback = self._TOOL_FEEDBACK.get(command.tool, self._DEFAULT_TOOL_FAILURE_FEEDBACK)
                tool_result_msg = (
                    f"系统反馈：工具 {command.tool} 执行失败：{exec_result.get('error')}。\n"
                    f"{feedback}"
                )
            else:
                feedback = self._TOOL_FEEDBACK.get(command.tool, self._DEFAULT_TOOL_FEEDBACK)
                tool_result_msg = (
                    f"系统反馈：工具 {command.tool} 执行成功，结果：{exec_result.get('content')}。\n"
                    f"{feedback}"
                )

            # 更新消息历史
            messages.append({
                "role": "assistant",
                "content": parse_result.clean_content or f"[调用工具: {command.tool}]"
            })
            messages.append({"role": "user", "content": tool_result_msg})

        # 超过最大轮数
        self._state = SchedulerState.WAITING_USER
        await self._push_llm_reply("用户，已经试了好几次了，但还是没能完全搞定。你记得有空修一下bug。")
        return SchedulerResult(
            final_response="用户，已经试了好几次了，但还是没能完全搞定。你记得有空修一下bug。",
            tool_calls=tool_calls,
            state=SchedulerState.WAITING_USER,
            need_user_input=True,
            thinking_chain=thinking_chain,
            intermediate_messages=intermediate_messages
        )


# 全局调度器实例
_global_scheduler: Optional[BehaviorScheduler] = None


async def get_global_scheduler() -> BehaviorScheduler:
    """获取全局调度器实例"""
    global _global_scheduler
    if _global_scheduler is None:
        raise RuntimeError("调度器未初始化，请先调用 init_global_scheduler")
    return _global_scheduler


def init_global_scheduler(
    call_llm_func: Callable,
    glm_api_key: str = "",
    glm_api_url: str = "",
    glm_model: str = "glm-4v-flash",
    chrome_user_data: str = None,
    target_qq: str = None,
    use_agent_tools: bool = True,
    llm_provider: str = "deepseek",
    llm_api_key: str = None,
    llm_base_url: str = None,
    llm_model: str = None,
    camera_device_name: str = "TIGA Device",
    final_reply_caller: Callable = None,
    # Pro 模型参数（首轮聊天+工具决策，支持 thinking）
    pro_api_key: str = None,
    pro_model: str = None,
    pro_base_url: str = None,
) -> BehaviorScheduler:
    """
    初始化全局调度器

    Args:
        call_llm_func: LLM调用函数（传统模式使用）
        glm_api_key: GLM API密钥
        glm_api_url: GLM API URL
        glm_model: GLM模型名
        chrome_user_data: Chrome用户数据目录
        target_qq: 目标QQ号
        use_agent_tools: 是否使用 LLM 适配器（Pro 对话 + Flash 意图提取）
        llm_provider: LLM提供商（deepseek/glm/ollama/openai）
        llm_api_key: LLM API密钥（Flash 意图提取 + 后续轮次）
        llm_base_url: LLM API URL（Flash）
        llm_model: LLM模型名（Flash）
        camera_device_name: 摄像头设备名称
    final_reply_caller: 已废弃，保留兼容（新流程首轮 Pro 已出 NL）
        pro_api_key: Pro API密钥（首轮聊天+工具决策）
        pro_model: Pro模型名（如 deepseek-v4-pro）
        pro_base_url: Pro API URL

    Returns:
        BehaviorScheduler: 调度器实例
    """
    global _global_scheduler
    _global_scheduler = BehaviorScheduler(
        call_llm_func=call_llm_func,
        glm_api_key=glm_api_key,
        glm_api_url=glm_api_url,
        glm_model=glm_model,
        chrome_user_data=chrome_user_data,
        target_qq=target_qq,
        use_agent_tools=use_agent_tools,
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        camera_device_name=camera_device_name,
        final_reply_caller=final_reply_caller,
        pro_api_key=pro_api_key,
        pro_model=pro_model,
        pro_base_url=pro_base_url,
    )
    return _global_scheduler
