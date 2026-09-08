"""Real SQLite UI roundtrips, without production data, models or devices."""
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wander_manager.public_api import create_wander_ui_router
from wander_manager.runtime_store import WanderRuntimeStore
from wander_manager.wish_store import WishStore
from wander_manager.runtime_models import WanderRun, WanderActivity, RunState, ActivityState, GoalMode


@pytest.fixture
def ui():
    with tempfile.TemporaryDirectory() as directory:
        runtime = WanderRuntimeStore(Path(directory) / 'runtime.db')
        runtime.initialize()
        wishes = WishStore(str(Path(directory) / 'wishes.db'))
        app = FastAPI()
        app.include_router(create_wander_ui_router(runtime, wishes))
        with TestClient(app) as client:
            yield client, runtime, wishes


def test_empty_is_real_and_bad_filters_fail(ui):
    client, _, _ = ui
    assert client.get('/api/wander/logs').json()['logs'] == []
    assert client.get('/api/wander/wishes').json() == {'wishes': []}
    assert client.get('/api/wander/logs?date=bad').status_code == 422
    assert client.get('/api/wander/logs?limit=-1').status_code == 422


def test_persona_returns_only_configured_names(ui, monkeypatch):
    client, _, _ = ui
    monkeypatch.setenv('PERSONA_AI_NAME', 'Test AI')
    monkeypatch.setenv('PERSONA_USER_NAME', 'Test User')
    assert client.get('/api/wander/persona').json() == {'ai_name': 'Test AI', 'user_name': 'Test User'}


def test_real_activity_and_share_state_remain_separate(ui):
    client, runtime, _ = ui
    run = WanderRun(session_id='test', state=RunState.COMPLETED)
    runtime.save_run(run)
    activity = WanderActivity(run_id=run.run_id, activity_type='keyword_expansion',
        state=ActivityState.COMPLETED, goal_mode=GoalMode.SINGLE, goal_value=1, summary='Synthetic finished thought')
    runtime.save_activity(activity)
    logs = client.get('/api/wander/logs?hours=0').json()['logs']
    assert len(logs) == 1
    assert logs[0]['state'] == 'completed'
    assert logs[0]['pushed'] is False
    assert client.get('/api/wander/logs?hours=0&pushed=true').json()['logs'] == []


def test_wish_comment_edit_delete_and_status_roundtrip(ui):
    client, _, wishes = ui
    wish = wishes.add_or_merge('Synthetic feature', 'Synthetic reason')
    wish_id = wish['id']
    route = f'/api/wander/wishes/{wish_id}'
    assert client.post(route + '/comments', json={'content': 'A comment'}).status_code == 200
    board = client.get('/api/wander/wishes').json()['wishes']
    comment = board[0]['all_comments'][0]
    assert comment['author'] == 'user'
    assert 'source_key' not in comment
    assert 'history' not in board[0]
    comment_route = f"/api/wander/wishes/comments/{comment['id']}"
    assert client.put(comment_route, json={'content': 'Edited comment'}).status_code == 200
    assert client.delete(comment_route).status_code == 200
    assert client.get('/api/wander/wishes').json()['wishes'][0]['all_comments'] == []
    assert client.put(route + '/status', json={'status': 'in_progress'}).status_code == 200
    assert client.get('/api/wander/wishes').json()['wishes'][0]['status'] == 'in_progress'


def test_failed_mutations_never_claim_success(ui):
    client, _, wishes = ui
    wish = wishes.add_or_merge('Synthetic feature', 'Synthetic reason')
    comment = wishes.add_comment(wish['id'], 'Model comment', author='k')['comment']
    assert client.delete(f"/api/wander/wishes/comments/{comment['id']}").status_code == 409
    assert client.post(f"/api/wander/wishes/{wish['id']}/comments", json={'content': '  '}).status_code == 422
    assert client.put(f"/api/wander/wishes/{wish['id']}/status", json={'status': 'deleted'}).status_code == 422
    assert len(client.get('/api/wander/wishes').json()['wishes'][0]['all_comments']) == 1


def test_example_uses_only_explicit_temp_directory():
    from examples.wander_ui import create_app
    with tempfile.TemporaryDirectory() as directory:
        with TestClient(create_app(directory)) as client:
            assert client.get('/api/wander/wishes').json() == {'wishes': []}
        assert (Path(directory) / 'wander_runtime.db').is_file()
