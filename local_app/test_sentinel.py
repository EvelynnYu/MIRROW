"""Synthetic receipts and observations only; no private device or provider."""
import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from local_app.api import create_app
from local_app.config import Config
from local_app.preferences import Preferences
from silicon_perception.analysis.anomaly_detector import DataSnapshot
from silicon_perception.analysis.location_episode import LocationEpisode
from silicon_perception.analysis.conversation_evidence import validate_history_evidence
from silicon_perception.delivery import deliver_push


def test_gps_cached_and_continuing_motion_do_not_repeat():
    episode = LocationEpisode()
    now = datetime.now().astimezone() - timedelta(minutes=5)
    def snap(seconds, lat):
        return DataSnapshot(location_lat=lat,location_lng=0,field_ts={'gps':(now+timedelta(seconds=seconds)).isoformat()})
    assert episode.observe(snap(0,0)) == []
    assert episode.observe(snap(60,0.01)) == ['gps_moved']
    assert episode.observe(snap(60,0.01)) == []
    assert episode.observe(snap(50,0.02)) == []
    assert episode.observe(snap(120,0.02)) == []


def test_quote_identity_and_speaker_must_match():
    index = {'H1':{'role':'assistant','content':'合成邀请','message_id':'m1','timestamp':'test'}}
    accepted, errors = validate_history_evidence([{'source_ref':'H1','speaker':'人类伙伴','quote':'合成邀请'}], index)
    assert not accepted and errors == ['history_evidence_speaker_mismatch']
    accepted, errors = validate_history_evidence([{'source_ref':'H1','speaker':'AI','quote':'不存在'}], index)
    assert not accepted and errors == ['history_evidence_quote_mismatch']


def test_delivery_needs_true_receipt():
    store = Mock()
    for result in (False,None):
        assert not asyncio.run(deliver_push(AsyncMock(return_value=result),{'message':'合成'},store=store,event={'message':'test'}))
        assert store.insert_event.call_args.kwargs['pushed'] is False
    assert asyncio.run(deliver_push(AsyncMock(return_value=True),{'message':'合成'},store=store,event={'message':'test'}))


@pytest.fixture
def sentinel(tmp_path):
    app = create_app(Config(tmp_path,'synthetic','https://model.example/v1','test-only-placeholder'))
    s = app.state.services
    s['store'].set_setting('profile',{'persona':'合成身份','human_name':'Human','agent_name':'Agent'})
    s['lifecycle'].configure(Preferences(sentinel_enabled=True))
    s['store'].append('user','合成开场')
    item = s['sentinel']
    item.detector.evaluate = Mock(return_value=['periodic_check'])
    return item


def test_actual_two_stage_decision_and_committed_message(sentinel):
    calls = []
    async def complete(messages, **kwargs):
        calls.append(messages)
        return {'content': json.dumps({'observation':'没有新的设备观测','should_push':True,
            'push_motivation':'想打个招呼','history_evidence':[]}, ensure_ascii=False) if len(calls)==1 else '想来和你打个招呼。'}
    sentinel.provider.complete = complete
    asyncio.run(sentinel.tick())
    assert len(calls) == 2
    assert sentinel.store.messages()[-1]['is_sentinel'] == 1
    assert sentinel.state()['last_delivery']['state'] == 'delivered'


def test_new_chat_during_generation_cancels_delivery(sentinel):
    count = [0]
    async def complete(messages, **kwargs):
        count[0] += 1
        if count[0] == 1:
            return {'content':json.dumps({'observation':'没有新的观测','should_push':True,'push_motivation':'想打招呼','history_evidence':[]})}
        sentinel.store.append('user','新消息打断')
        return {'content':'旧主动消息'}
    sentinel.provider.complete = complete
    asyncio.run(sentinel.tick())
    assert not any(r['is_sentinel'] for r in sentinel.store.messages())
    assert sentinel.state()['last_delivery'] is None


def test_broken_source_is_unknown_and_never_reuses_cached_data(sentinel):
    def broken():
        raise RuntimeError('synthetic failure')
    sentinel.register_source('synthetic_sensor', broken)
    snap = asyncio.run(sentinel.snapshot())
    assert snap.location_lat is None and 'gps' not in snap.field_ts
    assert sentinel.error
