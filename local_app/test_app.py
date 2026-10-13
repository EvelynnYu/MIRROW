"""Synthetic, temporary-data tests. Never uses a real model or deployment."""
import asyncio
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient
from local_app.api import create_app
from local_app.config import Config
from local_app.store import Store
from local_app.provider import Provider


@pytest.fixture
def client(tmp_path):
    requests = []
    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': '合成测试回复。'}}]})
    app = create_app(Config(tmp_path, 'synthetic-model', 'https://model.example/v1', 'test-only-placeholder'),
        httpx.MockTransport(reply))
    with TestClient(app) as client:
        client.test_requests = requests
        yield client


def configured(client):
    assert client.put('/api/local/profile', json={'human_name': '测试用户', 'agent_name': '测试伙伴',
        'persona': '这是合成身份设定，不是真实人格。'}).status_code == 200


def test_cold_start_no_calls(client):
    data = client.get('/api/local/state').json()
    assert data['messages'] == []
    assert data['profile']['persona'] == ''
    assert not data['wander']['running']
    assert 'api_key' not in json.dumps(data)
    assert client.test_requests == []
    assert client.get('/api/local/memory?q=example').json() == {'hits': []}
    assert client.get('/api/wander/wishes').status_code == 200


def test_no_config_no_provider(tmp_path):
    with TestClient(create_app(Config(tmp_path))) as client:
        assert client.post('/api/local/chat', json={'request_id': 'test00001', 'content': 'hello'}).status_code == 409
        assert client.get('/api/local/state').json()['messages'] == []


def test_browser_boundary(client):
    assert client.get('/api/local/state', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/local/state', headers={'Host': 'evil.example'}).status_code == 400
    assert client.get('/api/local/state', headers={'Origin': 'http://testserver'}).status_code == 200


def test_chat_exactly_once_and_stable_system(client):
    configured(client)
    payload = {'request_id': 'test00001', 'content': '第一条合成问候'}
    assert client.post('/api/local/chat', json=payload).json()['state'] == 'complete'
    count = len(client.test_requests)
    assert client.post('/api/local/chat', json=payload).json()['state'] == 'complete'
    assert len(client.test_requests) == count
    assert client.post('/api/local/chat', json={**payload, 'content': 'different'}).status_code == 409
    rows = client.get('/api/local/state').json()['messages']
    assert [r['role'] for r in rows] == ['user', 'assistant']
    wire = client.test_requests[0]['messages']
    assert len([m for m in wire if m['role'] == 'system']) == 1
    assert sum('第一条合成问候' in m['content'] for m in wire) == 1
    assert 'search_memory' in wire[0]['content']
    assert client.post('/api/local/chat', json={'request_id': 'test00002', 'content': '第二条合成问候'}).status_code == 200
    chat_requests = [r for r in client.test_requests if r['messages'][0] == wire[0]]
    assert len(chat_requests) == 2


def test_failure_not_assistant_message(client):
    configured(client)
    provider = client.app.state.services['provider']
    provider.transport = httpx.MockTransport(lambda request: httpx.Response(401, text='never expose this provider body'))
    result = client.post('/api/local/chat', json={'request_id': 'test00001', 'content': 'hello'})
    assert result.status_code == 502
    assert 'provider body' not in result.text
    assert len(client.get('/api/local/state').json()['messages']) == 1
    assert client.app.state.services['store'].request('test00001')['state'] == 'failed'


def test_restart_preserves_identity_and_marks_interruption(tmp_path):
    store = Store(tmp_path)
    session = store.session_id
    store.begin_request('test00001', 'synthetic')
    restarted = Store(tmp_path)
    assert restarted.session_id == session
    assert restarted.request('test00001')['state'] == 'interrupted'
    assert len(restarted.messages()) == 1


def test_current_day_not_encoded(client):
    configured(client)
    response = client.post('/api/local/memory/encode', json={'date': date.today().isoformat()})
    assert response.status_code == 409
    assert client.test_requests == []


def test_source_encoding_and_recall(client):
    service = client.app.state.services
    store, memory = service['store'], service['memory']
    store.append('user', '我喜欢薄荷茶。')
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with store.connection() as db:
        db.execute('UPDATE conversation_messages SET active_date=?,calendar_date=?,timestamp=?',
            (yesterday, yesterday, yesterday + 'T12:00:00+08:00'))
    output = {'episodes': [{'source_refs': ['s1'], 'primary_subject_id': 'human', 'participant_ids': ['human'],
        'event_type': 'preference', 'aspects': [], 'summary': 'Human 表示喜欢薄荷茶。', 'occurred_at': '',
        'calendar_date': '', 'temporal_basis': 'contemporaneous_report', 'time_precision': 'reported_at',
        'time_expression': '', 'importance': 0.4, 'emotional_weight': 0.0, 'confidence': 1.0,
        'epistemic_status': 'explicit_report'}], 'overflow': False, 'overflow_source_refs': [], 'non_event_source_refs': []}
    from types import SimpleNamespace
    settlement = SimpleNamespace(status='completed', safe_dict=lambda: {'status': 'completed'})
    with patch.object(service['provider'], 'complete', AsyncMock(return_value={'content': json.dumps(output)})) as model:
        with patch('local_app.memory.settle_completed_day', AsyncMock(return_value=settlement)):
            report = asyncio.run(memory.encode(yesterday))
            assert report['batches'][0]['status'] == 'committed'
            assert asyncio.run(memory.encode(yesterday))['batches'][0]['status'] == 'cached'
            assert model.await_count == 1
    hits = asyncio.run(memory.search('薄荷茶'))
    assert hits and '薄荷茶' in hits[0]['summary']
    assert any('薄荷茶' in source['content'] for source in hits[0]['sources'])


def test_wander_fact_builder_and_ack(client):
    service = client.app.state.services
    wander = service['wander']
    built = asyncio.run(wander.build('WANDER_V2', wander_runtime_text='合成运行材料'))
    assert built.decision_messages[-1]['content'] == '合成运行材料'
    assert built.sections['wander_runtime']['text'] == '合成运行材料'
    service['lifecycle'].active_date()
    payload = {'message': '合成分享', 'event_id': 'activity-example', 'event_type': 'keyword_expansion'}
    wander.stopping = False
    assert wander.push(payload) is True
    assert wander.push(payload) is True
    assert len(service['store'].messages()) == 1
    wander.stopping = True
    assert wander.push({**payload, 'event_id': 'other'}) is False


def test_real_wander_chain_with_synthetic_model(client):
    configured(client)
    from wander_manager.flash_structured import FlashJsonResult
    from wander_manager.event_types import EventType
    service = client.app.state.services
    wander = service['wander']
    # Actual public planner, node handler, reviewer, settlement, share and stores;
    service['lifecycle'].active_date()
    # only the external model transport is replaced with deterministic JSON.
    async def plan(**kwargs):
        return FlashJsonResult(parsed={'horizon_min': 20, 'activities': [{'event_type': 'keyword_expansion',
            'goal': {'mode': 'open_ended', 'value': None}}]}, raw_content='synthetic', model='test', status='ok')
    async def review(**kwargs):
        return FlashJsonResult(parsed={'reflection': '合成节点感想', 'continue_activity': False,
            'abort_reason': '自然结束', 'emotion_effect': {'changed': False}}, raw_content='synthetic', model='test', status='ok')
    async def settle(**kwargs):
        return FlashJsonResult(parsed={'summary': '合成结算', 'share': True, 'continue_next': False,
            'next_inclination_note': '', 'emotion_effect': {'changed': False}}, raw_content='synthetic', model='test', status='ok')
    async def keyword(**kwargs):
        return FlashJsonResult(parsed={'keyword': '连续性', 'expansion': '合成联想材料'}, raw_content='synthetic', model='test', status='ok')
    wander.runner.planner.llm_caller = plan
    wander.runner.reviewer.llm_caller = review
    wander.runner.settlement.llm_caller = settle
    wander.runner.share_adapter.share_llm = AsyncMock(return_value={'content': '合成漫想分享'})
    wander.stopping = False
    with patch.object(service['provider'], 'structured', keyword):
        result = asyncio.run(wander.runner.tick())
    activities = wander.store.list_activities(result.run_id)
    assert activities, [(d.get('status'), d.get('error')) for d in wander.store.list_decisions(run_id=result.run_id)]
    activity = activities[0]
    assert activity['activity_type'] == EventType.KEYWORD_EXPANSION.value
    assert wander.store.list_nodes(activity['activity_id'])[0]['execution_status'] == 'succeeded'
    delivery = wander.store.get_delivery_for_activity(activity['activity_id'])
    assert delivery['status'] == 'sent'
    rows = service['store'].messages()
    assert len(rows) == 1 and rows[0]['is_wander'] == 1
