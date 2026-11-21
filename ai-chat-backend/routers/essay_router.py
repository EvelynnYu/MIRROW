"""Public read-only essays API."""
import asyncio
from fastapi import APIRouter, HTTPException, Query, Response
from wander_manager.essays import read
from wander_manager.reading_illustrations import for_work

router = APIRouter(prefix="/api/wander", tags=["wander"])

@router.get("/essays")
async def list_essays(response: Response, query: str = "", date: str = "", limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(read, query=query, date=date, limit=limit, offset=offset)

@router.get("/essays/{essay_id}")
async def get_essay(essay_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    item = await asyncio.to_thread(read, event_id=essay_id)
    if item is None:
        raise HTTPException(404, "随笔不存在")
    return item


@router.get("/reading-illustrations")
async def list_reading_illustrations(response: Response, source: str = "", work_id: str = ""):
    response.headers["Cache-Control"] = "no-store"
    if source != "essays" or not work_id:
        raise HTTPException(422, "仅支持已指定的随笔作品")
    return {"items": await asyncio.to_thread(for_work, source, work_id)}
