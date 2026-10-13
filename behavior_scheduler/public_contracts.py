"""Stable registry guidance and source-attributed tool receipts for public hosts."""
from datetime import datetime
import json
from uuid import uuid4
from .tool_guidance import build_stable_tool_guidance


def tool_contract(tools):
    schemas = {}
    for name, entry in sorted(tools.items()):
        tool = entry.get('tool', entry) if isinstance(entry, dict) else entry
        if not getattr(tool, 'visible_to_pro', True):
            continue
        schemas[name] = getattr(tool, 'parameters_schema', None) or getattr(tool, 'input_schema', {})
    return (build_stable_tool_guidance(tools) + '\n\n【本次可用工具调用合同】\n'
        '需要执行时，输出 [TOOL_CALL] {"tool":"工具名","parameters":{}} [/TOOL_CALL]。'
        '工具名和参数以注册表为准；未注册的能力不可执行。无需工具时直接自然回复。'
        '执行结果以实际回执为准，失败和未确认与成功分别表述。\n参数定义：'
        + json.dumps(schemas, ensure_ascii=False, sort_keys=True))


def complete_receipt(result, tool=None, parameters=None):
    result = dict(result)
    result.setdefault('event_id', 'tool_event:' + uuid4().hex)
    result.setdefault('completed_at', datetime.now().astimezone().isoformat())
    result.setdefault('status', 'success' if result.get('success') is True else 'failed')
    declared = tool.get_result_delivery(**(parameters or {})) if tool and hasattr(tool, 'get_result_delivery') else 'model'
    result['delivery'] = 'ui_only' if declared == 'ui_only' or result.get('delivery') == 'ui_only' else 'model'
    return result


def execution_record(record, result):
    return {**record, **{key: result[key] for key in ('success', 'status', 'delivery', 'event_id', 'completed_at') if key in result}}


def receipt_message(record):
    # UI-only actions remain in the UI audit, not model-result material.
    if record.get('delivery') == 'ui_only':
        return None
    return {'role': 'system', 'name': 'mirrow_tool_receipt', '_material_kind': 'tool_receipt',
        'content': json.dumps({key: record.get(key) for key in ('tool','parameters','result','success','status','event_id','completed_at')}, ensure_ascii=False)}
