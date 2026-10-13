"""Single foreground transaction owner and an explicitly registered read-only tool."""
import asyncio
import json
from behavior_scheduler.base_tool import BaseTool, ToolResult, ToolStatus
from behavior_scheduler.scheduler import BehaviorScheduler
from .context import chat_context


class MemorySearch(BaseTool):
    name = 'search_memory'
    description = '查找本地已编码的记忆及可核验原话；没有命中时如实返回空列表。'
    parameters_schema = {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}

    def __init__(self, memory):
        self.memory = memory

    async def execute(self, query='', **kwargs):
        result = await self.memory.search(str(query))
        return ToolResult(ToolStatus.SUCCESS, json.dumps(result, ensure_ascii=False))


class Chat:
    def __init__(self, store, provider, memory, wander):
        self.store, self.provider, self.memory, self.wander = store, provider, memory, wander
        self.lock = asyncio.Lock()
        self.scheduler = BehaviorScheduler(call_llm_func=provider.complete,
            use_agent_tools=False, tool_registry={'search_memory': MemorySearch(memory)},
            enable_memorial_detection=False, raise_model_errors=True)
        self.provider.chat_contract = self.scheduler._build_system_prompt()
        # Internal intent extraction must use this host's configured transport,
        # never a process-global INTENT_PARSER credential discovered elsewhere.
        async def intent(messages):
            return await provider.complete(messages, internal=True)
        self.scheduler.command_parser.intent_recognizer._lightweight_caller = intent

    async def send(self, request_id, content):
        async with self.lock:
            prior = self.store.request(request_id)
            if prior:
                original = next(row for row in self.store.messages() if row['message_id'] == request_id)
                if original['content'] != content:
                    raise ValueError('request_identity_conflict')
                return prior
            # Explicitly turn off the current idle period before freezing history.
            await self.wander.stop()
            self.store.begin_request(request_id, content)
            try:
                history = await chat_context(self.store, self.memory, content)
                result = await self.scheduler.process_message(content, conversation_history=history,
                    session_id=self.store.session_id)
                if not result.final_response.strip():
                    raise ValueError('empty_reply')
                self.store.finish_request(request_id, result.final_response)
            except BaseException:
                self.store.fail_request(request_id)
                raise
            return self.store.request(request_id)
