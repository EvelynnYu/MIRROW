"""上下文展示边界的纯文本处理。

这些函数只处理送入 LLM 的展示文本，不修改 OB_Rev 原文、索引或数据库内容。
"""

import json
import re
from typing import Any, Optional


_OBSIDIAN_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_TOOL_HISTORY_MARKER = "[历史行为事实]"
_LEGACY_TOOL_MARKERS = ("[工具行为]", "[工具调用摘要]", "[工具执行事实]")
_LEGACY_TOOL_RECORD_RE = re.compile(
    r"^\s*[A-Za-z_][A-Za-z0-9_-]*\s*[（(](?:已调用|成功|失败)[）)]\s*[:：].*$"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_DATA_URL_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)
_LEGACY_SPEAKER_PREFIXES = {
    "user": ("洛月凝", "人类伙伴", "user"),
    "assistant": ("Agent", "assistant"),
}

# 这些是开发/基础设施能力，结果可能直接包含本机文件、命令输出或网络拓扑。
# 即使旧记录意外把它们写进 tool_calls，也不把结果送给外部 LLM。
_NON_CONTEXT_TOOLS = {
    "shell", "http_request", "file", "advanced_file", "code_runner",
    "package_manager", "documentation_check",
}

# 书柜检索是一次性历史材料；《性爱大全》抽卡则是 Agent 真实做过的
# 创作事件，行为事实应留下，完整结果仍只按需续接。
_EPHEMERAL_HISTORY_TOOLS = {"bookshelf", "ambient_event_link"}

# 这是展示层的有限词表，不是工具的执行注册表。未列出的工具宁可使用
# 中性描述，也不把内部函数名泄露给模型或成为 Agent 的语言范例。
_TOOL_LABELS = {
    "taobao_roam": "逛淘宝并保存本地收藏",
    "generate_image": "生成图片", "send_voice": "生成语音消息",
    "web_search": "检索公开资料", "get_weather": "查询天气",
    "check_phone": "查看手机状态", "eyes": "进行视觉观察",
    "band": "读取健康数据", "cloud_music": "操作音乐播放",
    "manage_calendar": "处理日历事项", "manage_ledger": "处理账本事项",
    "create_scheduled_task": "安排提醒", "manage_scheduled_task": "处理提醒事项",
    "rift_read_archive": "翻阅虚构案件目录",
    "rift_start_case": "开始一宗虚构互动案件",
    "rift_get_state": "核对虚构案件局面",
    "rift_take_action": "在虚构互动案件中执行一步行动",
}


def strip_obsidian_wikilinks(text: str) -> str:
    """将 Obsidian wikilink 转为 LLM 可读文本。

    ``[[target|alias]]`` 显示 alias，``[[target]]`` 显示 target；嵌入式
    ``![[asset]]`` 同样去掉 ``!``，避免把 Obsidian 语法残留给模型。
    普通 ``[状态]`` 和 Markdown ``[text](url)`` 不会被匹配。
    """
    if not isinstance(text, str) or not text:
        return text or ""

    def _replace(match: re.Match) -> str:
        target = match.group(2).strip()
        if "|" in target:
            target = target.rsplit("|", 1)[1].strip()
        return target

    return _OBSIDIAN_WIKILINK_RE.sub(_replace, text)


def strip_legacy_speaker_prefix(content: Any, role: str) -> str:
    """Remove only a known old speaker label at the start of a message."""

    text = str(content or "")
    labels = _LEGACY_SPEAKER_PREFIXES.get(str(role or "").lower(), ())
    if not labels:
        return text
    label_pattern = "|".join(re.escape(label) for label in labels)
    return re.sub(
        rf"^\s*(?:{label_pattern})\s*[:：]\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )


def _redact_tool_fact(value: Any) -> str:
    """把工具结果压成可注入的单行事实，并遮蔽常见密钥/IP/大字段。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value or "")
    text = _DATA_URL_RE.sub("[图片数据已省略]", text)
    text = _BEARER_RE.sub("Bearer [已隐藏]", text)
    text = _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}=[已隐藏]", text)
    text = _IPV4_RE.sub("[IP已隐藏]", text)
    return re.sub(r"\s+", " ", text).strip()


def _tool_fact_status(call: dict) -> str:
    """兼容同步工具与异步附件的语义状态。"""
    extra = call.get("extra_data") or {}
    attachment = extra.get("attachment_status") if isinstance(extra, dict) else ""
    raw = str(attachment or call.get("status") or "").lower()
    if raw in {"pending", "processing", "running"}:
        return "进行中"
    if raw in {"ready", "success", "completed", "complete"}:
        return "成功"
    if raw in {"failed", "error", "cancelled"}:
        return "失败"
    if call.get("success") is True:
        return "成功"
    if call.get("success") is False:
        return "失败"
    return "已完成"


def build_tool_behavior_facts(tool_calls: Any) -> str:
    """生成仅含客观事实的自然语言历史摘要。

    不读取参数、结果、错误或附件；内部工具名只用于本地映射为自然语言能力。
    """
    if not tool_calls:
        return ""
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not isinstance(tool_calls, list):
        return ""

    facts = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool") or call.get("name") or "").strip()
        if not name or name in _NON_CONTEXT_TOOLS or name in _EPHEMERAL_HISTORY_TOOLS:
            continue
        if name == "intimacy_book":
            status = _tool_fact_status(call)
            params = call.get("parameters") or {}
            action = str(params.get("action") or "") if isinstance(params, dict) else ""
            verb = "随机组合抽卡" if action == "combine" else "随机查询写作素材"
            facts.append(f"Agent曾翻阅《性爱大全》并{verb}。结果：{status}。")
            if len(facts) >= 4:
                break
            continue
        if name == 'band_touch':
            if (call.get('extra_data') or {}).get('band_notice_id'):
                continue  # The independently persisted notification is the history authority.
            from miband.history import action_fact
            facts.append(_redact_tool_fact(action_fact(call)))
            if len(facts) >= 4:
                break
            continue
        status = _tool_fact_status(call)
        label = _TOOL_LABELS.get(name, "执行一项操作")
        # description 是本地工具生成的短行为说明；结果、错误、参数与 extra_data
        # 均不读取。UI-only 也属于 Agent 已做过的行为，因此同样保留。
        description = _redact_tool_fact(call.get("description"))[:100]
        sentence = f"Agent曾{label}。结果：{status}。"
        if description:
            sentence += f"内容摘要：{description}。"
        facts.append(sentence)
        if len(facts) >= 4:
            break
    return "；".join(facts)[:600]


def build_tool_history_fact(role: str, tool_summary: Optional[str], tool_calls: Any = None, event_type: str = '') -> str:
    from lounge_visits.ui_action import invitation_fact, is_invitation
    if is_invitation(role, event_type):
        return f'{_TOOL_HISTORY_MARKER} {invitation_fact()}'
    """为一条 assistant 历史消息建立紧邻的独立 system 事实。"""
    if role == 'user' and event_type == 'ui_action:browse_taobao':
        from taobao_roam.invitation import SOURCE_FACT
        return f'{_TOOL_HISTORY_MARKER} {SOURCE_FACT}'
    if role != "assistant":
        return ""
    parsed_calls = tool_calls
    if isinstance(parsed_calls, str):
        try:
            parsed_calls = json.loads(parsed_calls)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_calls = None
    if isinstance(parsed_calls, dict):
        parsed_calls = [parsed_calls]
    call_names = {
        str(call.get("tool") or call.get("name") or "").strip()
        for call in (parsed_calls or []) if isinstance(call, dict)
    }
    if call_names and call_names.issubset(_EPHEMERAL_HISTORY_TOOLS):
        return ""
    facts = build_tool_behavior_facts(tool_calls)
    from lounge_visits.context import history_fact as lounge_history_fact
    lounge = lounge_history_fact(tool_calls)
    if lounge:
        return f'{_TOOL_HISTORY_MARKER} {_redact_tool_fact(lounge)}'
    # The original outing is the authority; the card only supplies its stable ID.
    from taobao_roam.context import shopping_history_fact
    shopping = shopping_history_fact(tool_calls)
    if shopping:
        return f"{_TOOL_HISTORY_MARKER} {facts} {_redact_tool_fact(shopping)}"
    if facts:
        # 保持单行：若模型偶发复述，最终输出边界可以原子剥离整条事实，
        # 不会只删标记却把后半句留在可见回复中。
        return f"{_TOOL_HISTORY_MARKER} {facts}"
    if not tool_summary:
        return ""
    summary = re.sub(r"\s+", " ", str(tool_summary)).strip()
    # 旧摘要是展示协议，不能复述其工具名、括号或“已调用”。只保留其事实存在。
    if not summary:
        return ""
    return f"{_TOOL_HISTORY_MARKER} Agent曾执行一项操作。结果：已完成。"


def append_tool_summary(content: str, role: str, tool_summary: Optional[str], tool_calls: Any = None) -> str:
    """兼容旧调用方：正文永远不再拼接工具历史。"""
    return content


def strip_internal_history_markers(content: Any) -> str:
    """清除内部历史事实及旧版伪工具记录的整行文本。"""
    text = str(content or "")
    lines = text.splitlines()
    clean = [
        line for line in lines
        if not any(marker in line for marker in (_TOOL_HISTORY_MARKER, *_LEGACY_TOOL_MARKERS))
        and not _LEGACY_TOOL_RECORD_RE.match(line)
    ]
    return "\n".join(clean).strip()


__all__ = [
    "strip_obsidian_wikilinks", "build_tool_behavior_facts", "build_tool_history_fact",
    "append_tool_summary", "strip_internal_history_markers", "strip_legacy_speaker_prefix",
]
