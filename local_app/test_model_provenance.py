"""Offline model provenance contracts; temporary data and synthetic transports."""
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from local_app.api import create_app
from local_app.config import Config
from local_app.context_sources import history_messages
from local_app.day_store import source_digest
from local_app.provider import Provider
from local_app.store import Store
from mirrow_core.model_provenance import (
    PROVENANCE_GUIDE, annotate_history, day_model_evidence, normalize_model_label,
)


def test_labels_exclude_relay_and_price():
    for value in ('逆[Ag1-次-0.02￥]gemini-3.8-flash-high', '55 gemini-3.8-flash-high official',
                  'gemini-3.8-flash-high 倍率 0.25'):
        assert normalize_model_label(value) == 'gemini-3.8-flash-high'
    assert normalize_model_label('Gemini 3.8 55') == 'Gemini 3.8'
    assert normalize_model_label('DeepSeek v4-pro 官方') == 'DeepSeek v4-pro'
    assert normalize_model_label('https://model.example/v1') == ''
    assert normalize_model_label('sk-secret-value') == ''
    assert normalize_model_label('') == ''


def test_sources_are_immutable_and_old_day_digest_is_compatible(tmp_path):
    store = Store(tmp_path)
    old = store.append('assistant', 'Original quotation.')
    keys = ('message_id', 'role', 'content', 'timestamp', 'active_date', 'event_type', 'tool_calls')
    expected = hashlib.sha256(json.dumps([{k: old.get(k) for k in keys}], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert source_digest([old]) == expected
    row = store.append('assistant', 'New quotation.', generation_model='gemini-2.5-flash')
    assert row['content'] == 'New quotation.'
    assert annotate_history(row)['content'].endswith('New quotation.')
    assert '生成模型' not in annotate_history(old)['content']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with store.connection() as db:
            db.execute('UPDATE conversation_messages SET generation_model=? WHERE message_id=?', ('other', row['message_id']))
    assert source_digest([row]) != source_digest([{**row, 'generation_model': ''}])


@pytest.mark.asyncio
async def test_call_freezes_requested_model_and_internal_is_independent(tmp_path):
    store = Store(tmp_path)
    seen = []
    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'model': 'untrusted-other-model', 'choices': [{'message': {'content': 'reply'}}]})
    provider = Provider(Config(tmp_path, 'deepseek-v4-flash', 'https://model.example/v1', 'placeholder'), store, httpx.MockTransport(reply))
    store.set_setting('selected_model', 'gemini-3.8-flash')
    with provider.freeze_model():
        first = await provider.complete([{'role': 'user', 'content': 'hello'}])
        store.set_setting('selected_model', 'deepseek-v4-pro')
        await provider.complete([{'role': 'user', 'content': 'tool continuation'}])
        internal = await provider.complete([{'role': 'user', 'content': 'internal'}], internal=True)
        assert provider.response_model == 'gemini-3.8-flash'
    await provider.complete([{'role': 'user', 'content': 'next turn'}])
    assert [b['model'] for b in seen] == ['gemini-3.8-flash', 'gemini-3.8-flash', 'deepseek-v4-flash', 'deepseek-v4-pro']
    assert first['generation_model'] == 'gemini-3.8-flash'
    assert internal['generation_model'] == 'deepseek-v4-flash'
    assert seen[0]['messages'][0] == seen[1]['messages'][0] == seen[3]['messages'][0]
    assert PROVENANCE_GUIDE in seen[0]['messages'][0]['content']
    for body in seen:
        assert sum(m['role'] == 'system' for m in body['messages']) == 1
        assert body['messages'][-1]['role'] == 'user'
        assert body['model'] in body['messages'][-1]['content']


def test_manual_switch_is_one_event_and_history_is_not_relabelled(tmp_path):
    calls = []
    def reply(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Synthetic reply.'}}]})
    app = create_app(Config(tmp_path, 'deepseek-v4-pro', 'https://model.example/v1', 'placeholder'), httpx.MockTransport(reply))
    with TestClient(app) as client:
        client.put('/api/local/profile', json={'persona': 'Synthetic identity.'})
        assert client.post('/api/local/chat', json={'request_id': 'request001', 'content': 'Hello'}).status_code == 200
        count = len(calls)
        result = client.put('/api/local/model', json={'model': 'gemini-3.8-flash'}).json()
        assert result['changed'] and not result['connectivity_verified']
        assert len(calls) == count
        assert not client.put('/api/local/model', json={'model': 'gemini-3.8-flash'}).json()['changed']
        assert client.post('/api/local/chat', json={'request_id': 'request002', 'content': 'Hi again'}).status_code == 200
        rows = app.state.services['store'].messages()
        replies = [r for r in rows if r['role'] == 'assistant']
        assert [r['generation_model'] for r in replies] == ['deepseek-v4-pro', 'gemini-3.8-flash']
        events = [r for r in rows if r['event_type'] == 'model_switch']
        assert len(events) == 1 and events[0]['role'] == 'event'
        projected = history_messages(events)[0]
        assert projected['role'] == 'user' and '不是用户发言' in projected['content']
        assert annotate_history(annotate_history(events[0])) == annotate_history(events[0])
        assert '主聊天' in projected['content']
        main_call = next(call for call in calls if call['model'] == 'gemini-3.8-flash')
        assert any('生成模型：deepseek-v4-pro' in m['content'] for m in main_call['messages'])


def services(tmp_path):
    values = create_app(Config(tmp_path, 'deepseek-v4-flash', 'https://model.example/v1', 'placeholder')).state.services
    values['store'].set_setting('profile', {'persona': 'Synthetic', 'human_name': 'Human', 'agent_name': 'Agent'})
    life = values['lifecycle']
    life.clock = lambda: datetime.fromisoformat('2026-10-12T23:00:00+08:00')
    values['store'].append('user', 'A synthetic greeting.')
    values['store'].append('assistant', 'A synthetic reply.', generation_model='gemini-3.8-flash')
    values['store'].switch_model('gemini-3.8-flash', 'deepseek-v4-pro')
    life.days.close('2026-10-12', 'test', life.clock())
    return values


@pytest.mark.asyncio
async def test_diary_and_all_cognition_lanes_receive_provenance(tmp_path):
    s = services(tmp_path)
    rows = s['lifecycle'].days.verify('2026-10-12')
    provider = s['provider']
    provider.structured = AsyncMock(return_value=SimpleNamespace(parsed={'text': 'Synthetic diary', 'source_message_ids': [rows[0]['message_id']]}))
    await s['lifecycle'].daily.diary('2026-10-12', rows)
    wire = provider.structured.call_args.kwargs['messages']
    assert PROVENANCE_GUIDE in wire[0]['content']
    packet = json.loads(wire[1]['content'])
    assert packet['model_provenance'] == day_model_evidence(rows)
    assert packet['messages'][1]['content'] == 'A synthetic reply.'
    from cognition import daily, maintenance_sources
    evidence = maintenance_sources.sources(rows)
    assert evidence[1]['generation_model'] == 'gemini-3.8-flash'
    assert all(e['text'] != rows[-1]['content'] for e in evidence)
    calls = []
    async def model(prompt):
        calls.append(prompt)
        return '{}'
    async def lane(rows, llm, **kwargs):
        await llm('Synthetic task contract.')
        return {'status': 'completed', 'items': []}
    with patch.object(daily.maintenance, 'run', lane), patch.object(daily.autonomous, 'run', lane), patch.object(daily.other_daily, 'run_evidence', lane):
        await daily.run_daily(rows, model, source_date='2026-10-12', session_id=s['store'].session_id)
    assert len(calls) == 3
    assert all(PROVENANCE_GUIDE in p and 'gemini-3.8-flash' in p and 'switch_events' in p for p in calls)


@pytest.mark.asyncio
async def test_memory_encoder_and_day_settlement_get_sidecars_without_quote_changes(tmp_path):
    s = services(tmp_path)
    wires = []
    async def model(messages, **kwargs):
        wires.append(messages)
        return {'content': json.dumps({'episodes': [], 'overflow': False, 'overflow_source_refs': [], 'non_event_source_refs': ['s1', 's2']})}
    s['provider'].complete = model
    async def settle(*args, model_call, **kwargs):
        await model_call(messages=[{'role': 'user', 'content': 'Synthetic day settlement.'}], schema_name='test', schema={}, max_tokens=100)
        return SimpleNamespace(status='completed', safe_dict=lambda: {'status': 'completed'})
    with patch('local_app.memory.settle_completed_day', settle):
        result = await s['memory'].encode('2026-10-12')
    assert result['status'] == 'completed' and len(wires) == 2
    for messages in wires:
        assert any(m['content'] == PROVENANCE_GUIDE for m in messages)
        assert 'gemini-3.8-flash' in messages[-1]['content']
        assert 'switch_events' in messages[-1]['content']
    assert '"source_ref": "s2"' in wires[0][-1]['content']
    source_rows = s['memory'].source.read_active_date('2026-10-12')
    assert source_rows[1].content == 'A synthetic reply.'
