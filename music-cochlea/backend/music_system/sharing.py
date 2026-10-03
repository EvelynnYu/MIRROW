"""Durable UI-only music attachments; the job id belongs to one tool-call placeholder."""
import asyncio
import json
import uuid
from .service import get_service

_tasks = {}
_push_reload = None


def configure(callback):
    global _push_reload
    _push_reload = callback


def projection(job):
    return {k: job[k] for k in ('generation_id', 'attachment_kind', 'attachment_status', 'music_card', 'attachment_error') if k in job}


async def enqueue(query, song_id=None, session_id='', playlist_id=None, *, mode=None, action=None):
    from behavior_scheduler.base_tool import ToolResult, ToolStatus
    if not query and not song_id and not playlist_id:
        return ToolResult(ToolStatus.ERROR, '', error='推歌需要具体歌名或歌曲 ID')
    if not session_id:
        from shared_state import get_latest_session_id
        session_id = get_latest_session_id() or ''
    if not session_id:
        return ToolResult(ToolStatus.ERROR, '', error='没有可挂载音乐卡片的会话')
    gid = uuid.uuid4().hex
    job = {'generation_id': gid, 'attachment_kind': 'music', 'attachment_status': 'pending',
           'session_id': session_id, 'query': query, 'song_id': song_id, 'playlist_id': playlist_id,
           'mode': mode, 'action': action or ('playlist_share' if playlist_id else 'share')}
    get_service().store.save_material('share:' + gid, job)
    task = asyncio.create_task(_run(job))
    _tasks[gid] = task
    task.add_done_callback(lambda _: _tasks.pop(gid, None))
    card_label = '歌单卡片' if job['action'] == 'playlist_share' else '歌曲卡片'
    return ToolResult(ToolStatus.SUCCESS, f'{card_label}正在后台制作，尚未开始播放',
                      extra_data=projection(job), delivery='ui_only')


async def _run(job):
    from .tools import execute
    from behavior_scheduler.base_tool import ToolStatus
    try:
        action = job.get('action') or ('playlist_share' if job.get('playlist_id') else 'share')
        result = await asyncio.wait_for(execute(
            action, job['query'], song_id=job.get('song_id'), playlist_id=job.get('playlist_id'), mode=job.get('mode')
        ), 25)
        if result.status != ToolStatus.SUCCESS:
            raise RuntimeError('provider did not resolve the song')
        card = result.extra_data['music_card']
        if card.get('kind') != 'playlist':
            from .materials import prepare, project, schedule_analysis
            material = await prepare(card)
            card = project(card, material)
            schedule_analysis(card)
        # Resolution is not delivery.  The occurrence counter is committed only
        # after an existing assistant placeholder has accepted this card.
        job.update(attachment_status='ready', music_card=card)
    except asyncio.CancelledError:
        job.update(attachment_status='failed', attachment_error='服务重启中，卡片制作未完成；可重新发起推荐')
        raise
    except Exception:
        job.update(attachment_status='failed', attachment_error='音乐资料取得失败，未开始播放；可请 K 重新推荐')
    finally:
        get_service().store.save_material('share:' + job['generation_id'], job)
    # The provider may finish before the assistant message has reached storage.
    for _ in range(60):
        try:
            if await _fill(job): return
        except Exception:
            pass  # Durable job receipt remains available; UI polling retries projection.
        await asyncio.sleep(.5)


async def _fill(job):
    """Only update an existing matching placeholder, never create a message."""
    from event_chronicle import get_global_chronicle
    from session_manager import get_global_session_manager
    chronicle = get_global_chronicle()
    rows = chronicle.find_tool_attachment_messages(job['session_id'], job['generation_id'])
    for row in rows:
        try:
            calls = json.loads(row.get('tool_calls') or '[]')
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list): continue
        for call in calls:
            if not isinstance(call, dict): continue
            extra = call.get('extra_data') or {}
            if not isinstance(extra, dict): continue
            if extra.get('attachment_kind') != 'music' or extra.get('generation_id') != job['generation_id']: continue
            changed = extra.get('attachment_status') != job['attachment_status'] or extra.get('music_card') != job.get('music_card')
            extra.update(projection(job)); call['extra_data'] = extra
            ready = job['attachment_status'] == 'ready'
            card_label = '歌单卡片' if job.get('playlist_id') else '歌曲卡片'
            call.update(success=ready, status='success' if ready else 'failed', delivery='ui_only',
                        result=(f'{card_label}已完成；分享未开始播放' if ready else job['attachment_error']))
            if changed and not chronicle.update_message_tool_calls(row['message_id'], json.dumps(calls, ensure_ascii=False)):
                return True  # Deleted while the job was finishing: never recreate it.
            manager = get_global_session_manager()
            if not manager or not await manager.update_message_tool_calls(job['session_id'], row['message_id'], calls):
                return False
            if ready and not job.get('share_recorded') and job['music_card'].get('kind') != 'playlist':
                song = get_service().store.record_song_share(
                    job['music_card'], 'generation:' + job['generation_id'], 'k'
                )
                job.update(music_card=song, share_recorded=True)
                get_service().store.save_material('share:' + job['generation_id'], job)
                extra.update(projection(job)); call['extra_data'] = extra
                if not chronicle.update_message_tool_calls(
                    row['message_id'], json.dumps(calls, ensure_ascii=False)
                ):
                    return True
                if not await manager.update_message_tool_calls(job['session_id'], row['message_id'], calls):
                    return False
                changed = True
            elif ready and job['music_card'].get('kind') == 'playlist':
                job['share_recorded'] = True
            if changed and _push_reload: await _push_reload()
            return True
    return False


async def status(gid):
    from .errors import MusicNotFound
    if len(gid) != 32 or any(c not in '0123456789abcdef' for c in gid): raise MusicNotFound('卡片任务不存在')
    job = get_service().store.material('share:' + gid)
    if not job: raise MusicNotFound('卡片任务不存在')
    if job['attachment_status'] == 'pending' and gid not in _tasks:
        job.update(attachment_status='failed', attachment_error='服务已重启，卡片制作未完成；可重新发起推荐')
        get_service().store.save_material('share:' + gid, job)
    if job['attachment_status'] != 'pending': await _fill(job)
    return projection(job)


async def stop():
    tasks = list(_tasks.values())
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
