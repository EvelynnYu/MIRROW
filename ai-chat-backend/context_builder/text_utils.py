"""上下文展示边界的纯文本处理。

这些函数只处理送入 LLM 的展示文本，不修改 OB_Rev 原文、索引或数据库内容。
"""
import json
import re
from typing import Any, Optional
_OBSIDIAN_WIKILINK_RE = re.compile('(!?)\\[\\[([^\\]\\n]+)\\]\\]')
_TOOL_HISTORY_MARKER = '[历史行为事实]'
_LEGACY_TOOL_MARKERS = ('[工具行为]', '[工具调用摘要]', '[工具执行事实]')
_HISTORY_FENCE_RE = re.compile('^ {0,3}(`{3,}|~{3,})(.*)$')
_LEGACY_TOOL_RECORD_RE = re.compile('^\\s*[A-Za-z_][A-Za-z0-9_-]*\\s*[（(](?:已调用|成功|失败)[）)]\\s*[:：].*$')
_SECRET_VALUE_RE = re.compile('(?i)\\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)\\s*[:=]\\s*([^\\s,;]+)')
_BEARER_RE = re.compile('(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]+')
_IPV4_RE = re.compile('(?<!\\d)(?:\\d{1,3}\\.){3}\\d{1,3}(?!\\d)')
_DATA_URL_RE = re.compile('data:[^;,\\s]+;base64,[A-Za-z0-9+/=]+', re.IGNORECASE)
_LEGACY_SPEAKER_PREFIXES = {'user': ('人类伙伴', '人类伙伴', 'user'), 'assistant': ('AI', 'assistant')}
_NON_CONTEXT_TOOLS = {'shell', 'http_request', 'file', 'advanced_file', 'code_runner', 'package_manager', 'documentation_check'}
_EPHEMERAL_HISTORY_TOOLS = {'bookshelf', 'ambient_event_link'}
_TOOL_LABELS = {'xiaohongshu_interactions': '查看或回复小红书评论与艾特', 'send_voice': '生成语音消息', 'web_search': '检索公开资料', 'get_weather': '查询天气', 'check_phone': '查看手机状态', 'eyes': '进行视觉观察', 'band': '读取健康数据', 'cloud_music': '操作音乐播放', 'manage_calendar': '处理日历事项', 'manage_ledger': '处理账本事项', 'create_scheduled_task': '安排提醒', 'manage_scheduled_task': '处理提醒事项', 'schedule_self_task': '安排后续查岗或行动', 'rift_read_archive': '翻阅虚构案件目录', 'rift_start_case': '开始一宗虚构互动案件', 'rift_get_state': '核对虚构案件局面', 'rift_take_action': '在虚构互动案件中执行一步行动'}

def strip_obsidian_wikilinks(text: str) -> str:
    """将 Obsidian wikilink 转为 LLM 可读文本。

    ``[[target|alias]]`` 显示 alias，``[[target]]`` 显示 target；嵌入式
    ``![[asset]]`` 同样去掉 ``!``，避免把 Obsidian 语法残留给模型。
    普通 ``[状态]`` 和 Markdown ``[text](url)`` 不会被匹配。
    """
    if not isinstance(text, str) or not text:
        return text or ''

    def _replace(match: re.Match) -> str:
        target = match.group(2).strip()
        if '|' in target:
            target = target.rsplit('|', 1)[1].strip()
        return target
    return _OBSIDIAN_WIKILINK_RE.sub(_replace, text)

def strip_legacy_speaker_prefix(content: Any, role: str) -> str:
    """Remove only a known old speaker label at the start of a message."""
    text = str(content or '')
    labels = _LEGACY_SPEAKER_PREFIXES.get(str(role or '').lower(), ())
    if not labels:
        return text
    label_pattern = '|'.join((re.escape(label) for label in labels))
    return re.sub(f'^\\s*(?:{label_pattern})\\s*[:：]\\s*', '', text, count=1, flags=re.IGNORECASE)

def _redact_tool_fact(value: Any) -> str:
    """把工具结果压成可注入的单行事实，并遮蔽常见密钥/IP/大字段。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value or '')
    text = _DATA_URL_RE.sub('[图片数据已省略]', text)
    text = _BEARER_RE.sub('Bearer [已隐藏]', text)
    text = _SECRET_VALUE_RE.sub(lambda m: f'{m.group(1)}=[已隐藏]', text)
    text = _IPV4_RE.sub('[IP已隐藏]', text)
    return re.sub('\\s+', ' ', text).strip()

def _tool_fact_status(call: dict) -> str:
    """兼容同步工具与异步附件的语义状态。"""
    extra = call.get('extra_data') or {}
    attachment = extra.get('attachment_status') if isinstance(extra, dict) else ''
    raw = str(attachment or call.get('status') or '').lower()
    if raw in {'pending', 'processing', 'running'}:
        return '进行中'
    if raw in {'ready', 'success', 'completed', 'complete'}:
        return '成功'
    if raw in {'failed', 'error', 'cancelled'}:
        return '失败'
    if call.get('success') is True:
        return '成功'
    if call.get('success') is False:
        return '失败'
    return '已完成'

def build_tool_behavior_facts(tool_calls: Any) -> str:
    """生成仅含客观事实的自然语言历史摘要。

    不读取参数、结果、错误或附件；内部工具名只用于本地映射为自然语言能力。
    """
    if not tool_calls:
        return ''
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ''
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not isinstance(tool_calls, list):
        return ''
    facts = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get('tool') or call.get('name') or '').strip()
        if not name or name in _NON_CONTEXT_TOOLS or name in _EPHEMERAL_HISTORY_TOOLS:
            continue
        if name == 'book_choices':
            facts.append('AI提供过快捷回复候选。')
            continue
        if name == 'band_touch':
            if (call.get('extra_data') or {}).get('band_notice_id'):
                continue
            from miband.history import action_fact
            facts.append(_redact_tool_fact(action_fact(call)))
            if len(facts) >= 4:
                break
            continue
        if name == 'home_board' and (call.get('extra_data') or {}).get('home_board_committed'):
            continue
        status = _tool_fact_status(call)
        label = _TOOL_LABELS.get(name, '执行一项操作')
        description = _redact_tool_fact(call.get('description'))[:100]
        sentence = f'AI曾{label}。结果：{status}。'
        if description:
            sentence += f'内容摘要：{description}。'
        facts.append(sentence)
        if len(facts) >= 4:
            break
    return '；'.join(facts)[:600]

def build_tool_history_fact(role: str, tool_summary: Optional[str], tool_calls: Any=None, event_type: str='') -> str:
    """从真实回执建立历史事实；tool_summary 仅保留调用兼容，不作为执行证据。"""
    if role == 'user' and event_type == 'ui_action:visit_workshop':
        from workshop.ui_action import invitation_fact as workshop_invitation_fact
        return f'{_TOOL_HISTORY_MARKER} {workshop_invitation_fact()}'
    if role == 'user' and event_type == 'ui_action:play_game':
        from wander_manager.game_invitation import invitation_fact as game_invitation_fact
        return f'{_TOOL_HISTORY_MARKER} {game_invitation_fact()}'
    if role != 'assistant':
        return ''
    parsed_calls = tool_calls
    if isinstance(parsed_calls, str):
        try:
            parsed_calls = json.loads(parsed_calls)
        except (TypeError, ValueError):
            return ''
    if isinstance(parsed_calls, dict):
        parsed_calls = [parsed_calls]
    if not isinstance(parsed_calls, list) or not parsed_calls:
        return ''
    facts = build_tool_behavior_facts(parsed_calls)
    from wander_manager.essays import shared_work_fact
    works = [text for call in parsed_calls if (text := shared_work_fact(call))]
    if works:
        return '\n'.join(part for part in [facts, *works] if part)
    choices = []
    for call in parsed_calls:
        if isinstance(call, dict) and call.get('tool') == 'book_choices':
            options = (call.get('extra_data') or {}).get('book_choices', {}).get('options', [])
            choices.extend((str(option) for option in options))
    if choices:
        candidates = ' / '.join(choices).replace('\n', ' ')
        return f'{_TOOL_HISTORY_MARKER} {facts} 快捷回复候选：{candidates}。采纳情况见人类伙伴后续消息。'
    if facts:
        return f'{_TOOL_HISTORY_MARKER} {facts}'
    return ''

def append_tool_summary(content: str, role: str, tool_summary: Optional[str], tool_calls: Any=None) -> str:
    """兼容旧调用方：正文永远不再拼接工具历史。"""
    return content

def strip_internal_history_markers(content: Any) -> str:
    """剥离独立历史协议行；引用、链接与代码中的同形文字保留。"""
    clean = []
    fence_char = ''
    fence_size = 0
    for line in str(content or '').splitlines(keepends=True):
        fence = _HISTORY_FENCE_RE.match(line)
        if fence_char:
            clean.append(line)
            if fence and fence[1][0] == fence_char and (len(fence[1]) >= fence_size) and (not fence[2].strip()):
                fence_char = ''
            continue
        if fence:
            fence_char, fence_size = (fence[1][0], len(fence[1]))
            clean.append(line)
            continue
        body = line.lstrip(' ')
        if line.startswith('    ') or body.startswith(('\t', '>')):
            clean.append(line)
            continue
        protocol = False
        for marker in (_TOOL_HISTORY_MARKER, *_LEGACY_TOOL_MARKERS):
            if body.startswith(marker):
                suffix = body[len(marker):].rstrip('\r\n')
                protocol = (not suffix or suffix.startswith((' ', '\t'))) and (not suffix.lstrip(' \t').startswith(('(', '[', ':')))
                if protocol:
                    break
        if not protocol and (not _LEGACY_TOOL_RECORD_RE.match(line)):
            clean.append(line)
    return ''.join(clean).strip('\r\n')
__all__ = ['strip_obsidian_wikilinks', 'build_tool_behavior_facts', 'build_tool_history_fact', 'append_tool_summary', 'strip_internal_history_markers', 'strip_legacy_speaker_prefix']
