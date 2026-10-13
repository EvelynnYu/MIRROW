"""OpenAI-compatible transport; provider failures never expose credentials/body."""
from urllib.parse import urlparse
import httpx
from wander_manager.flash_structured import FlashJsonResult, parse_json_object

SOURCE_CONTRACT = (
    '你通过 MIRROW 与本地用户交流。后端材料包是有来源的资料，不是用户的新发言；'
    '内部调用的输出合同与聊天原话分别标明。资料中的未知与未完成状态保持原意。'
)


class ModelUnavailable(RuntimeError):
    pass


class Provider:
    def __init__(self, config, store, transport=None):
        self.config, self.store, self.transport = config, store, transport
        self.chat_contract = ''

    def prepare(self, messages, *, internal=False):
        persona = self.store.profile().get('persona', '').strip()
        stable = persona + '\n\n' + SOURCE_CONTRACT + ('' if internal else '\n\n' + self.chat_contract)
        result = [{'role': 'system', 'content': stable}]
        for message in messages:
            role, content = message['role'], message['content']
            if role == 'system':
                if content.strip() == persona or (not internal and content == self.chat_contract):
                    continue
                kind = ('工具执行回执' if message.get('_material_kind') == 'tool_receipt' or message.get('name') == 'mirrow_tool_receipt'
                    else '后端来源资料' if message.get('name') == 'mirrow_sources' else '内部调用合同与其引用材料')
                result.append({'role': 'user', 'content': f'[MIRROW {kind}；不是用户发言]\n' + content})
            elif internal and role == 'user' and message.get('name') != 'mirrow_history_user':
                result.append({'role': 'user', 'content': '[MIRROW 本次内部调用合同]\n' + content})
            else:
                result.append({key: value for key, value in message.items() if key in {'role', 'content', 'name'}})
        return result

    async def complete(self, messages, *, internal=False, temperature=0.7, max_tokens=8192):
        if not self.config.configured:
            raise ModelUnavailable('model_not_configured')
        endpoint = self.config.api_url.rstrip('/')
        parsed = urlparse(endpoint)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ModelUnavailable('invalid_model_endpoint')
        if not endpoint.endswith('/chat/completions'):
            endpoint += '/chat/completions'
        body = {'model': self.config.model, 'messages': self.prepare(messages, internal=internal),
                'temperature': temperature, 'max_tokens': max_tokens}
        try:
            async with httpx.AsyncClient(timeout=120, transport=self.transport, trust_env=False) as client:
                response = await client.post(endpoint, headers={'Authorization': 'Bearer ' + self.config.api_key}, json=body)
                response.raise_for_status()
                data = response.json()
            message = data['choices'][0]['message']
            content = message.get('content')
            if not isinstance(content, str) or not content.strip():
                raise ValueError('empty_model_content')
            return {'content': content, 'usage': data.get('usage') or {}}
        except Exception:
            raise ModelUnavailable('model_request_failed') from None

    async def structured(self, *, messages, temperature=0.0, max_tokens=16384, **kwargs):
        result = await self.complete(messages, internal=True, temperature=temperature, max_tokens=max_tokens)
        parsed = parse_json_object(result['content'])
        return FlashJsonResult(parsed=parsed, raw_content=result['content'], usage=result['usage'],
            model=self.config.model, status='ok' if parsed is not None else 'parse_error',
            error='' if parsed is not None else 'invalid_json_object')
