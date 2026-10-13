"""Lossless history-first Sentinel request, with a legacy string view."""
from copy import deepcopy
import json


class SentinelPrompt(str):
    def __new__(cls, text, messages):
        obj = super().__new__(cls, text)
        obj._messages_json = json.dumps(messages, ensure_ascii=False)
        return obj

    @classmethod
    def build(cls, template, **values):
        rules, separator, _ = template.partition('{current_time}')
        if not separator:
            raise ValueError('sentinel_template_missing_dynamic_boundary')
        history_keys = ('conversation_context', 'events_line', 'baseline_context')
        history = {key: values[key] for key in history_keys}
        current = {key: value for key, value in values.items() if key not in history_keys}
        boundary = (
            '\n后续 user 消息是后端提供的带来源材料，不是人类伙伴的新消息。'
            '第一份 JSON 是历史参考：conversation_context 为近期私聊，events_line 为今日事件，'
            'baseline_context 为个人历史基线。最后一份 JSON 是本次实时观测与唤醒变化，'
            '其中 data_line 为身体数据、trends_line 为近期趋势、triggers 为本次唤醒焦点。'
            'current_time 为本次时间，last_msg 为距人类伙伴最后消息的时长，chat_phase 为互动阶段，'
            'status_timeline 为本次状态时间线，affect_context 为 AI 此刻的主观状态。'
            '按各自时间和来源理解，历史判断不能替代当前事实；fact_contract 为本次事实约束。'
        )
        return cls(template.format(**values), [
            {'role': 'system', 'content': rules.format() + boundary},
            {'role': 'user', 'content': json.dumps(history, ensure_ascii=False)},
            {'role': 'user', 'content': json.dumps(current, ensure_ascii=False)},
        ])

    def wire(self):
        return json.loads(self._messages_json)

    def with_repair(self, instruction):
        messages = self.wire()
        messages.append({'role': 'user', 'content': instruction})
        return type(self)(str(self) + '\n\n' + instruction, messages)


def sentinel_messages(prompt):
    if isinstance(prompt, SentinelPrompt):
        return prompt.wire()
    if isinstance(prompt, list):
        return deepcopy(prompt)
    if isinstance(prompt, str):
        return [{'role': 'user', 'content': prompt}]
    raise TypeError('sentinel_prompt_requires_text_or_messages')
