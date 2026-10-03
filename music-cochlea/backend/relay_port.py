"""Music-only authenticated long-poll relay; it cannot invoke other phone tools."""
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/api/music-relay')
queue = asyncio.Queue(maxsize=32)
pending = {}

async def relay(request_id, tool, params):
    if tool != 'mobile_music_control': raise ValueError('Unsupported relay tool')
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        queue.put_nowait({'request_id':request_id, 'params':params})
        return await asyncio.wait_for(future, 10)
    except (asyncio.TimeoutError, asyncio.QueueFull):
        return {'success':False, 'error':'The mobile music relay did not respond'}
    finally:
        pending.pop(request_id, None)

@router.get('/next')
async def next_command():
    while True:
        try: command = await asyncio.wait_for(queue.get(), 5)
        except asyncio.TimeoutError: return {'request_id':None}
        if command['request_id'] in pending: return command

class Result(BaseModel):
    request_id: str
    result: dict

@router.post('/result')
async def result(body: Result):
    future = pending.get(body.request_id)
    if future is None or future.done(): raise HTTPException(409, 'Receipt expired')
    future.set_result(body.result)
    return {'accepted':True}
