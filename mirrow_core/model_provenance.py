"""Pure model-only provenance projections; never changes a source quotation."""
import re

PROVENANCE_GUIDE = (
    '生成模型与运行切换记录是技术来源，不是新的说话人、人格或情绪证据。'
    '不同模型生成的 assistant 发言仍属于同一位 Agent。'
    '结合原话、用户反馈和自述区分一时表达与长期认识；不预设任何型号的性格。'
    '标签不证明情绪或人格变化的原因，不因生成来源排除真实经历；长期认识仍须原话与持续互动支持。'
)


def normalize_model_label(model, display_name=''):
    """Extract a model name/version from a relay alias, without station/pricing."""
    value = str(model or '').strip()
    match = re.search(
        r'(?:deepseek|gemini|qwen|claude|gpt|o[134](?=[-\s]|$)|glm|kimi|llama|mistral|minimax|grok)'
        r'(?:[- ]?(?:sonnet|opus|haiku))?'
        r'(?:[- ]?v?\d+(?:[.\-]\d+)*)?'
        r'(?:[- ]?(?:flash|pro|lite|high|low|mini|nano|chat|reasoner|instruct|thinking|turbo|preview|exp|vision))*', value, re.I)
    if match:
        return match.group(0).strip()[:120]
    # A deployment may use its own neutral model ID; never expose URLs or secrets.
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]{0,119}', value) and not value.lower().startswith(('sk-', 'key-', 'token-')) and not re.search(r'official|relay|provider|station|倍率', value, re.I):
        return value
    return ''


def row_model(row):
    return normalize_model_label(row.get('generation_model', ''))


def annotate_history(row):
    """Return a wire copy. Unknown provenance stays unknown, not inferred."""
    result = dict(row)
    model = row_model(row)
    if row.get('role') == 'assistant' and model:
        result['content'] = f'【生成模型：{model}】\n' + str(row.get('content') or '')
    if row.get('event_type') == 'model_switch':
        result['role'] = 'user'
        result['name'] = 'mirrow_runtime_event'
        if row.get('name') != 'mirrow_runtime_event':
            result['content'] = f"【运行事件｜{row.get('timestamp', '')}；不是用户发言】\n{row.get('content', '')}"
    return result


def current_call_message(model):
    label = normalize_model_label(model) or '未知'
    return {'role': 'user', 'name': 'mirrow_current_model',
            'content': f'【运行信息；不是用户发言】本次调用模型：{label}。'}


def day_model_evidence(rows):
    """A bounded-by-source sidecar; IDs bind labels to unchanged source bodies."""
    return {'message_models': [{'message_id': str(r.get('message_id') or r.get('id') or ''),
                                'model': row_model(r)}
                               for r in rows if r.get('role') == 'assistant' and row_model(r)],
            'switch_events': [{'message_id': str(r.get('message_id') or r.get('id') or ''),
                               'timestamp': r.get('timestamp', ''), 'content': r.get('content', '')}
                              for r in rows if r.get('event_type') == 'model_switch']}
