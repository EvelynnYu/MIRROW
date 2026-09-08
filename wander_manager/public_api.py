"""Small local UI adapter, explicitly bound to the host's existing stores.

No main-app import, model calls, device controls or runtime startup.
"""
from datetime import date as Date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .runtime_store import WanderRuntimeStore, redact_sensitive
from .wish_store import WishStore


class StatusBody(BaseModel):
    status: Literal['open', 'in_progress', 'impossible_pending', 'fulfilled']
    note: str = Field(default='', max_length=4000)


class CommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    reply_to_comment_id: int | None = None


def _comment_view(item):
    return {key: ('user' if key == 'author' and item.get(key) == 'user' else item.get(key))
            for key in ('id', 'wish_id', 'author', 'content', 'reply_to_comment_id', 'created_at')}


def _wish_view(item):
    # The modal does not need source keys, raw mutation payloads or internal audit.
    result = {key: item.get(key) for key in (
        'id', 'feature', 'reason', 'status', 'times_wished', 'first_wished_at',
        'last_wished_at', 'updated_at')}
    for key in ('latest_comments', 'all_comments'):
        result[key] = [_comment_view(comment) for comment in item.get(key, [])]
    return result


def create_wander_ui_router(runtime: WanderRuntimeStore, wishes: WishStore) -> APIRouter:
    """Stores must already be initialized by the host. This function is pure wiring."""
    router = APIRouter(prefix='/api/wander', tags=['wander-ui'])

    @router.get('/persona')
    def persona():
        from mirrow_core.persona import get_ai_name, get_user_name
        return {'ai_name': get_ai_name(), 'user_name': get_user_name()}

    @router.get('/logs')
    def logs(limit: int = Query(default=50, ge=0), pushed: bool | None = None,
             hours: int | None = Query(default=None, ge=0), date: str = '', last_viewed_at: str = ''):
        if date:
            try:
                Date.fromisoformat(date)
            except ValueError:
                raise HTTPException(422, '日期须为 YYYY-MM-DD') from None
        filter_hours = 24 if hours is None and not date else (hours or 0)
        return redact_sensitive({
            'logs': runtime.list_activity_logs(limit=limit, pushed=pushed, hours=filter_hours, date=date),
            'stats': runtime.activity_log_stats(pushed=pushed, hours=filter_hours, date=date,
                                                last_viewed_at=last_viewed_at),
            'schedule': runtime.get_scheduler_wake(),
        })

    @router.get('/wishes')
    def board():
        return {'wishes': [_wish_view(wish) for wish in wishes.list_all(include_threads=True)]}

    def mutate(call, result_key):
        try:
            result = call()
        except (ValueError, RuntimeError):
            raise HTTPException(400, '操作未完成，请核对内容及当前愿望状态') from None
        if not result.get(result_key):
            raise HTTPException(409, '记录不存在、归属不符或状态已变化，请刷新后核对')
        return {'success': True, 'mutated': bool(result.get('mutated', True))}

    @router.put('/wishes/{wish_id}/status')
    def status(wish_id: int, body: StatusBody):
        return mutate(lambda: wishes.set_status(wish_id, body.status, actor='user', note=body.note), 'wish')

    @router.post('/wishes/{wish_id}/comments')
    def add_comment(wish_id: int, body: CommentBody):
        if not body.content.strip():
            raise HTTPException(422, '评论不能为空')
        return mutate(lambda: wishes.add_comment(wish_id, body.content.strip(), author='user',
                       reply_to_comment_id=body.reply_to_comment_id), 'comment')

    @router.put('/wishes/comments/{comment_id}')
    def edit_comment(comment_id: int, body: CommentBody):
        if not body.content.strip():
            raise HTTPException(422, '评论不能为空')
        return mutate(lambda: wishes.edit_comment(comment_id, body.content.strip(), actor='user'), 'comment')

    @router.delete('/wishes/comments/{comment_id}')
    def delete_comment(comment_id: int):
        return mutate(lambda: wishes.delete_comment(comment_id, actor='user'), 'deleted')

    return router
