"""Runnable local host. Real device adapters are used; no playback is simulated."""
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from music_system import integration, router
from music_system import service as service_module
from music_system.provider import NetEaseProvider
from music_system.store import MusicStore
from music_system.context import build_context
from behavior_scheduler.cloud_music_tool import CloudMusicTool
from behavior_scheduler.execution_context import set_request_platform, reset_request_platform
from shared_state import set_latest_session_id, set_mobile_relay_callback
from local_host import chronicle, write_event, record_note, notes
from relay_port import router as relay_router, relay

TOKEN = os.getenv('MIRROW_ACCESS_TOKEN', '')
DATA = Path(__file__).parent/'data'

@asynccontextmanager
async def lifespan(app):
    service_module._service = service_module.MusicService(
        store=MusicStore(DATA/'music_system.sqlite'), provider=NetEaseProvider(DATA/'music_cookies.json'))
    set_latest_session_id('standalone')
    if TOKEN: set_mobile_relay_callback(relay)
    await integration.start(None, on_library_event=write_event)
    try: yield
    finally:
        await integration.stop()
        set_mobile_relay_callback(None)

app = FastAPI(title='MIRROW Cochlea', lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=os.getenv('MIRROW_ALLOWED_ORIGINS','http://localhost:5173,http://127.0.0.1:5173').split(','),
    allow_methods=['GET','POST','PATCH','DELETE'], allow_headers=['Content-Type','Authorization'])

@app.middleware('http')
async def access(request: Request, call_next):
    if request.method == 'OPTIONS': return await call_next(request)
    client = request.client.host if request.client else ''
    if TOKEN:
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied, TOKEN):
            return JSONResponse({'detail':'Access token required'}, status_code=401)
    else:
        origin = request.headers.get('origin')
        if client not in {'127.0.0.1','::1','localhost','testclient','testserver'} or (
            origin and urlparse(origin).hostname not in {'127.0.0.1','localhost'}):
            return JSONResponse({'detail':'Configure an access token before network access'}, status_code=403)
        if request.url.path.startswith('/api/music-relay'):
            return JSONResponse({'detail':'Mobile relay requires an access token'},status_code=403)
    return await call_next(request)

app.include_router(router)
app.include_router(relay_router)

@app.get('/api/music-host/messages')
async def messages(): return {'messages':chronicle.messages()}

class Message(BaseModel):
    body: str = Field(default='', max_length=100000)
    card: dict | None = None

@app.post('/api/music-host/messages')
async def send(body: Message):
    from long_text_cards import normalize_long_text_card
    card = normalize_long_text_card(body.card) if body.card else None
    calls = []
    if card:
        calls = [{'extra_data':{'attachment_kind':'music','attachment_status':'ready','long_text_card':card}}]
    mid = chronicle.add(body.body, calls)
    if card and card.get('song_id'):
        service_module.get_service().store.record_song_share(
            {'id':card['song_id'],'name':card['title'],'artist':card.get('artist',''),'duration':int(card.get('duration') or 0)},
            'message:'+mid, 'owner')
    return {'message_id':mid}

@app.delete('/api/music-host/messages/{mid}')
async def delete(mid: str):
    from local_host import connect
    with connect() as db: db.execute('DELETE FROM messages WHERE id=?', (mid,))
    return {'deleted':True}

class ToolCall(BaseModel):
    action: str
    query: str = ''
    platform: str = 'pc'
    parameters: dict = Field(default_factory=dict)

@app.post('/api/music-host/tool')
async def tool_call(body: ToolCall):
    context_token = set_request_platform(body.platform)
    try:
        result = await CloudMusicTool().execute(body.action, body.query, **{**body.parameters,'_session_id':'standalone'})
        item = {'tool':'cloud_music','status':result.status.value,'result':result.content,
                'error':result.error,'delivery':result.delivery,'extra_data':result.extra_data}
        mid = chronicle.add('', [item])
        return {'message_id':mid, **item}
    finally: reset_request_platform(context_token)

@app.get('/api/music-host/context')
async def context(): return {'context':build_context()}

@app.get('/api/cognition/music')
async def music_notes(): return {'records':notes(),'dimensions':['旋律','歌词','感想']}

@app.post('/api/cognition/music')
async def add_note(payload: dict):
    return {'record':record_note({**payload,'mode':'manual'},source='Manual text note; not playback evidence')}
