import asyncio
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app
import app as module
import local_host
import relay_port
import json
import time

def test_local_host_message_card_and_context(tmp_path):
    with patch.object(module,'DATA',tmp_path),patch.object(local_host,'DB',tmp_path/'host.sqlite'),patch.object(module,'TOKEN',''):
        with TestClient(app) as client:
            response=client.post('/api/music-host/messages',json={'body':'Hello','card':{'id':'card1','kind':'music','title':'Demo','body':'Share','song_id':'1','duration':'1000'}})
            assert response.status_code==200
            mid=response.json()['message_id']
            messages=client.get('/api/music-host/messages').json()['messages']
            assert messages[0]['role']=='user'
            assert 'card1' in messages[0]['tool_calls']
            assert client.get('/api/music-host/context').status_code==200
            assert client.get('/api/music/v2/status').json()['session'] is None
            assert client.get('/api/music-relay/next').status_code==403
            assert client.delete('/api/music-host/messages/'+mid).status_code==200
            assert client.get('/api/music-host/messages').json()['messages']==[]

def test_authentication_and_browser_origin(tmp_path):
    with patch.object(module,'DATA',tmp_path),patch.object(local_host,'DB',tmp_path/'host.sqlite'),patch.object(module,'TOKEN','demo-token'):
        with TestClient(app) as client:
            assert client.get('/api/music-host/messages').status_code==401
            assert client.get('/api/music-host/messages',headers={'Authorization':'Bearer demo-token'}).status_code==200
    with patch.object(module,'DATA',tmp_path),patch.object(local_host,'DB',tmp_path/'host.sqlite'),patch.object(module,'TOKEN',''):
        with TestClient(app) as client:
            assert client.get('/api/music-host/messages',headers={'Origin':'https://untrusted.example'}).status_code==403

def test_music_only_relay_receipt_roundtrip():
    async def scenario():
        relay_port.queue=asyncio.Queue(maxsize=32)
        task=asyncio.create_task(relay_port.relay('demo-id','mobile_music_control',{'action':'now_playing'}))
        await asyncio.sleep(0)
        command=await relay_port.next_command()
        assert command['request_id']=='demo-id'
        await relay_port.result(relay_port.Result(request_id='demo-id',result={'success':True,'data':{'available':False}}))
        assert await task=={'success':True,'data':{'available':False}}
        assert not relay_port.pending
    asyncio.run(scenario())

def test_async_tool_card_updates_existing_message(tmp_path,monkeypatch):
    from music_system import tools, materials
    from behavior_scheduler.base_tool import ToolResult,ToolStatus
    song={'id':'900001','name':'Fictitious test song','artist':'Demo','duration':203000}
    async def resolve(*args,**kwargs):return ToolResult(ToolStatus.SUCCESS,'Resolved',extra_data={'music_card':song})
    async def prepare(*args,**kwargs):return {}
    monkeypatch.setattr(tools,'execute',resolve)
    monkeypatch.setattr(materials,'prepare',prepare)
    monkeypatch.setattr(materials,'project',lambda song,material:dict(song))
    monkeypatch.setattr(materials,'schedule_analysis',lambda song:None)
    with patch.object(module,'DATA',tmp_path),patch.object(local_host,'DB',tmp_path/'host.sqlite'),patch.object(module,'TOKEN',''):
        with TestClient(app) as client:
            response=client.post('/api/music-host/tool',json={'action':'share','query':'Demo song','platform':'pc'})
            assert response.status_code==200
            receipt=response.json()
            assert receipt['delivery']=='ui_only'
            for attempt in range(100):
                rows=client.get('/api/music-host/messages').json()['messages']
                extra=json.loads(rows[0]['tool_calls'])[0]['extra_data']
                if extra.get('music_card',{}).get('share_number')==1:break
                time.sleep(.01)
            assert len(rows)==1 and rows[0]['id']==receipt['message_id']
            assert rows[0]['role']=='assistant'
            assert extra['attachment_status']=='ready'
            assert extra['music_card']['id']==song['id']
            assert extra['music_card']['share_number']==1
            assert client.get('/api/music/v2/status').json()['session'] is None
