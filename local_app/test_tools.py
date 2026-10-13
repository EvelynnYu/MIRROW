import asyncio
import json
from local_app.api import create_app
from local_app.config import Config
from behavior_scheduler.base_tool import BaseTool, ToolResult, ToolStatus
from behavior_scheduler.command_layer import Command, CommandExecutor
from behavior_scheduler.public_contracts import tool_contract, execution_record, receipt_message
from behavior_scheduler.public_projection import project_messages


class SyntheticTool(BaseTool):
    name = 'synthetic'
    description = '合成查询'
    pro_guide = '读取合成测试资料。'
    async def execute(self, **kwargs):
        return ToolResult(ToolStatus.SUCCESS, 'test result')


def test_registered_guidance_only_and_static():
    tool = SyntheticTool()
    text = tool_contract({'synthetic':tool})
    assert '读取合成测试资料' in text and 'eyes' not in text and 'toys' not in text
    assert text == tool_contract({'synthetic':tool})
    tool.visible_to_pro = False
    assert 'synthetic' not in tool_contract({'synthetic':tool})


def test_receipt_identity_and_ui_only():
    tool = SyntheticTool()
    executor = CommandExecutor({'synthetic':tool})
    first = asyncio.run(executor.execute(Command(tool='synthetic',parameters={})))
    second = asyncio.run(executor.execute(Command(tool='synthetic',parameters={})))
    assert first['success'] is True and first['status'] == 'success'
    assert first['event_id'] != second['event_id'] and first['completed_at']
    record = execution_record({'tool':'synthetic','result':first['content']}, first)
    assert json.loads(receipt_message(record)['content'])['event_id'] == first['event_id']
    tool.ui_only_result = True
    hidden = asyncio.run(executor.execute(Command(tool='synthetic',parameters={})))
    assert receipt_message(execution_record({},hidden)) is None
    unknown = asyncio.run(executor.execute(Command(tool='missing',parameters={})))
    assert unknown['success'] is False and unknown['event_id']


def test_material_projection_is_lossless_and_source_labelled():
    original = [{'role':'system','content':'identity'}, {'role':'user','content':'actual human'},
        {'role':'assistant','content':'actual reply'}, {'role':'system','_material_kind':'tool_receipt','content':'all source text'}]
    wire = project_messages(original, enabled=True)
    assert len(wire) == len(original)
    assert sum(m['role'] == 'system' for m in wire) == 1
    assert wire[1:3] == original[1:3]
    assert wire[-1]['content'].endswith(original[-1]['content'])
    assert '工具执行回执' in wire[-1]['content']
    assert original[-1]['role'] == 'system'


def test_actual_scheduler_tool_roundtrip(tmp_path):
    import httpx
    calls = []
    def model(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) == 1:
            content = '[TOOL_CALL]{"tool":"search_memory","parameters":{"query":"合成"}}[/TOOL_CALL]'
        else:
            content = '这次没有检索到已编码记忆。'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
    app = create_app(Config(tmp_path,'synthetic','https://model.example/v1','test-only-placeholder'),httpx.MockTransport(model))
    s = app.state.services
    s['store'].set_setting('profile',{'human_name':'Human','agent_name':'Agent','persona':'合成身份'})
    result = asyncio.run(s['chat'].send('synthetic0001','回忆一下合成内容'))
    assert result['state'] == 'complete'
    assert len(calls) >= 2
    chat_calls = [c for c in calls if c['messages'][0] == calls[0]['messages'][0]]
    assert len(chat_calls) >= 2
    receipts = [m for c in chat_calls for m in c['messages'] if '工具执行回执' in m['content']]
    assert receipts and 'event_id' in receipts[-1]['content']
    assert len(s['store'].messages()) == 2
    saved = json.loads(s['store'].messages()[-1]['tool_calls'])
    assert saved[0]['event_id'] and saved[0]['success'] is True
