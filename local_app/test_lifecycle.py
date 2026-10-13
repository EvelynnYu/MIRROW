"""Synthetic local-only daily cycle, recovery and history replacement tests."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from local_app.api import create_app
from local_app.config import Config
from local_app.day_store import DayStore, STAGES
from local_app.preferences import Preferences, in_window


@pytest.fixture
def services(tmp_path):
    app = create_app(Config(tmp_path, 'synthetic', 'https://model.example/v1', 'test-only-placeholder'))
    values = app.state.services
    values['store'].set_setting('profile', {'persona':'合成身份', 'human_name':'Human','agent_name':'Agent'})
    values['lifecycle'].clock = lambda: datetime.fromisoformat('2026-10-12T23:50:00+08:00')
    return values


def clock(life, text):
    life.clock = lambda: datetime.fromisoformat(text)


def seed(s, content='合成聊天'):
    return s['store'].append('user', content)


def test_midnight_window_and_cancel_before_due(services):
    life = services['lifecycle']; seed(services)
    life.configure(Preferences(covenant_word='晚安', wait_minutes=30))
    life.accepted_message('晚安')
    assert life.snapshot()['reservation']['due_at'] == '2026-10-13T00:20:00+08:00'
    clock(life,'2026-10-13T00:05:00+08:00')
    asyncio.run(life.before_message())
    assert life.snapshot()['reservation'] is None  # intentionally resume chatting
    assert life.active_date() == '2026-10-12'
    assert in_window(life.clock(),'22:00','03:00')
    clock(life,'2026-10-13T04:00:00+08:00')
    life.accepted_message('晚安')
    assert life.snapshot()['reservation'] is None


def test_due_reservation_survives_restart_before_next_message(services):
    life = services['lifecycle']; seed(services)
    life.configure(Preferences(covenant_word='晚安', wait_minutes=30))
    life.accepted_message('晚安')
    clock(life,'2026-10-13T00:21:00+08:00')
    asyncio.run(life.before_message())
    assert life.days.get('2026-10-12')['state'] == 'closed'
    assert life.active_date() == '2026-10-13'
    assert life.status()['status'] == 'idle'
    assert len(life.days.verify('2026-10-12')) == 1


def test_manual_early_close_is_deferred_and_fallback_rollover(services):
    life = services['lifecycle']; seed(services)
    assert life.reserve()['deferred_to_midnight']
    clock(life,'2026-10-13T07:00:00+08:00')
    asyncio.run(life.before_message())
    assert life.days.get('2026-10-12')['state'] == 'closed'
    assert life.days.active()['date'] == '2026-10-13'


def test_jobs_partial_failure_retry_and_restart(services):
    life = services['lifecycle']; seed(services)
    asyncio.run(life.close('day_change'))
    life.memory.encode = AsyncMock(return_value={'status':'committed'})
    life.daily.cognition = AsyncMock(side_effect=ValueError('synthetic failure'))
    life.daily.diary = AsyncMock(return_value={'status':'completed','text':'合成日记'})
    asyncio.run(life.daily.run('2026-10-12'))
    assert [life.days.result('2026-10-12',s)['state'] for s in STAGES] == ['completed','error','completed']
    life.daily.cognition = AsyncMock(return_value={'status':'completed'})
    asyncio.run(life.daily.run('2026-10-12', retry=True))
    assert life.memory.encode.await_count == 1
    assert life.daily.diary.await_count == 1
    assert life.daily.cognition.await_count == 1
    life.days.update('2026-10-12','diary','running')
    recovered = DayStore(services['store'])
    assert recovered.result('2026-10-12','diary')['state'] == 'pending'


def test_changed_source_never_runs_model_or_discards_history(services):
    life = services['lifecycle']; row = seed(services)
    asyncio.run(life.close('day_change'))
    with services['store'].connection() as db:
        db.execute('UPDATE conversation_messages SET content=? WHERE id=?', ('edited synthetic source',row['id']))
    life.memory.encode = AsyncMock()
    asyncio.run(life.daily.run('2026-10-12'))
    assert life.days.result('2026-10-12','memory')['state'] == 'review_required'
    life.memory.encode.assert_not_awaited()


def test_history_keeps_raw_until_all_stages_complete(services):
    life, store = services['lifecycle'], services['store']
    for day in ('2026-10-09','2026-10-10','2026-10-11'):
        life.days.open(day, life.clock())
        store.append('user', '原文'+day, active_date=day)
        life.days.close(day,'test',life.clock())
        for stage in STAGES:
            life.days.update(day,stage,'completed', {'text':'日记'+day} if stage == 'diary' else {})
    seed(services)
    rows, diaries = services['context_sources'].select_history(exclude_current=True)
    assert {r['active_date'] for r in rows} == {'2026-10-11'}
    assert diaries[0]['active_date'] == '2026-10-10'
    life.days.update('2026-10-09','memory','error')
    rows, _ = services['context_sources'].select_history()
    assert '2026-10-09' in {r['active_date'] for r in rows}


def test_diary_requires_real_message_anchors(services):
    row = seed(services)
    services['provider'].structured = AsyncMock(return_value=SimpleNamespace(parsed={'text':'合成日记','source_message_ids':['invented']}))
    with pytest.raises(ValueError, match='diary_source'):
        asyncio.run(services['lifecycle'].daily.diary('2026-10-12',[row]))
    services['provider'].structured.return_value.parsed['source_message_ids'] = [row['message_id']]
    assert asyncio.run(services['lifecycle'].daily.diary('2026-10-12',[row]))['status'] == 'completed'


def test_module_events_are_immutable_after_day_end(services):
    life = services['lifecycle']; seed(services)
    life.days.register_event('event1','social','2026-10-12',life.clock().isoformat(),{'text':'合成来源'})
    asyncio.run(life.close('day_change'))
    with pytest.raises(ValueError, match='closed_day'):
        life.days.register_event('event2','social','2026-10-12',life.clock().isoformat(),{})


def test_auto_idle_start_status_and_foreground_gates(services):
    life = services['lifecycle']; seed(services)
    life.configure(Preferences(autonomous_wander=True, idle_minutes=1))
    life.accepted_message('合成消息')
    life.wander.start = AsyncMock()
    clock(life,'2026-10-12T23:52:00+08:00')
    asyncio.run(life.tick())
    life.wander.start.assert_awaited_once()
    life.set_status('sleeping')
    asyncio.run(life.tick())
    life.wander.start.assert_awaited_once()


def test_real_daily_domains_with_synthetic_provider(services):
    import json
    life = services['lifecycle']; row = seed(services, '嗯，晚安。')
    # Only external model responses are synthetic. Real source checks, stores,
    # encoding, three cognition lanes and diary receipt execute unchanged.
    async def model(messages, **kwargs):
        prompt = '\n'.join(m['content'] for m in messages)
        if '长期主观认识整理过程' in prompt:
            value = {'entities':[], 'entries':[]}
        elif '其他实体的长期主观认识分析器' in prompt:
            value = {'queries':[], 'entities':[], 'patches':[], 'observations':[]}
        elif 'domain=world' in prompt:
            value = {'proposals':[]}
        else:
            value = {'episodes':[], 'overflow':False, 'overflow_source_refs':[], 'non_event_source_refs':['s1']}
        return {'content':json.dumps(value)}
    life.provider.complete = model
    life.provider.structured = AsyncMock(return_value=SimpleNamespace(parsed={
        'text':'今天留下了简短的晚安。','source_message_ids':[row['message_id']]}))
    clock(life,'2026-10-13T00:30:00+08:00')
    asyncio.run(life.close('covenant'))
    asyncio.run(life.daily.run('2026-10-12'))
    jobs = [life.days.result('2026-10-12',stage) for stage in STAGES]
    assert all(job['state'] == 'completed' for job in jobs), jobs
    asyncio.run(life.before_message())
    services['store'].append('user','早上好')
    from local_app.context import chat_context
    messages = asyncio.run(chat_context(services['store'],life.memory,'早上好'))
    assert any('嗯，晚安。' in m['content'] for m in messages)
