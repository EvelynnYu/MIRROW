"""Local lifecycle controls, separate from application startup wiring."""
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .preferences import Preferences


class StatusInput(BaseModel):
    status: str
    detail: str = Field(default='', max_length=1000)


def create_router(life, require_model):
    router = APIRouter(prefix='/api/local/lifecycle')

    @router.get('')
    def state():
        return life.snapshot()

    @router.put('/settings')
    async def settings(body: Preferences):
        async with life.chat.lock:
            life.configure(body)
            if not body.autonomous_wander:
                await life.wander.stop()
        return life.snapshot()

    @router.put('/status')
    async def status(body: StatusInput):
        try:
            value = life.set_status(body.status, body.detail)
        except ValueError:
            raise HTTPException(422, '未知状态') from None
        if body.status in {'sleeping', 'napping', 'bathing'}:
            async with life.chat.lock:
                await life.wander.stop()
        return value

    @router.post('/end')
    async def end():
        async with life.chat.lock:
            result = life.reserve()
        return {'reservation': result}

    @router.post('/cancel')
    async def cancel():
        async with life.chat.lock:
            life.store.set_setting('day_reservation', None)
        return {'cancelled': True}

    @router.post('/days/{day}/retry')
    async def retry(day: str):
        require_model()
        if life.digest_task and not life.digest_task.done():
            raise HTTPException(409, '已有日结正在处理')
        try:
            life.days.verify(day)
        except ValueError:
            raise HTTPException(409, '该活跃日尚未结束或来源发生变化') from None
        life.digest_task = asyncio.create_task(life.daily.run(day, retry=True))
        return {'status': 'started'}

    @router.get('/cognition')
    def cognition():
        from cognition import books
        return {domain: books.catalog(domain) for domain in ('self', 'world', 'other')}

    return router
