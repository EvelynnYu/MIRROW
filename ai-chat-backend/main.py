import sys, io
for _startup_stream in (sys.stdout, sys.stderr):
    if hasattr(_startup_stream, 'reconfigure'):
        _startup_stream.reconfigure(encoding='utf-8', errors='replace')
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Body, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
import os
import base64
import tempfile
from typing import List, Optional, Dict, Any
import json
import asyncio
from body_admission import ordinary, PrivateChatAdmission, enter_work, leave_work
import time
from time_utils import now_iso, now_date, to_beijing_time
import contextvars
from token_tracker import log_token_usage
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging.handlers
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)-7s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S', handlers=[logging.StreamHandler(), logging.handlers.RotatingFileHandler(os.path.join(_log_dir, 'mirrow.log'), encoding='utf-8', maxBytes=10 * 1024 * 1024, backupCount=5)])
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
_request_platform: contextvars.ContextVar[str] = contextvars.ContextVar('request_platform', default='pc')
from profile_manager import get_global_profile_manager
from tool_call_utils import strip_voice_base64, build_tool_summary
import music_monitor as _music_monitor_mod
from music_monitor import MusicMonitor
from music_profile import MusicProfile
if sys.platform == 'win32':
    import functools
    from asyncio.proactor_events import _ProactorBasePipeTransport
    _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

    @functools.wraps(_orig_call_connection_lost)
    def _patched_call_connection_lost(self, exc):
        try:
            _orig_call_connection_lost(self, exc)
        except OSError:
            logger.debug('expected OSError in _patched_call_connection_lost')
            pass
    _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
logger = logging.getLogger(__name__)
from shared_state import _latest_session_id, _latest_persona_prompt, _mirrow_page_visible, _mirrow_page_last_seen, _night_mode_active, _music_mode_active, _last_user_message_time, _pending_tasks, _pending_tasks_lock, set_latest_session_id, set_latest_persona_prompt, set_mirrow_page_visible, set_mirrow_page_last_seen, set_night_mode_active, set_music_mode_active, set_last_user_message_time, set_phone_browse_paused, set_phone_browse_mode, get_phone_browse_mode, get_latest_session_id, get_active_session_id, get_latest_persona_prompt, get_mirrow_page_visible, get_mirrow_page_last_seen, get_night_mode_active, get_music_mode_active, get_last_user_message_time, get_mobile_connected, set_last_private_chat_time, get_last_private_chat_time, set_wander_manager, get_wander_manager, get_last_wb_detection_date, set_last_wb_detection_date, get_wb_detection_ampm, set_wb_detection_ampm, get_session_op_lock, get_allowed_ips, set_allowed_ips, get_task_last_success, set_task_last_success, get_task_fail_count, incr_task_fail_count, _task_fail_count, get_task_last_error, set_task_last_error, get_cron_last_success, set_cron_last_success, get_cron_last_error, set_cron_last_error, get_last_reminder_injected_at, set_last_reminder_injected_at, get_recently_active_sessions, add_recently_active_session, discard_recently_active_session, _task_last_success, _task_last_error, _cron_last_success, _cron_last_error, _recently_active_sessions, get_context_scheduler, set_context_scheduler, get_scheduler, set_scheduler, set_on_topic_ended_callback
from mobile_relay import get_mobile_relay_hub
_diary_injected_date: str = ''
_recent_messages: dict = {}
_async_task_refs: set = set()

def _spawn_async_task(coro):
    """创建后台任务并持有引用，防止 asyncio 陷阱（无引用任务被 GC 回收，从未执行）。"""
    task = asyncio.create_task(coro)
    _async_task_refs.add(task)
    task.add_done_callback(_async_task_refs.discard)
    return task

def _bind_mobile_relay(hub=None):
    """Bind the shared execution authority and the legacy payload adapter."""
    hub = hub or get_mobile_relay_hub()
    from shared_state import set_mobile_relay_callback
    set_mobile_relay_callback(hub.send_tool)
    if behavior_scheduler:
        behavior_scheduler.set_mobile_sender(lambda payload: hub.send_tool(str(payload.get('request_id') or ''), str(payload.get('tool') or ''), dict(payload.get('params') or {})))

def _unbind_mobile_relay_if_empty(hub=None):
    """Keep the relay while any fresh connection remains; clear only at the last exit."""
    hub = hub or get_mobile_relay_hub()
    if hub.has_live_connection():
        _bind_mobile_relay(hub)
        return
    from shared_state import set_mobile_relay_callback
    set_mobile_relay_callback(None)
    if behavior_scheduler:
        behavior_scheduler.set_mobile_sender(None)

async def _flush_mobile_delivery(relay_sender):
    """在原生 Relay 重连后补发手机通知；会话正文不在此处复制。"""
    from mobile_relay.delivery import claim_pending, mark_delivered, mark_failed
    try:
        pending = claim_pending(limit=20)
        for item in pending:
            try:
                result = await asyncio.wait_for(relay_sender(f'delivery_{item.delivery_id}', 'mobile_notify', {'title': item.title, 'body': item.body, 'delivery_id': item.delivery_id}), timeout=15.0)
                ok = isinstance(result, dict) and bool(result.get('success'))
                if ok:
                    mark_delivered(item.delivery_id)
                else:
                    mark_failed(item.delivery_id, 'relay_rejected')
            except Exception as exc:
                mark_failed(item.delivery_id, type(exc).__name__)
    except Exception as exc:
        logger.debug('mobile delivery flush skipped: %s', type(exc).__name__)
_first_topic_session_id: Optional[str] = None
_pending_music_analysis: Dict[str, tuple] = {}
_due_check_task: Optional[asyncio.Task] = None

def _strip_wander_prefix(text: str) -> str:
    """移除已定义的展示前缀，保留正文标签、链接和引用。"""
    from context_builder.display_markers import strip_display_markers
    return strip_display_markers(text)

def _is_mirrow_frontend_active() -> bool:
    """通过前端心跳判断 MIRROW 页面是否可见且在前台（5 秒内有心跳且可见）。"""
    import time
    return _mirrow_page_visible and time.monotonic() - _mirrow_page_last_seen < 5.0

def _mark_diary_injected_today() -> bool:
    """标记日记已注入，返回 True 表示今天尚未注入过（首次）"""
    global _diary_injected_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _diary_injected_date == today:
        return False
    _diary_injected_date = today
    return True

async def _has_active_topics() -> bool:
    """检查是否有正在活跃的话题"""
    global _recently_active_sessions
    if not _recently_active_sessions:
        return False
    from summary import topic_state_machine
    for session_id in list(_recently_active_sessions):
        try:
            status = await topic_state_machine.get_topic_status(session_id)
            if status.get('is_active', False):
                return True
        except Exception:
            _recently_active_sessions.discard(session_id)
    return False
_running_task_objs: dict = {}
_tasks_shutting_down = False
_memory_v2_online_task: Optional[asyncio.Task] = None
_memory_v2_pending_request: Optional[dict] = None
_last_topic_end_ts: str = ''
STALL_SECONDS = 300
WATCHDOG_INTERVAL = 20

def _track_task(task_id: str):
    """注册一个 pending 任务（供前端进度条查询）"""
    from datetime import datetime as dt
    now_iso = dt.now().isoformat()
    _pending_tasks[task_id] = {'status': 'running', 'started_at': now_iso, 'last_progress': now_iso}

def _stamp_progress(task_id: str):
    """心跳：标记任务仍在推进（供卡住检测）。从 grow/digest 的 progress_cb 同步调用，
    单键写入在事件循环中原子，无需持锁（持锁需改 async，会污染 grow 的 cb 接口）。"""
    from datetime import datetime as dt
    t = _pending_tasks.get(task_id)
    if t is not None:
        t['last_progress'] = dt.now().isoformat()
        t.pop('stalled', None)

def _mark_task_done(task_id: str):
    """标记任务完成，持久化最后成功时间"""
    from datetime import datetime as dt
    now_iso = dt.now().isoformat()
    previous = _pending_tasks.get(task_id, {})
    _pending_tasks[task_id] = {'status': 'completed', 'started_at': previous.get('started_at', ''), 'completed_at': now_iso, 'progress': 100, 'message': previous.get('completion_message', ''), **{k: previous[k] for k in ('label', 'source_date', 'purpose', 'details') if k in previous}}
    _task_last_success[task_id] = now_iso
    _task_fail_count[task_id] = 0

def _prereg_task(task_id: str):
    """预注册任务为 pending（话题结束时同步预登记，固定进度条任务集，
    消除 all_done 因空字典/部分注册提前判真的竞态）。协程真跑起来后
    _run_tracked 会经 _track_task 把 pending→running。"""
    from datetime import datetime as dt
    if _pending_tasks.get(task_id, {}).get('status') == 'running':
        return
    _pending_tasks[task_id] = {'status': 'pending', 'started_at': dt.now().isoformat()}
from api_sources.scope import routing_scope

@routing_scope
async def _run_tracked(task_id: str, coro, timeout: float=None, *, propagate: bool=False, source_date: str='', activation=None):
    if _tasks_shutting_down:
        if asyncio.iscoroutine(coro):
            coro.close()
        raise asyncio.CancelledError('backend_shutdown')
    from day_end_scheduling import task_slot
    async with task_slot(task_id, coro):
        from day_end_issues import record_task
        from api_sources.scope import _WAIT_CALLBACK
        from api_sources.task_wait import update_wait
        wait_token = _WAIT_CALLBACK.set(lambda route, until, phase: update_wait(_pending_tasks.get(task_id), route, until, phase))
        try:
            if source_date:
                try:
                    record_task(source_date, task_id, 'running')
                except Exception:
                    logger.warning('日结运行回执保存失败: %s', task_id, exc_info=True)
            return await _run_tracked_once(task_id, coro, timeout, propagate=propagate, activation=activation)
        finally:
            _WAIT_CALLBACK.reset(wait_token)
            if source_date:
                from day_end_issues import record_task
                current = _pending_tasks.get(task_id, {})
                try:
                    for date in current.get('failed_dates') or [source_date]:
                        record_task(date, task_id, current.get('status'), current.get('message', ''), current.get('failure_stage', ''))
                except Exception:
                    logger.warning('日结问题回执保存失败: %s', task_id, exc_info=True)

async def _run_tracked_once(task_id: str, coro, timeout: float=None, *, propagate: bool=False, activation=None):
    """包装异步任务，自动追踪状态。不再设自动超时——任务跑到完成为止。
    普通任务由 _stall_watchdog（连续 STALL_SECONDS 无进展）标记 stalled，
    Memory V2 / 统一分析不设固定时长告警。
    由用户在前端决定是否终止（终止 → task.cancel() → 走 CancelledError 分支）。
    timeout 参数保留仅为兼容旧调用点，不再使用。"""
    if _tasks_shutting_down:
        if asyncio.iscoroutine(coro):
            coro.close()
        raise asyncio.CancelledError('backend_shutdown')

    def activate():
        if _pending_tasks.get(task_id, {}).get('status') != 'running':
            _track_task(task_id)
        _pending_tasks[task_id].update(activation.metadata)
    if activation is None:
        async with _pending_tasks_lock:
            _track_task(task_id)
    else:
        activation.on_start = activate
        async with _pending_tasks_lock:
            previous = _pending_tasks.get(task_id)
            if previous and previous.get('status') in {'completed', 'error'}:
                previous['progress_visible'] = False
    task = asyncio.ensure_future(coro)
    _running_task_objs[task_id] = asyncio.current_task()
    try:
        await task
        from api_sources.scope import failed_phases, phase_failure_message
        if failed_phases():
            raise RuntimeError(phase_failure_message(failed_phases()))
        if activation is None or activation.started:
            async with _pending_tasks_lock:
                _mark_task_done(task_id)
    except asyncio.CancelledError as exc:
        shutting_down = bool(exc.args and exc.args[0] == 'backend_shutdown')
        async with _pending_tasks_lock:
            t = _pending_tasks.get(task_id) or {}
            t['status'] = 'error'
            t['message'] = '后端关闭，任务已终止；已保存结果保留，可继续恢复' if shutting_down else '已手动终止（疑似卡死），日志已保存'
            if activation is not None:
                t.update(activation.metadata)
                t['progress_visible'] = activation.started
            _pending_tasks[task_id] = t
            _task_fail_count[task_id] = _task_fail_count.get(task_id, 0) + 1
            _task_last_error[task_id] = t['message']
        logger.warning(f'任务被终止: {task_id}')
        if propagate:
            if shutting_down:
                raise
            raise RuntimeError(f'日终依赖任务已终止: {task_id}')
    except Exception as e:
        async with _pending_tasks_lock:
            t = _pending_tasks.get(task_id) or {}
            t['status'] = 'error'
            t['message'] = str(e)[:200]
            if activation is not None:
                t.update(activation.metadata)
                t['progress_visible'] = activation.started
            if getattr(e, 'failure_stage', '') in {'cognition', 'auxiliary'}:
                t['failure_stage'] = e.failure_stage
            if task_id == 'diary_backfill' and getattr(e, 'failed_dates', None):
                t['failed_dates'] = list(e.failed_dates)
            _pending_tasks[task_id] = t
            _task_fail_count[task_id] = _task_fail_count.get(task_id, 0) + 1
            _task_last_error[task_id] = str(e)[:200]
        logger.error(f'任务异常: {task_id}: {e}')
        if propagate:
            raise
    finally:
        _running_task_objs.pop(task_id, None)
        if activation is not None:
            activation.on_start = None

async def _stall_watchdog():
    """常驻监控：普通 running 任务连续 STALL_SECONDS 无进展时标记 stalled（不 cancel，任务继续跑，
    交由前端弹窗让用户决定「继续等待」或「打印日志并终止」）。"""
    from datetime import datetime as dt
    from task_stall_policy import check_stall
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            now = dt.now()
            async with _pending_tasks_lock:
                for tid, t in _pending_tasks.items():
                    elapsed = check_stall(tid, t, now, STALL_SECONDS)
                    if elapsed is not None:
                        logger.warning(f'任务疑似卡住（{int(elapsed)}s 无进展）: {tid}')
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception('stall watchdog error')

def _rotate_debug_logs(days: int=7):
    """删除 logs/debug/ 下 mtime 超过 days 天的文件（仿 backup_manager.rotate_backups）。"""
    import os, time
    d = os.path.join('logs', 'debug')
    if not os.path.isdir(d):
        return
    cutoff = time.time() - days * 86400
    for fn in os.listdir(d):
        fp = os.path.join(d, fn)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except Exception:
            pass

def _dump_topic_logs(since_ts: str) -> str:
    """把 mirrow.log 中时间戳 >= since_ts 的行导出到 logs/debug/topic-end-<ts>.log。
    since_ts 格式 'YYYY-MM-DD HH:MM:SS'（与日志行首一致，字典序=时间序）。
    跨 rotate 时并读 mirrow.log.1。返回写出的文件路径（失败返回 ''）。"""
    import os
    from datetime import datetime as dt
    debug_dir = os.path.join('logs', 'debug')
    os.makedirs(debug_dir, exist_ok=True)
    out_path = os.path.join(debug_dir, f"topic-end-{dt.now().strftime('%Y%m%d_%H%M%S')}.log")
    sources = [os.path.join('logs', 'mirrow.log.1'), os.path.join('logs', 'mirrow.log')]
    collected = []
    keep = False
    for src in sources:
        if not os.path.isfile(src):
            continue
        try:
            with open(src, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    ts = line[:19]
                    if len(ts) == 19 and ts[4] == '-' and (ts[13] == ':'):
                        keep = ts >= since_ts
                    if keep:
                        collected.append(line)
        except Exception:
            continue
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(f'# topic-end debug log, since {since_ts}\n')
            f.writelines(collected)
        return out_path
    except Exception:
        return ''

async def cancel_tracked_task(task_id: str, dump_log: bool=True) -> dict:
    """终止某个后台任务（供前端「打印日志并终止」/❌按钮）。可选先导出日志。"""
    log_path = ''
    if dump_log:
        since = _last_topic_end_ts or datetime.now().strftime('%Y-%m-%d') + ' 00:00:00'
        log_path = _dump_topic_logs(since)
    task = _running_task_objs.get(task_id)
    cancelled = False
    if task and (not task.done()):
        task.cancel()
        cancelled = True
    logger.warning(f'用户终止任务: {task_id} (dump_log={dump_log}, log={log_path})')
    return {'success': True, 'cancelled': cancelled, 'log_path': log_path}

async def _get_active_date(session_id: str) -> Optional[str]:
    """获取当前活跃话题的开始日期（YYYY-MM-DD 北京时间），用于跨午夜消息归属。
    活跃话题 → 返回其 start_time 的日期部分（UTC→北京时间）；无活跃话题 → 返回 None。
    """
    try:
        from summary import topic_state_machine
        from time_utils import to_beijing_time
        status = await topic_state_machine.get_topic_status(session_id)
        if status and status.get('is_active'):
            start_time = status.get('start_time', '')
            if start_time and len(start_time) >= 10:
                beijing = to_beijing_time(start_time)
                return beijing[:10]
    except Exception:
        logger.exception('unexpected error in _get_active_date')
        pass
    return None

def _record_cron_success(name: str):
    """记录 cron 任务成功时间"""
    from datetime import datetime as dt
    _cron_last_success[name] = dt.now().isoformat()

def _record_cron_error(name: str, error: str):
    """记录 cron 任务失败信息"""
    from datetime import datetime as dt
    _cron_last_error[name] = f'{dt.now().isoformat()}: {error[:150]}'

def _record_reminder_injected():
    """记录日程提醒最后注入时间"""
    from datetime import datetime as dt
    set_last_reminder_injected_at(dt.now().isoformat())

async def _backfill_missing_diaries(max_days: int=30) -> int:
    """补全最近 max_days 天内缺失的日记；失败会传给任务追踪层。"""
    from event_chronicle import get_global_chronicle, generate_diary_entry
    from silicon_perception.analysis.daily_summary import build_sentinel_diary_section
    from topic_end_maintenance import backfill_missing_diaries
    chronicle = get_global_chronicle()

    def report_review(dates):
        task = _pending_tasks.get('diary_backfill')
        if task is not None:
            task['completion_message'] = '缺失日记补全完成；已有日记待复核: ' + ', '.join(dates)

    def report_completed(date):
        try:
            from day_end_issues import read, key, record_task
            previous = read()['tasks'].get(key(date, 'diary_backfill'), {})
            if previous.get('status') in {'error', 'running'}:
                record_task(date, 'diary_backfill', 'completed')
        except Exception:
            logger.warning('日记补全成功回执保存失败', exc_info=True)
    return await backfill_missing_diaries(chronicle=chronicle, generate_diary=generate_diary_entry, flash_llm_func=call_llm_flash_for_long_text, build_sentinel_summary=build_sentinel_diary_section, max_days=max_days, logger=logger, on_review_required=report_review, on_date_completed=report_completed)

def _memory_v2_online_task_running() -> bool:
    return _memory_v2_online_task is not None and (not _memory_v2_online_task.done())

def _memory_v2_clock_allows_topic_date(topic_date: str, *, explicitly_closed: bool=False) -> bool:
    """Whether the clock plus an ended topic prove this date can be frozen."""
    try:
        from memory_v2.online_ingest import conservative_completed_before_active_date, online_ingest_enabled
        if not online_ingest_enabled():
            return False
        now = datetime.now(pytz.timezone(TIMEZONE))
        cutoff = conservative_completed_before_active_date(now)
        return bool(topic_date and (explicitly_closed or topic_date < cutoff))
    except Exception:
        logger.warning('Memory V2 online clock boundary unavailable', exc_info=True)
        return False

async def _memory_v2_completed_before_active_date(closed_active_date: str='') -> str:
    """Resolve a conservative cutoff from the clock and every active topic."""
    from memory_v2.online_ingest import conservative_completed_before_active_date
    from summary import topic_state_machine
    session_manager = get_global_session_manager()
    if session_manager is None:
        raise RuntimeError('memory_v2_session_manager_unavailable')
    active_topic_dates: list[str] = []
    for session in await session_manager.get_all_sessions():
        status = await topic_state_machine.get_topic_status(session.id)
        if not status.get('is_active'):
            continue
        start_time = str(status.get('start_time') or '')
        if not start_time:
            raise RuntimeError('memory_v2_active_topic_missing_start_time')
        active_topic_dates.append(to_beijing_time(start_time)[:10])
    now = datetime.now(pytz.timezone(TIMEZONE))
    return conservative_completed_before_active_date(now, active_topic_dates=active_topic_dates, explicitly_closed_active_date=closed_active_date)

async def _run_memory_v2_online_ingest(trigger: str, *, closed_active_date: str='', after_tasks: tuple=(), activation=None) -> dict:
    """Run one body-free, bounded completed-day catch-up pass."""
    from memory_v2.online_ingest import ingest_completed_days_from_environment
    if after_tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in after_tasks), return_exceptions=True)
        _stamp_progress('memory_v2_online_ingest')
    cutoff = await _memory_v2_completed_before_active_date(closed_active_date)
    result = await ingest_completed_days_from_environment(completed_before_active_date=cutoff, trigger=trigger, on_work_started=activation.start if activation is not None else None, on_continuation_started=activation.start_continuation if activation is not None else None)
    safe = result.safe_dict()
    logger.info('Memory V2 online ingest: status=%s trigger=%s cutoff=%s candidates=%s repair_candidates=%s processed=%s repaired=%s ingested=%s requests=%s events=%s refresh=%s', safe['status'], safe['trigger'], safe['completed_before_active_date'], safe['candidate_count'], safe['repair_candidate_count'], safe['processed_count'], safe['repaired_count'], safe['ingested_count'], safe['request_count'], safe['event_count'], safe['refresh_status'])
    return safe

def _schedule_memory_v2_online_ingest(trigger: str, *, closed_active_date: str='', after_tasks: tuple=()) -> bool:
    """Coalesce startup, daily and day-change requests onto the main loop."""
    global _memory_v2_online_task, _memory_v2_pending_request
    try:
        from memory_v2.online_ingest import online_ingest_enabled
        if not online_ingest_enabled():
            return False
    except Exception:
        logger.warning('Memory V2 online ingest scheduling unavailable', exc_info=True)
        return False
    if _memory_v2_online_task_running():
        from day_end_scheduling import merge_ingest_request
        _memory_v2_pending_request = merge_ingest_request(_memory_v2_pending_request, trigger, closed_active_date, after_tasks)
        return True
    from day_end_scheduling import WorkActivation
    activation = WorkActivation('Memory V2 记忆补漏检查' if trigger == 'startup' else 'Memory V2 每日维护检查') if trigger in {'startup', 'daily_0610'} and (not closed_active_date) else None
    task = _spawn_async_task(_run_tracked('memory_v2_online_ingest', _run_memory_v2_online_ingest(str(trigger or 'unspecified')[:80], closed_active_date=closed_active_date, after_tasks=after_tasks, activation=activation), activation=activation))
    _memory_v2_online_task = task

    def _clear(completed: asyncio.Task) -> None:
        global _memory_v2_online_task, _memory_v2_pending_request
        if _memory_v2_online_task is completed:
            _memory_v2_online_task = None
            pending, _memory_v2_pending_request = (_memory_v2_pending_request, None)
            if pending is not None:
                _schedule_memory_v2_online_ingest(**pending)
    task.add_done_callback(_clear)
    return True

async def _compute_time_awareness_hint(session_id: str, current_user_message_timestamp: str) -> Optional[str]:
    """计算时间感知提示（犹豫/已读不回检测），传递给 context_scheduler。"""
    try:
        from context_scheduler_mcp.time_awareness import get_time_awareness_hint, exclude_wander_messages
        from summary import conversation_manager as _cm
        all_msgs = await _cm.get_messages(session_id) if _cm else []
        if not all_msgs:
            return None
        filtered = exclude_wander_messages(all_msgs)
        last_k_msg = None
        last_k_ts = None
        for msg in reversed(filtered):
            if msg.get('role') == 'assistant':
                last_k_msg = msg
                last_k_ts = msg.get('timestamp')
                break
        if not last_k_msg:
            return None
        return get_time_awareness_hint(last_k_message=last_k_msg, last_k_message_timestamp=last_k_ts, current_user_message_timestamp=current_user_message_timestamp, page_visible=_mirrow_page_visible, last_heartbeat_monotonic=_mirrow_page_last_seen, topic_boundary_crossed=False)
    except Exception:
        return None

async def _on_topic_ended(session_id: str, trigger_type: str='auto', start_time: str=''):
    """话题结束：整合记忆 → 检查并触发日记

    Args:
        trigger_type: "manual"（前端点"聊到这里"）或 "auto"
        start_time: 在 end_topic/process_message 清理状态前捕获的 topic_start_time
    """
    global _last_topic_end_ts
    _last_topic_end_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if trigger_type in ('manual', 'covenant'):
        try:
            wander_manager.set_user_status('sleeping', '', source='topic_end')
        except Exception:
            logger.exception('unexpected error in _on_topic_ended')
            pass
    from topic_end_maintenance import resolve_topic_date
    topic_date = resolve_topic_date(start_time)
    today = datetime.now().strftime('%Y-%m-%d')
    logger.info('话题结束后处理: session=%s reason=%s topic_date=%s', session_id[:20], trigger_type, topic_date)
    daily_close_eligible = False
    daily_chronicle = None
    existing_daily_diary = None
    if trigger_type in ('manual', 'covenant'):
        enough_msgs = False
        try:
            from event_chronicle import _read_all_messages_for_date
            enough_msgs = len(_read_all_messages_for_date(topic_date)) >= 10
        except Exception:
            logger.exception('每日收尾读取消息数失败')
        from daily_close import resolve_target
        if resolve_target(trigger_type, topic_date, datetime.now(), 10 if enough_msgs else 0, await _has_active_topics()):
            from event_chronicle import get_global_chronicle
            daily_chronicle = get_global_chronicle()
            existing_daily_diary = daily_chronicle.get_diary_entry_by_date(topic_date)
            daily_close_eligible = True
    memory_v2_close_eligible = _memory_v2_clock_allows_topic_date(topic_date, explicitly_closed=daily_close_eligible)
    async with _pending_tasks_lock:
        task_ids = ['diary_backfill', 'daily_aggregation', 'unified_analysis']
        if daily_close_eligible:
            task_ids.append('social_feed_summary')
            from daily_close import needs_diary_close_work
            if needs_diary_close_work(existing_daily_diary):
                task_ids.append('diary_generation')
        if memory_v2_close_eligible:
            task_ids.append('memory_v2_online_ingest')
        for _tid in task_ids:
            _prereg_task(_tid)
    from habit_radar.runtime import request_run as request_habit_shadow, current_task as habit_shadow_task
    request_habit_shadow('topic_end', closed_active_date=topic_date if daily_close_eligible else '')
    habit_task = habit_shadow_task()

    def _agg_topic_date():
        from silicon_perception.analysis.daily_aggregator import DailyAggregator
        from silicon_perception.recording.health_store import get_store
        DailyAggregator(get_store()).aggregate(topic_date)
    agg_task = asyncio.create_task(_run_tracked('daily_aggregation', asyncio.to_thread(_agg_topic_date), timeout=60, propagate=True, source_date=topic_date))

    async def _backfill_after_agg():
        await agg_task
        if diary_task is not None:
            await asyncio.shield(diary_task)
        return await _backfill_missing_diaries()
    asyncio.create_task(_run_tracked('diary_backfill', _backfill_after_agg(), timeout=300 if daily_close_eligible else 120, source_date=topic_date))
    diary_task = None
    social_task = None
    if daily_close_eligible and daily_chronicle is not None:
        from daily_close import needs_diary_close_work
        if needs_diary_close_work(existing_daily_diary):
            from event_chronicle import generate_diary_entry
            logger.info('睡前结束话题，整理活跃日日记与挂念 (%s)', topic_date)

            async def _diary_after_agg():
                await agg_task
                from silicon_perception.analysis.daily_summary import build_sentinel_diary_section
                from daily_close import ensure_diary
                await ensure_diary(topic_date, daily_chronicle, generate_diary_entry, call_llm_flash_for_long_text, build_sentinel_diary_section, review_open_loops=True)
            diary_task = asyncio.create_task(_run_tracked('diary_generation', _diary_after_agg(), timeout=180, propagate=True, source_date=topic_date))

        async def _social_feed_after_diary():
            from social_feed import get_social_feed_store
            from topic_end_social_feed import decide_daily_social_post, daily_diary_material
            from diary_source_audit import source_snapshot

            async def prepare_diary():
                if diary_task is not None:
                    await diary_task
                else:
                    await agg_task
                entry = daily_chronicle.get_diary_entry_by_date(topic_date)
                if entry is None or not str(entry.content or '').strip():
                    raise RuntimeError(f'朋友圈收尾缺少当日日记: {topic_date}')
                material = daily_diary_material(entry, topic_date)
                before_sources = material['source_snapshot']

                async def verify_before_commit():
                    current_entry = daily_chronicle.get_diary_entry_by_date(topic_date)
                    if await _has_active_topics() or current_entry is None or current_entry.id != entry.id or (current_entry.content != entry.content) or (source_snapshot(topic_date) != before_sources):
                        raise RuntimeError('朋友圈决策期间日记或原料变化，等待重新收尾')
                return dict(diary_content=entry.content, current_body_facts=material['current_body_facts'], diary_body_updated=material['body_updated'], before_commit=verify_before_commit)
            result = await decide_daily_social_post(target_date=topic_date, prepare_diary=prepare_diary, diary_content='', persona_prompt=_latest_persona_prompt or '你是 AI。', flash_llm_func=call_llm_flash_for_long_text, store=get_social_feed_store(), on_notification=lambda moment_id: _emit_ui_notification(session_id, moment_id, None, event_type='social_feed_update', content='💌 AI 发了一条共域动态'))
            logger.info('每日朋友圈收尾完成: date=%s action=%s replay=%s', topic_date, result.get('action'), bool(result.get('idempotent_replay')))
        social_task = asyncio.create_task(_run_tracked('social_feed_summary', _social_feed_after_diary(), timeout=180, source_date=topic_date))
    asyncio.create_task(_run_tracked('unified_analysis', _run_unified_post_topic_analysis(session_id, topic_date, habit_task=habit_task), timeout=420, source_date=topic_date))
    if memory_v2_close_eligible:
        _schedule_memory_v2_online_ingest(f'topic_end:{trigger_type}', closed_active_date=topic_date if daily_close_eligible else '', after_tasks=tuple((task for task in (agg_task, diary_task, social_task) if task is not None)))
    if daily_close_eligible:
        from memory_v2.scene_digest_runtime import notify_closed
        notify_closed(topic_date)

async def _trigger_persona_evolution(session_id: str, target_date: str=''):
    """v2: 日锁触发人格演化检测（每日一次，≥10 条用户消息）"""

    class _PersonaDetectionSkipped(Exception):
        pass
    detection_error = None
    try:
        from persona_evolution import should_trigger_date, mark_triggered_date, detect_persona_insights, get_suggestions, save_suggestions
        from datetime import datetime as _dt
        target_date = target_date or _dt.now().strftime('%Y-%m-%d')
        if not should_trigger_date(target_date):
            raise _PersonaDetectionSkipped()
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()
            user_count = 0
            if chronicle:
                import sqlite3
                with sqlite3.connect(chronicle.db_path, timeout=3) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE active_date=? AND role='user'", (target_date,)).fetchone()
                    user_count = row[0] if row else 0
        except Exception:
            user_count = 0
        if user_count < 10:
            logger.info(f'人格演化: {target_date} 消息不足 ({user_count} < 10)，跳过')
            raise _PersonaDetectionSkipped()
        logger.info(f'人格演化: 日触发检测（{target_date} 共 {user_count} 条用户消息）')
        suggestions = await detect_persona_insights(flash_llm_func=call_llm_flash_for_long_text, current_persona=_latest_persona_prompt or '', session_id=session_id, target_date=target_date, raise_on_error=True)
        mark_triggered_date(target_date)
        if suggestions:
            existing = get_suggestions()
            existing.extend(suggestions)
            save_suggestions(existing)
            logger.info(f'人格演化: 保存 {len(suggestions)} 条建议（共 {len(existing)} 条）')
    except _PersonaDetectionSkipped:
        pass
    except Exception as e:
        logger.warning(f'人格演化触发失败: {e}')
        detection_error = e
    if detection_error is not None:
        raise detection_error

def _build_clean_dialog(unjudged) -> str:
    """构建纯对话文本：去掉 thinking 和内部标记。
    原则：只要推送到前端、人类伙伴看到了的，就保留。
    兼容 Pydantic 对象和 dict。"""
    lines = []
    for msg in unjudged:
        role = getattr(msg, 'role', None) or (msg.get('role') if isinstance(msg, dict) else None) or ''
        content = getattr(msg, 'content', None) or (msg.get('content') if isinstance(msg, dict) else None) or ''
        if role == 'system':
            if content.strip():
                lines.append(f'[系统事实] {content.strip()}')
            continue
        if role not in ('user', 'assistant'):
            continue
        clean = content.replace('[voice:', '').replace(']', '').strip()
        if not clean:
            continue
        event_type = getattr(msg, 'event_type', '') or getattr(msg, 'eventType', '') or (msg.get('event_type') or msg.get('eventType') or '' if isinstance(msg, dict) else '')
        if role == 'user' and event_type == 'ui_action:visit_workshop':
            from workshop.ui_action import invitation_fact as workshop_invitation_fact
            lines.append(f'[系统事实] {workshop_invitation_fact(clean)}')
            continue
        if role == 'user' and event_type == 'ui_action:play_game':
            from wander_manager.game_invitation import invitation_fact as game_invitation_fact
            lines.append(f'[系统事实] {game_invitation_fact(clean)}')
            continue
        role_label = '人类伙伴' if role == 'user' else 'AI'
        lines.append(f'[{role_label}] {clean}')
    return '\n'.join(lines)

async def _run_unified_post_topic_analysis(session_id: str='', target_date: str='', *, habit_task=None):
    """认知维护独立于旧画像服务与其 24h 早退，均完成后报告真实状态。"""
    from cognition.maintenance import run_for_date
    from unified_analysis_progress import StageProgress

    def publish(details):
        state = _pending_tasks.get('unified_analysis')
        if state is not None:
            state['details'] = details
        _stamp_progress('unified_analysis')
    stages = StageProgress(publish)
    today = target_date or datetime.now().strftime('%Y-%m-%d')
    results = []

    async def cognition_after_radar():
        if habit_task is not None:
            await asyncio.shield(habit_task)
        return await run_for_date(today, session_id, call_llm_flash_for_long_text, progress_cb=lambda: _stamp_progress('unified_analysis'), status_cb=stages.report)
    for name, step in (('auxiliary', lambda: _run_post_topic_aux_analysis(session_id, today)), ('cognition', cognition_after_radar)):
        stages.report('auxiliary' if name == 'auxiliary' else 'preparation', 'running')
        try:
            results.append(await step())
            if name == 'auxiliary':
                stages.report(name, 'completed')
        except asyncio.CancelledError:
            stages.finish('interrupted')
            raise
        except BaseException as exc:
            results.append(exc)
            if name == 'auxiliary':
                stages.report(name, 'error')
            else:
                stages.finish('error')
        _stamp_progress('unified_analysis')
    if any((isinstance(result, BaseException) for result in results)):
        from day_end_scheduling import DayEndAnalysisError
        raise DayEndAnalysisError([name for name, result in zip(('auxiliary', 'cognition'), results) if isinstance(result, BaseException)])

async def _run_post_topic_aux_analysis(session_id: str='', target_date: str=''):
    """收藏与纪念日辅助整理；事件与 AI 的明确承诺由 Memory V2 消化。"""
    from post_topic_aux import run_post_topic_aux_analysis
    from event_chronicle import get_global_chronicle
    messages = await conversation_manager.get_unsummarized_messages(session_id) if session_id else []
    if len(messages) < 2:
        return
    await run_post_topic_aux_analysis(messages=messages, dialog=_build_clean_dialog(messages), session_id=session_id, target_date=target_date or datetime.now().strftime('%Y-%m-%d'), call_flash=call_llm_api_flash, chronicle=get_global_chronicle())
try:
    from behavior_scheduler import init_global_scheduler, get_global_scheduler as get_behavior_scheduler
    from behavior_scheduler.intent_parser import create_lightweight_caller
    BEHAVIOR_SCHEDULER_AVAILABLE = True
except ImportError:
    BEHAVIOR_SCHEDULER_AVAILABLE = False
    create_lightweight_caller = None
    logger.warning('Behavior scheduler unavailable')
try:
    from wander_manager import get_wander_manager, init_wander_manager, WanderMode, EventType
    WANDER_MANAGER_AVAILABLE = True
except ImportError:
    WANDER_MANAGER_AVAILABLE = False
    logger.warning('Wander manager unavailable')
try:
    from silicon_perception import init_sentinel, get_sentinel, SENTINEL_AVAILABLE
except ImportError:
    SENTINEL_AVAILABLE = False
    init_sentinel = None
    get_sentinel = None
    logger.warning('Silicon sentinel unavailable')
try:
    from task_scheduler import init_global_task_manager, get_global_task_manager
    TASK_SCHEDULER_AVAILABLE = True
except ImportError:
    TASK_SCHEDULER_AVAILABLE = False
    logger.warning('Task scheduler unavailable')
from summary import conversation_manager, topic_state_machine
from message_roles import is_ui_only_role
from vector_embedder import get_global_embedder, SENTENCE_TRANSFORMERS_AVAILABLE, FAISS_AVAILABLE
from session_manager import get_global_session_manager, initialize_session_manager, ThinkingStep
try:
    from context_scheduler_mcp.context_scheduler import get_global_scheduler
    CONTEXT_SCHEDULER_AVAILABLE = True
except ImportError:
    CONTEXT_SCHEDULER_AVAILABLE = False
    logger.warning('Context scheduler unavailable, falling back to legacy memory retrieval')
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)
app = FastAPI(title='AI Chat Backend', description='Backend proxy for AI chat with memory integration')
app.add_middleware(PrivateChatAdmission)
from local_access_boundary import LocalAccessBoundary, allowed_browser_origins
allowed_origins = allowed_browser_origins()
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'], expose_headers=['*'], max_age=600)
app.add_middleware(GZipMiddleware, minimum_size=500)
from shared_state import resolve_allowed_ips
from network_access import NetworkAccessPolicy
_ALLOWED_IPS = resolve_allowed_ips()
set_allowed_ips(_ALLOWED_IPS)
_NETWORK_ACCESS = NetworkAccessPolicy.from_environment(_ALLOWED_IPS)
app.add_middleware(LocalAccessBoundary, network_policy=_NETWORK_ACCESS, allowed_origins=allowed_origins)

@app.middleware('http')
async def ip_whitelist_middleware(request: Request, call_next):
    from starlette.responses import JSONResponse
    client_ip = request.client.host if request.client else None
    if not _NETWORK_ACCESS.allows(client_ip):
        logger = logging.getLogger(__name__)
        logger.warning('Blocked access from an untrusted client')
        return JSONResponse(status_code=403, content={'detail': 'Access denied'})
    platform = request.headers.get('X-Client-Platform', 'pc')
    _request_platform.set(platform)
    return await call_next(request)
import os as _os
_voice_dir = _os.path.join(_os.path.dirname(__file__), 'voice_manager', 'output')
_os.makedirs(_voice_dir, exist_ok=True)
_user_voice_dir = _os.path.join(_os.path.dirname(__file__), 'voice_manager', 'output', 'user')
_os.makedirs(_user_voice_dir, exist_ok=True)
app.mount('/voice/user', StaticFiles(directory=_user_voice_dir), name='voice_user')
_cochlea_dir = _os.path.join(_os.path.dirname(__file__), 'voice_manager', 'output', 'spectrogram')
_os.makedirs(_cochlea_dir, exist_ok=True)
app.mount('/voice/spectrogram', StaticFiles(directory=_cochlea_dir), name='voice_spectrogram')
app.mount('/voice', StaticFiles(directory=_voice_dir), name='voice')
_static_dir = _os.path.join(_os.path.dirname(__file__), 'static')
_os.makedirs(_static_dir, exist_ok=True)
app.mount('/static', StaticFiles(directory=_static_dir), name='static')
CHAT_IMAGE_DIR = _os.path.join(_static_dir, 'chat_images')
_os.makedirs(CHAT_IMAGE_DIR, exist_ok=True)
from llm_client import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL, DEEPSEEK_THINKING_ENABLED, DEEPSEEK_REASONING_EFFORT, DEEPSEEK_FLASH_MODEL, DEEPSEEK_FLASH_API_KEY, DEEPSEEK_FLASH_API_URL, GLM_API_KEY, GLM_API_URL, GLM_MODEL, SUMMARY_MODEL, http_client, set_http_client, close_http_client, llm_semaphore, LLM_TIMEOUT, LLM_LIMITS, call_llm_for_wander, Message, write_context_log, call_llm_api, call_llm_api_flash, call_llm_flash_for_text, call_llm_flash_for_long_text, call_sentinel_first_layer_flash, call_llm_for_behavior, call_llm_api_visual, call_vision_api
from truncation_config import get_truncation_limit
TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')
INTENT_PARSER_API_KEY = os.getenv('INTENT_PARSER_API_KEY', '') or DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY
memory_semaphore = asyncio.Semaphore(int(os.getenv('MAX_CONCURRENT_MEMORY_REQUESTS', '10')))
profile_manager = None
behavior_scheduler = None
wander_manager = None
wander_connections: set = set()
_pushed_sentinel_ids: dict = {}
_pushed_event_ids_lock = asyncio.Lock()
_phone_browser = None
_cochlea = None
CHROME_USER_DATA = os.getenv('CHROME_USER_DATA', '')
TARGET_QQ = os.getenv('TARGET_QQ', '')
CAMERA_DEVICE_NAME = os.getenv('CAMERA_DEVICE_NAME', '')

@app.on_event('startup')
async def startup_event():
    global behavior_scheduler, wander_manager, _due_check_task, _phone_browser
    set_on_topic_ended_callback(_on_topic_ended)
    try:
        from log_filters import HeartbeatAccessFilter
        logging.getLogger('uvicorn.access').addFilter(HeartbeatAccessFilter())
    except Exception:
        logger.warning('挂载心跳日志过滤器失败', exc_info=True)
    set_http_client(httpx.AsyncClient(timeout=LLM_TIMEOUT, limits=LLM_LIMITS, proxy=None, trust_env=False))
    try:
        from memory_v2.context_shadow import warm_context_shadow_from_environment
        readiness = warm_context_shadow_from_environment()
        logger.info('Memory V2 context startup: %s', readiness.get('status', 'unknown'))
    except Exception as exc:
        logger.warning('Memory V2 context startup skipped: %s', type(exc).__name__)
    try:
        from settings_manager import get_setting
        persona = get_setting('aiPersona')
        if persona and isinstance(persona, dict) and persona.get('systemPrompt'):
            set_latest_persona_prompt(persona['systemPrompt'])
            print(f'已从设置恢复人设提示词 ({len(get_latest_persona_prompt())} 字符)')
    except Exception:
        logger.warning('non-critical error in startup_event', exc_info=True)
        pass
    try:
        from wander_manager.user_status import restore_user_status
        restore_user_status()
        from wander_manager.user_status import get_user_status, get_user_status_custom_text
        restored_status = get_user_status()
        restored_text = get_user_status_custom_text()
        print(f"已恢复用户状态: {(restored_status.value if hasattr(restored_status, 'value') else restored_status)}" + (f' ({restored_text})' if restored_text else ''))
    except Exception as e:
        print(f'恢复用户状态失败: {e}')

    async def _auto_wakeup_on_startup():
        await asyncio.sleep(5)
        try:
            from wander_manager.user_status import check_wakeup, set_user_status_combined, UserStatus
            if check_wakeup():
                set_user_status_combined(UserStatus.IDLE, '')
                try:
                    from health_tracker.tracker import get_health_tracker
                    get_health_tracker().on_status_change('sleeping', 'idle', '')
                except Exception:
                    pass
                print('开机自动唤醒: sleeping → idle')
        except Exception as e:
            print(f'开机自动唤醒失败: {e}')
    asyncio.create_task(_auto_wakeup_on_startup())

    async def _warmup_connections():
        warmup_urls = [url for url in [DEEPSEEK_API_URL, DEEPSEEK_FLASH_API_URL] if url]
        for url in warmup_urls[:1]:
            try:
                await http_client.post(url, json={}, headers={'Authorization': f'Bearer {DEEPSEEK_API_KEY or DEEPSEEK_FLASH_API_KEY}', 'Content-Type': 'application/json'})
            except Exception:
                pass
        print('连接池预热完成')
    asyncio.create_task(_warmup_connections())
    asyncio.create_task(_stall_watchdog())
    try:
        _rotate_debug_logs()
    except Exception:
        logger.warning('debug 日志轮转失败', exc_info=True)
    try:
        from routers.voice_router import _cleanup_old_tts_cache
        _cleanup_old_tts_cache()
    except Exception:
        logger.warning('TTS 缓存清理失败', exc_info=True)
    try:
        await initialize_session_manager()
        print('会话管理器初始化成功')
    except Exception as e:
        print(f'会话管理器初始化失败: {e}')
    try:
        from affect import get_affect_service
        get_affect_service().set_flash_caller(call_llm_flash_for_text)
        print('Affect v2 Flash 评价器已注册')
    except Exception as e:
        print(f'情绪 Flash 评价器注册失败（非关键）: {e}')
    try:
        from affect.quiet_timer import start_quiet_timer
        start_quiet_timer()
        print('情绪 quiet checkpoint 定时器已启动', flush=True)
    except Exception as e:
        print(f'情绪 quiet checkpoint 定时器启动失败（非关键）: {e}', flush=True)
    if BEHAVIOR_SCHEDULER_AVAILABLE:
        try:
            from main_model_profiles import DEFAULT_PROFILE_ID, resolve_startup_profile
            _main_profile = resolve_startup_profile()
            if not _main_profile.available and _main_profile.id == DEFAULT_PROFILE_ID:
                raise RuntimeError('没有可用的主聊天模型配置，请先检查 .env 中的 provider key')
            behavior_scheduler = init_global_scheduler(call_llm_func=call_llm_for_behavior, glm_api_key=GLM_API_KEY, glm_api_url=GLM_API_URL, glm_model=GLM_MODEL, chrome_user_data=CHROME_USER_DATA, target_qq=TARGET_QQ, camera_device_name=CAMERA_DEVICE_NAME, use_agent_tools=True, llm_provider='deepseek', llm_api_key=DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY, llm_base_url=(DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL).rsplit('/v1', 1)[0] if '/v1' in (DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL) else 'https://api.deepseek.com', llm_model=DEEPSEEK_FLASH_MODEL, pro_api_key=_main_profile.api_key, pro_model=_main_profile.model, pro_base_url=_main_profile.base_url, pro_provider=_main_profile.provider, pro_profile_id=_main_profile.id, pro_profile_label=_main_profile.label, pro_supports_vision=_main_profile.supports_vision)
            print(f'行为调度器初始化成功 (主聊天: {_main_profile.label}; 内部 Flash 独立)')
            from shared_state import set_behavior_scheduler_available
            set_behavior_scheduler_available(True)
        except Exception as e:
            print(f'行为调度器初始化失败: {e}')
            behavior_scheduler = None
    else:
        behavior_scheduler = None
        print('行为调度器不可用')
    if behavior_scheduler is not None:
        try:

            async def _attachment_push_reload():
                if wander_connections:
                    _reload = {'type': 'chat_reload', 'reason': 'async_attachment_ready'}
                    for _ws in list(wander_connections):
                        try:
                            await _ws.send_json(_reload)
                        except Exception:
                            pass
            _voice_tool = behavior_scheduler.tools.get('send_voice')
            if _voice_tool and hasattr(_voice_tool, 'set_push_reload'):
                _voice_tool.set_push_reload(_attachment_push_reload)
                print('语音异步回调已注册 (send_voice 非阻塞模式)')
            _music_tool = behavior_scheduler.tools.get('cloud_music')
            if _music_tool and hasattr(_music_tool, 'set_push_reload'):
                _music_tool.set_push_reload(_attachment_push_reload)
            from behavior_scheduler.tools import inject_social_feed_dependencies
            from social_feed import get_social_feed_store
            inject_social_feed_dependencies(behavior_scheduler.tools, call_llm_func=call_llm_for_behavior, store=get_social_feed_store(), on_notification=_emit_ui_notification)
            print('朋友圈工具依赖已注册')
        except Exception as e:
            print(f'异步附件回调注册失败: {e}')
    from memory_v2.retrieval import get_wander_memories as get_memories_for_wander

    if WANDER_MANAGER_AVAILABLE:
        try:
            async def _web_search_for_wander(query: str) -> str:
                """异步调用 web_search 工具，返回结果文本"""
                try:
                    from behavior_scheduler.agent_tools_lib.src.tools.web_search_tool import WebSearchTool
                    tool = WebSearchTool()
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, tool.run, {'query': query, 'max_results': 3})
                    if isinstance(result, dict):
                        return result.get('content', str(result))
                    return str(result)
                except Exception as e:
                    logger.warning(f'web_search 调用失败: {e}')
                    return f'搜索失败: {e}'
            _song_cache = None
            _k_self_book = None
            _music_mcp = None
            try:
                from music_cochlea.cochlea import get_cochlea
                _c = get_cochlea()
                if _c:
                    _song_cache = _c.cache
            except Exception:
                pass
            try:
                from self_book import get_global_self_book
                _k_self_book = get_global_self_book()
            except Exception:
                pass
            try:
                from behavior_scheduler.music_mcp_client import get_mcp_client
                _music_mcp = get_mcp_client()
            except Exception:
                pass
            wander_manager = init_wander_manager(idle_threshold=10 * 60, event_interval=15 * 60, call_llm_func=call_llm_for_wander, song_selection_llm_func=lambda messages, activity_context=None: call_llm_for_wander(messages, route_label='music_song_selection', enable_thinking=False, activity_context=activity_context), share_llm_func=lambda messages: call_llm_api_flash(messages, enable_thinking=True, temperature=0.7, usage_tag='wander_share'), on_push_to_user=_push_wander_message, on_activity_event=_record_wander_activity_event, on_notification=_emit_ui_notification, on_status_change=_on_wander_status_change_sync, web_search_func=_web_search_for_wander, get_memories_func=get_memories_for_wander, song_cache=_song_cache, k_self_book=_k_self_book, music_mcp_client=_music_mcp)
            set_wander_manager(wander_manager)
            if behavior_scheduler is not None:
                from behavior_scheduler.tools import inject_social_feed_dependencies
                inject_social_feed_dependencies(behavior_scheduler.tools, run_visit=wander_manager.browse_social_from_chat)
            try:
                sm = get_global_session_manager()
                if sm:
                    sessions = await sm.get_all_sessions()
                    print(f'[RECOVER] 获取到 {(len(sessions) if sessions else 0)} 个会话')
                    if sessions:
                        latest_session = max(sessions, key=lambda s: s.updatedAt if s.updatedAt else '')
                        session_messages = getattr(latest_session, 'messages', []) or []
                        print(f'[RECOVER] 最新会话ID={latest_session.id}, 消息数={len(session_messages)}')
                        if session_messages:
                            for i, m in enumerate(session_messages[:3]):
                                mtype = type(m).__name__
                                role = getattr(m, 'role', None) or (m.get('role') if isinstance(m, dict) else None)
                                print(f'[RECOVER] 消息[{i}] type={mtype}, role={role}')
                        user_timestamps = []
                        for m in session_messages:
                            ts = None
                            if hasattr(m, 'timestamp'):
                                ts = m.timestamp
                            elif isinstance(m, dict):
                                ts = m.get('timestamp')
                            if ts and (hasattr(m, 'role') and m.role == 'user' or (isinstance(m, dict) and m.get('role') == 'user')):
                                user_timestamps.append(ts.isoformat() if hasattr(ts, 'isoformat') else str(ts))
                        if user_timestamps:
                            last_ts = user_timestamps[-1]
                            last_time = datetime.fromisoformat(last_ts)
                            wander_manager.on_user_reply(timestamp=last_time, capture_away=False)
                            print(f'漫想管理器：从会话恢复最后用户消息时间 {last_time}')
                        else:
                            print('漫想管理器：活跃会话中无用户消息，空闲计时从当前开始')
                    else:
                        print('漫想管理器：无活跃会话，空闲计时从当前开始')
            except Exception as e:
                print(f'漫想管理器：恢复用户消息时间失败（不影响启动）: {e}')
            from wander_manager.user_tracking_service import init_user_tracking_service
            init_user_tracking_service(http_client=http_client)
            print('[USER_TRACKING] 服务已初始化 (DS V4-Pro + GLM-4V 降级)')
            from wander_manager.user_tracking_service import UserTrackingService
            UserTrackingService.set_mirrow_check_callback(_is_mirrow_frontend_active)
            try:
                from settings_manager import get_setting
                _wander_on = get_setting('wander_enabled')
                _wander_on = True if _wander_on is None else bool(_wander_on)
            except Exception:
                _wander_on = True
            if _wander_on:
                await wander_manager.start()
                print('思维漫想管理器初始化并启动成功')
            else:
                print('思维漫想管理器已初始化，但总开关关闭（wander_enabled=false），未启动检测循环')
        except Exception as e:
            print(f'思维漫想管理器初始化失败: {e}')
            wander_manager = None
    else:
        wander_manager = None
        print('思维漫想管理器不可用')
    if _phone_browser is None:
        try:
            from phone_browser import setup_phone_browser
            relay_func = None
            if behavior_scheduler:
                relay_func = behavior_scheduler._relay_to_phone
            from routers.phone_browse.llm import phone_browse_vision_llm
            from routers.phone_browse.push import push_phone_browse_message
            _phone_browser = setup_phone_browser(mobile_relay=relay_func, push_callback=push_phone_browse_message, vision_llm_func=phone_browse_vision_llm)
            await _phone_browser.start()
            print('[PHONE_BROWSE] PhoneBrowser 初始化并启动成功', flush=True)
            from routers.phone_browse.push import setup_phone_browse_push
            from routers.phone_browse.context import setup_phone_browse_context
            from summary import conversation_manager
            setup_phone_browse_push(conversation_manager=conversation_manager, wander_connections=wander_connections, session_id_provider=lambda: _latest_session_id, persist_special_msg_func=_persist_special_msg, now_iso_func=now_iso, mobile_relay_func=relay_func)
            setup_phone_browse_context(persona_provider=get_latest_persona_prompt, session_messages_provider=None, page_last_seen_provider=lambda: _mirrow_page_last_seen)
            from music_cochlea.cochlea import MusicCochlea, setup_cochlea

            @ordinary
            async def _push_music_message(message: str, is_music_cochlea: bool=True, reasoning: str=''):
                """推送音乐耳蜗消息：写入 session + WS 广播"""
                from visible_reply import clean_visible_reply
                message = clean_visible_reply(message)
                if not message:
                    return
                sid = _latest_session_id
                print(f'[MUSIC_PUSH] start, _latest_session_id={_latest_session_id}, msg={message[:50]}', flush=True)
                if not sid:
                    try:
                        import json as _json, os as _os
                        _idx_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'sessions', 'sessions_index.json')
                        if _os.path.exists(_idx_path):
                            with open(_idx_path, 'r', encoding='utf-8') as _f:
                                _idx = _json.load(_f)
                            sid = _idx.get('current_session_id') if isinstance(_idx, dict) else None
                            print(f'[MUSIC_PUSH] fallback sid={sid}', flush=True)
                    except Exception:
                        pass
                if not sid:
                    logger.warning('MusicCochlea push: 无活跃会话，跳过')
                    return
                try:
                    import time as _tm
                    msg_id = f'mco_{int(_tm.time() * 1000)}'
                    song_title = message[:80]
                    tc = []
                    try:
                        from music_cochlea.cochlea import get_cochlea
                        c = get_cochlea()
                        if c:
                            st = c.get_current_state()
                            if st.current_song:
                                song_title = f'{st.current_song.title}——{st.current_song.artist}'
                        tc = [{'tool': '共享歌曲', 'description': f'《{song_title}》', 'result': '已检测到播放'}]
                    except Exception:
                        tc = [{'tool': '共享歌曲', 'description': f'《{song_title}》', 'result': '已检测到播放'}]
                    print(f'[MUSIC_PUSH] persist sid={sid} msg_id={msg_id}', flush=True)
                    try:
                        from summary import conversation_manager as _cm
                        import json as _json_tc
                        _tc_json = _json_tc.dumps(tc, ensure_ascii=False) if tc else None
                        await _cm.add_message(sid, 'assistant', message, message_id=msg_id, reasoning_content=reasoning or None, is_wander=False, is_sentinel=False, is_reminder=False, tool_calls=_tc_json)
                        print(f'[MUSIC_PUSH] SQLite OK (tc={len(tc)})', flush=True)
                    except Exception as e:
                        print(f'[MUSIC_PUSH] SQLite FAIL: {e}', flush=True)
                    await _persist_special_msg(session_id=sid, content=message, msg_id=msg_id, is_music_cochlea=True, thinking=reasoning, tool_calls=tc)
                    smgr = get_global_session_manager()
                    if smgr:
                        await smgr.flush_session(sid)
                    print(f'[MUSIC_PUSH] persist OK, broadcasting to {len(wander_connections)} connections', flush=True)
                    push_payload = {'type': 'music_cochlea_push', 'message': message, 'msg_id': msg_id, 'session_id': sid, 'isMusicCochlea': True, 'timestamp': now_iso(), 'thinking': reasoning, 'toolCalls': tc}
                    for ws in list(wander_connections):
                        try:
                            await ws.send_json(push_payload)
                        except Exception:
                            pass
                    print(f'[MUSIC_PUSH] broadcast done', flush=True)
                except Exception as e:
                    print(f'[MUSIC_PUSH] FAIL: {e}', flush=True)
                    logger.error(f'MusicCochlea push 失败: {e}')
            _cochlea = setup_cochlea(push_callback=_push_music_message, flash_llm_func=call_llm_flash_for_text, message_llm_func=lambda messages: call_llm_api_flash(messages, enable_thinking=True, usage_tag='music_cochlea'))
            print('[MUSIC_COCHLEA] MusicCochlea 已初始化', flush=True)
            try:
                from settings_manager import get_setting
                was_on = get_setting('phone_browse_mode')
                if not isinstance(was_on, bool):
                    raise TypeError('Invalid saved phone mode')
                set_phone_browse_mode(was_on)
                print(f'[PHONE_BROWSE] 从持久化恢复：模式={was_on}', flush=True)
            except Exception as e:
                print('[PHONE_BROWSE] 持久化状态读取未获确认，未恢复开关', flush=True)
        except Exception as e:
            print(f'[PHONE_BROWSE] 初始化失败: {e}', flush=True)
    try:
        setup_music_router(pending_cache=_pending_music_analysis, latest_session_getter=lambda: _latest_session_id, flash_llm_func=call_llm_flash_for_text)
        from music_router import setup_music_management
        setup_music_management(_music_monitor_mod)
        from music_system.integration import start as start_music_system
        await start_music_system(call_llm_flash_for_long_text, _record_music_library_event)
    except Exception as e:
        print(f'[MUSIC] setup_music_router 失败: {e}', flush=True)
    try:
        from behavior_scheduler.music_mcp_client import start_mcp_client
        await start_mcp_client()
    except Exception as e:
        print(f'[MUSIC] MCP 客户端启动失败: {e}', flush=True)
    if SENTINEL_AVAILABLE and init_sentinel:
        try:

            async def _on_sentinel_push(payload: dict):
                return await _push_sentinel_message(payload)

            async def _perception_push_pro_llm(decision_input) -> dict:
                """Flash [PUSH] 时调用 Flash 生成自然消息。走 SENTINEL_PUSH recipe。"""
                from silicon_perception.push_generation import generate_sentinel_push
                from proactive_expression import registered_tools
                return await generate_sentinel_push(decision_input, persona=get_latest_persona_prompt(), mood='', session_id=get_active_session_id(), call_flash=lambda messages: call_llm_api_flash(messages, enable_thinking=True, usage_tag='sentinel_push'), expression_tools=await registered_tools())
            sentinel = init_sentinel(on_push=_on_sentinel_push, tick_interval=60, enable_llm_polish=False, call_llm_func=call_sentinel_first_layer_flash, call_pro_llm=_perception_push_pro_llm, call_behavior_llm_func=lambda messages: call_llm_api_flash(messages, enable_thinking=False, usage_tag='sentinel_activity_summary', max_attempts=1))
            await sentinel.start()
            print('硅基哨兵初始化并启动成功')
        except Exception as e:
            print(f'硅基哨兵初始化失败: {e}')
    if CONTEXT_SCHEDULER_AVAILABLE:
        try:
            set_context_scheduler(await get_global_scheduler())
            print('上下文调度器初始化成功')
        except Exception as e:
            print(f'上下文调度器初始化失败: {e}')
            set_context_scheduler(None)
    else:
        set_context_scheduler(None)
        print('上下文调度器不可用，将使用旧版记忆检索逻辑')
    try:
        profile_manager = get_global_profile_manager()
        print('用户画像管理器初始化成功')
    except Exception as e:
        print(f'用户画像管理器初始化失败: {e}')
        profile_manager = None
    try:
        has_active = False
        try:
            from summary import topic_state_machine
            sm = get_global_session_manager()
            if sm:
                for session in await sm.get_all_sessions():
                    status = await topic_state_machine.get_topic_status(session.id)
                    if status.get('is_active'):
                        await topic_state_machine.maybe_end_for_day_change(session.id)
                        status = await topic_state_machine.get_topic_status(session.id)
                    if status.get('is_active'):
                        has_active = True
                        break
        except Exception as e:
            logger.warning(f'活跃话题检测失败（非关键，继续补全）: {e}')
        if has_active:
            logger.info('有活跃话题，跳过启动日记补全（将在话题结束时补全）')
        else:
            generated = await _backfill_missing_diaries(max_days=30)
            if generated == 0:
                logger.info('日记启动检查：无缺失')
        from event_chronicle import reset_maintenance_status
        reset_maintenance_status()
    except Exception as e:
        logger.error(f'事件纪初始化/日记补生成失败（非关键）: {e}')
    try:
        from world_book import get_global_world_book
        from vector_embedder import get_global_embedder
        wb = get_global_world_book()
        wb.set_vector_embedder(get_global_embedder())
        print('世界书语义检索已就绪')
    except Exception as e:
        logger.warning(f'世界书语义检索初始化失败（非关键）: {e}')
    try:
        from self_book import get_global_self_book
        from vector_embedder import get_global_embedder
        sb = get_global_self_book()
        sb.set_vector_embedder(get_global_embedder())
        from cognition.other_retrieval import set_embedder
        set_embedder(get_global_embedder())
        print('自我书语义检索已就绪')
    except Exception as e:
        logger.warning(f'自我书语义检索初始化失败（非关键）: {e}')
    if TASK_SCHEDULER_AVAILABLE:
        try:
            from task_scheduler import get_global_task_manager
            manager = init_global_task_manager()

            async def _task_llm_generator(task):
                """为定时任务生成 AI 主动消息，走 REMINDER_PUSH recipe。"""
                creator = '你自己(AI)' if task.created_by == 'ai' else '人类伙伴'
                task_info = f"任务标题：{task.title}\n创建人：{creator}\n提醒原因：{task.reason or '未指定'}\n上下文：{task.ai_context or task.description or ''}"
                built_result = None
                try:
                    from context_builder import ContextBuilder
                    result = await ContextBuilder.build('REMINDER_PUSH', persona=_latest_persona_prompt or '', mood='', task_info=task_info, context_query=task_info, session_id=get_active_session_id())
                    system_ctx = result.system_content
                    from context_builder.assembly import assemble_context_result
                    messages = assemble_context_result(result)
                    built_result = result
                except Exception:
                    system_ctx = f"{_latest_persona_prompt or '你是AI。'}\n当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n{task_info}"
                    messages = [{'role': 'system', 'content': system_ctx}]
                instruction = '根据最后的提醒事件，以AI的第一人称生成一条简短、自然、有温度的提醒消息（30-80字）。像日常对话一样自然，直接输出消息内容。'
                from proactive_band_touch import output_instruction, split_output
                instruction += output_instruction()
                messages.append({'role': 'system', 'content': instruction})
                response = await call_llm_api_flash(messages, temperature=0.7, enable_thinking=True, usage_tag='reminder_push')
                if built_result is not None:
                    from context_builder import commit_context_result
                    commit_context_result(built_result)
                raw_message = str(response.get('content') or '') if isinstance(response, dict) else str(response or '')
                ai_message, band_request, band_parse_error = split_output(raw_message)
                reasoning = str(response.get('reasoning') or '') if isinstance(response, dict) else ''
                from context_builder.proactive_output import clean_proactive_output
                return {'message': clean_proactive_output(ai_message) or task.message_content or f'⏰ {task.title}', 'thinking': reasoning.strip(), 'model_source': response.get('model_source') if isinstance(response, dict) and ai_message.strip() else None, 'band_touch_request': band_request, 'band_touch_parse_error': band_parse_error}
            manager.set_llm_message_generator(_task_llm_generator)
            from task_scheduler.reminder_injector import ReminderInjector
            reminder_injector = ReminderInjector(session_id_provider=get_active_session_id, set_current_session=manager.set_current_session_id, persist_special_msg=_persist_special_msg, record_injected=_record_reminder_injected, wander_connections=wander_connections, task_broadcast=manager._broadcast, emit_notification=_emit_ui_notification, spawn_async_task=_spawn_async_task)
            manager.set_chat_injector(reminder_injector.inject)
            try:
                from health_tracker.sync_manager import set_chat_injector as set_sync_injector
                set_sync_injector(reminder_injector.inject)
            except Exception:
                logger.exception('unexpected error registering reminder injector')
                pass
            try:
                from health_tracker.tracker import get_health_tracker
                ht = get_health_tracker()

                async def _step_sensor_callback() -> Optional[int]:
                    try:
                        from shared_state import get_mobile_relay_callback
                        _relay_now = get_mobile_relay_callback()
                    except ImportError:
                        return None
                    if not _relay_now:
                        return None
                    import uuid as _ht_uuid
                    try:
                        result = await asyncio.wait_for(_relay_now(str(_ht_uuid.uuid4()), 'mobile_steps_sensor', {}), timeout=10.0)
                        if isinstance(result, dict) and result.get('success') and result.get('data'):
                            steps = result['data'].get('steps', 0)
                            logger.info(f'[HealthTracker] 步数传感器读取: {steps} 步')
                            return steps
                    except asyncio.TimeoutError:
                        logger.debug('[HealthTracker] 步数传感器读取超时(10s)')
                    except Exception as e:
                        err_msg = str(e)
                        if 'websocket.send' in err_msg or 'websocket.close' in err_msg:
                            logger.debug(f'[HealthTracker] 步数传感器跳过（WS未就绪）: {err_msg[:60]}')
                        else:
                            logger.warning(f'[HealthTracker] 步数传感器读取失败: {e}')
                    return None
                ht.set_step_callback(_step_sensor_callback)
                logger.info('[HealthTracker] step_callback 已注入（外出步数差值可用）')

                async def _sleep_prompt_callback(sleep_type: str):
                    if wander_connections:
                        _msg = json.dumps({'type': 'sleep_prompt', 'sleep_type': sleep_type})
                        for _ws in list(wander_connections):
                            try:
                                await _ws.send_text(_msg)
                            except Exception:
                                logger.exception('unexpected error in _sleep_prompt_callback')
                                pass
                ht.set_sleep_prompt_callback(_sleep_prompt_callback)
                logger.info('[HealthTracker] sleep_prompt_callback 已注入')
            except Exception as e:
                logger.warning(f'[HealthTracker] 回调注入失败: {e}')

            def _persona_provider() -> str:
                return f'\n你的人设：{_latest_persona_prompt}' if _latest_persona_prompt else ''
            manager.set_persona_provider(_persona_provider)

            def _reply_checker() -> float:
                try:
                    from wander_manager.mode_switch import get_mode_switch
                    ms = get_mode_switch()
                    return ms.get_idle_seconds() if ms else 0.0
                except Exception:
                    return 0.0
            manager.set_reply_checker(_reply_checker)
            from task_scheduler.recheck_generator import create_recheck_llm_generator
            manager.set_recheck_llm_generator(create_recheck_llm_generator(persona_provider=get_latest_persona_prompt, session_id_provider=get_active_session_id))
            if behavior_scheduler:
                _scheduler_tools = behavior_scheduler.tools

                async def _execute_self_task_callback(task):
                    from task_scheduler.self_task_executor import execute_with_tools, _is_checkup_task
                    from task_scheduler.self_task_generation import generate_self_task_reply
                    task_tools = {name: tool for name, tool in _scheduler_tools.items() if name in {'eyes', 'check_phone', 'take_screenshot', 'band'}} if _is_checkup_task(task) else _scheduler_tools
                    return await execute_with_tools(task, tools=task_tools, call_pro_func=generate_self_task_reply, compile_action_func=behavior_scheduler._compile_tool_actions_with_flash, session_id=manager._current_session_id or '')
                manager.set_self_task_executor(_execute_self_task_callback)
                manager.set_checkup_notifier(_emit_ui_notification)
                print('[SELF_TASK] 执行器已注册（Flash + tools）', flush=True)
            print('定时任务管理器初始化成功（LLM消息生成 + 聊天注入 + 复查机制已启用）')
            if wander_manager:
                from task_scheduler.manager import get_global_task_manager as _get_tm

                async def _on_inconsistency(status_text, activity_description, status_mismatch_detail, session_id):
                    tm = _get_tm()
                    if tm:
                        sid = session_id or _latest_session_id or ''
                        await tm.inject_inconsistency_check(status_text, activity_description, status_mismatch_detail, sid)
                from wander_manager.event_handlers import UserTrackingHandler
                UserTrackingHandler.set_inconsistency_callback(_on_inconsistency)
                print('不一致查岗回调已注册（复查管道）')
        except Exception as e:
            print(f'定时任务管理器初始化失败: {e}')
    try:
        from calendar_manager import init_global_calendar_manager, get_global_calendar_manager
        cal_mgr = init_global_calendar_manager()

        async def _calendar_llm_generator(prompt: str) -> str:
            """为日历扫描生成 LLM 消息"""
            from neuron_registry import neuron_trace
            with neuron_trace('calendar_memorial_msg', model='Flash') as trace:
                trace.set_input(prompt[:500])
                ai_message = await call_llm_flash_for_text(prompt, temperature=0.7)
                trace.set_output(ai_message[:300] if ai_message else '')
            return ai_message.strip() if ai_message else ''
        cal_mgr.set_llm_generator(_calendar_llm_generator)
        print('日历管理器初始化成功（LLM扫描消息已启用）')
    except Exception as e:
        print(f'日历管理器初始化失败: {e}')
    set_scheduler(BackgroundScheduler(timezone=pytz.timezone(TIMEZONE)))
    main_event_loop = asyncio.get_running_loop()

    def run_async_daily_maintenance():
        """每日维护任务（同步包装器）"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            asyncio.create_task(_do_daily_maintenance())
        else:
            loop.run_until_complete(_do_daily_maintenance())
    get_scheduler().add_job(run_async_daily_maintenance, CronTrigger(hour=6, minute=0, timezone=pytz.timezone(TIMEZONE)))
    try:
        from memory_v2.online_ingest import online_ingest_enabled
        if online_ingest_enabled():

            def request_memory_v2_daily_ingest():
                main_event_loop.call_soon_threadsafe(_schedule_memory_v2_online_ingest, 'daily_0610')
            get_scheduler().add_job(request_memory_v2_daily_ingest, CronTrigger(hour=6, minute=10, timezone=pytz.timezone(TIMEZONE)), id='memory_v2_online_ingest', replace_existing=True)
    except Exception:
        logger.warning('Memory V2 daily ingest registration skipped', exc_info=True)
    if TASK_SCHEDULER_AVAILABLE:

        async def _due_check_loop():
            """纯 asyncio 定时检查到期任务（运行在主事件循环，不用 BackgroundScheduler 线程）"""
            print('[TASK_SCHEDULER] asyncio 定时检查已启动（每30秒）', flush=True)
            try:
                while True:
                    await asyncio.sleep(30)
                    try:
                        manager = get_global_task_manager()
                        triggered = await manager.check_due_tasks()
                        if triggered:
                            for task in triggered:
                                print(f'[TASK_SCHEDULER] 任务已触发: {task.title}', flush=True)
                        _record_cron_success('task_check')
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f'[TASK_SCHEDULER] 检查失败: {e}', flush=True)
            except asyncio.CancelledError:
                print('[TASK_SCHEDULER] 定时检查已停止', flush=True)
        _due_check_task = asyncio.create_task(_due_check_loop())

    def run_annual_archive():
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        from calendar_manager.manager import get_global_calendar_manager
        cal_mgr = get_global_calendar_manager()
        result = cal_mgr.archive_old_events()
        print(f"[CALENDAR] 年度归档完成: 删除 {result['deleted_period_count']} 个旧生理期事件")
    get_scheduler().add_job(run_annual_archive, CronTrigger(month=1, day=1, hour=0, minute=1, timezone=pytz.timezone(TIMEZONE)))
    from apscheduler.triggers.interval import IntervalTrigger
    try:
        from habit_radar.runtime import install as install_habit_shadow
        install_habit_shadow(get_scheduler(), main_event_loop, _memory_v2_completed_before_active_date)
    except Exception:
        logger.warning('Habit radar shadow startup unavailable')
    from memory_v2.scene_digest_runtime import install as install_scene_digests
    install_scene_digests(main_event_loop, _memory_v2_completed_before_active_date)
    from cognition.brief import install as install_cognition_brief
    install_cognition_brief(main_event_loop, call_llm_flash_for_long_text)
    get_scheduler().start()
    _schedule_memory_v2_online_ingest('startup')
    print('定时任务调度器已启动：每日维护（6:00），Memory V2日结（6:10），中期记忆更新（周一2:00），年度归档（1月1日 00:01）')
    try:
        from ambient_listening import get_ambient_listening_service
        _ambient = get_ambient_listening_service()
        _ambient.configure(event_writer=_write_ambient_observation_event, message_generator=call_llm_for_wander, message_sender=_push_wander_message, note_sender=_send_ambient_note, session_provider=get_active_session_id, recent_user_messages_provider=_recent_ambient_user_messages)
        await _ambient.start()
        print('环境监听主机处理器已启动', flush=True)
    except Exception:
        logger.warning('环境监听主机处理器启动失败', exc_info=True)
    if TASK_SCHEDULER_AVAILABLE:
        print('定时任务检查已启动：每30秒扫描到期任务')
    if _music_monitor_mod._MONITOR_DEPS_AVAILABLE:
        try:
            _music_monitor_mod.music_monitor = _music_monitor_mod.MusicMonitor()

            async def _on_music_interrupt(song_info: dict):
                """音乐播放中断回调 → 广播到 /ws/music + 持久化到 SQLite（权威源）"""
                import time as _t
                _ts_ms = int(_t.time() * 1000)
                _content = f"音乐播放中断：{song_info.get('song', '未知')} - {song_info.get('artist', '未知')}"
                await _music_monitor_mod.music_monitor.broadcast({'type': 'music_update', 'event': 'playback_interrupted', 'message': _content, 'song': song_info, 'timestamp': _ts_ms})
                try:
                    _sid = get_latest_session_id()
                    if _sid:
                        from summary import conversation_manager
                        await conversation_manager.add_message(_sid, 'assistant', _content, message_id=f'music_{_ts_ms}')
                except Exception:
                    logger.warning('音乐消息持久化失败', exc_info=True)
            _music_monitor_mod.music_monitor.start(on_interrupt=_on_music_interrupt)
            print('MusicMonitor 已启动')
            try:
                _music_monitor_mod.music_profile = MusicProfile()
                print('MusicProfile 已初始化')
            except Exception as e:
                print(f'MusicProfile 初始化失败: {e}')
                _music_monitor_mod.music_profile = None
        except Exception as e:
            print(f'MusicMonitor 启动失败: {e}')
    else:
        print('MusicMonitor 依赖不可用（非 Windows 或缺少 win32gui/psutil），跳过')

async def _check_due_tasks():
    """检查到期定时任务并触发"""
    try:
        from task_scheduler import get_global_task_manager
        manager = get_global_task_manager()
        triggered = await manager.check_due_tasks()
        if triggered:
            for task in triggered:
                print(f'[TASK_SCHEDULER] 任务已触发: {task.title}')
        _record_cron_success('task_check')
    except Exception as e:
        _record_cron_error('task_check', str(e))
        logger.error(f'检查到期定时任务失败: {e}')

async def _do_daily_maintenance():
    """每日维护：重置计数器 → 清理"""
    try:
        from event_chronicle import update_maintenance_progress, reset_maintenance_status
        reset_maintenance_status()
        from event_suggestion import reset_daily_counts
        reset_daily_counts()
        update_maintenance_progress('diary_generation', 100, '由聊到这里处理', 'completed')
        update_maintenance_progress('bucket_migration', 100, '已废弃', 'completed')
        update_maintenance_progress('log_cleanup', 100, '已废弃', 'completed')
        try:
            from calendar_manager.manager import get_global_calendar_manager
            cal_mgr = get_global_calendar_manager()
            scan_result = await cal_mgr.scan_today_events(days_absent=0)
            if scan_result and scan_result.messages:
                await cal_mgr.broadcast_scan_result(scan_result)
                sid = _latest_session_id
                if sid:
                    import uuid
                    for msg in scan_result.messages:
                        gen_text = msg.get('generated_message', '')
                        if not gen_text:
                            continue
                        msg_id = f'cal_daily_{uuid.uuid4().hex[:8]}'
                        try:
                            await conversation_manager.add_message(sid, 'assistant', gen_text, message_id=msg_id, is_wander=False, is_sentinel=False, is_reminder=False)
                        except Exception as e:
                            logger.warning(f'每日日历扫描 conv_mgr写入失败: {e}')
                        try:
                            await _persist_special_msg(sid, msg_id, gen_text, is_wander=False, is_sentinel=False, is_reminder=False)
                        except Exception as e:
                            logger.warning(f'每日日历扫描 session写入失败: {e}')
                    try:
                        for ws in list(wander_connections):
                            try:
                                await ws.send_json({'type': 'chat_reload', 'session_id': sid})
                            except Exception:
                                logger.exception('calendar refresh broadcast failed')
                    except Exception:
                        logger.exception('unexpected error in _do_daily_maintenance')
                        pass
                    logger.info(f'每日日历扫描: {len(scan_result.messages)} 条消息已注入会话 {sid}')
                else:
                    logger.info(f'每日日历扫描: {len(scan_result.messages)} 条消息但无活跃会话，仅广播')
            else:
                logger.info('每日日历扫描: 今天无纪念日/生理期事件')
        except Exception as e:
            logger.warning(f'每日日历扫描失败: {e}')
        try:
            from ledger_manager.manager import get_global_ledger_manager
            ledger_mgr = get_global_ledger_manager()
            decay_result = ledger_mgr.run_decay_cycle()
            total = decay_result['active_to_dormant'] + decay_result['dormant_to_archived']
            if total > 0:
                logger.info(f'记账本衰减完成: {decay_result}')
        except Exception as e:
            logger.warning(f'记账本衰减失败: {e}')
        from event_chronicle import get_maintenance_status
        status = get_maintenance_status()
        status.last_run = now_iso()
        status.last_success = now_iso()
        _record_cron_success('daily_maintenance')
        logger.info('每日维护全部完成')
    except Exception as e:
        _record_cron_error('daily_maintenance', str(e))
        logger.error(f'每日维护失败: {e}', exc_info=True)
    try:
        from auto_start_manager import ensure_defaults
        result = ensure_defaults()
        for k, v in result.items():
            print(f'[AUTO_START] {k}: {v}', flush=True)
    except Exception as e:
        print(f'[AUTO_START] 默认自启注册失败（非关键）: {e}', flush=True)

@app.on_event('shutdown')
async def shutdown_event():
    global _tasks_shutting_down
    _tasks_shutting_down = True
    from day_end_scheduling import stop_tracked_tasks
    await stop_tracked_tasks(_running_task_objs)
    from habit_radar.runtime import shutdown as stop_habit_shadow
    from memory_v2.scene_digest_runtime import shutdown as stop_scene_digests
    await stop_scene_digests()
    await stop_habit_shadow()
    from music_system.integration import stop as stop_music_system
    await stop_music_system()
    global http_client, profile_manager, wander_manager, behavior_scheduler, _due_check_task, _phone_browser
    try:
        from affect.quiet_timer import stop_quiet_timer
        await stop_quiet_timer()
    except Exception:
        logger.debug('affect quiet checkpoint timer stop skipped', exc_info=True)

    async def _stop_with_timeout(name: str, coro, timeout: float=5.0):
        """带超时的关闭辅助函数，防止单个组件卡住整个 shutdown"""
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            print(f'{name} 已停止', flush=True)
        except asyncio.TimeoutError:
            print(f'停止 {name} 超时（{timeout}s），已跳过', flush=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f'停止 {name} 失败: {e}', flush=True)
    try:
        from ambient_listening import get_ambient_listening_service
        await _stop_with_timeout('环境监听主机处理器', get_ambient_listening_service().stop())
    except Exception:
        logger.debug('环境监听关闭跳过', exc_info=True)
    if wander_manager is not None:
        await _stop_with_timeout('思维漫想管理器', wander_manager.stop(), timeout=10.0)
        wander_manager = None
    if _music_monitor_mod.music_monitor:
        try:
            _music_monitor_mod.music_monitor.stop()
            print('MusicMonitor 已停止', flush=True)
        except Exception as e:
            print(f'停止 MusicMonitor 失败: {e}', flush=True)
    if _phone_browser is not None:
        try:
            if _cochlea:
                await _cochlea.stop()
                print('[MUSIC_COCHLEA] MusicCochlea 已停止', flush=True)
            await _phone_browser.stop()
            _phone_browser = None
            print('[PHONE_BROWSE] PhoneBrowser 已停止', flush=True)
        except Exception as e:
            print(f'[PHONE_BROWSE] 停止失败: {e}', flush=True)
    try:
        from behavior_scheduler.music_mcp_client import stop_mcp_client
        await stop_mcp_client()
    except Exception as e:
        print(f'[MUSIC] MCP 客户端关闭失败: {e}', flush=True)
    if SENTINEL_AVAILABLE:
        try:
            sentinel = get_sentinel()
            if sentinel:
                await _stop_with_timeout('硅基哨兵', sentinel.stop(), timeout=5.0)
        except Exception:
            pass
    for ws in list(wander_connections):
        try:
            await asyncio.wait_for(ws.close(code=1001, reason='Server shutting down'), timeout=2.0)
        except (Exception, asyncio.CancelledError):
            logger.debug('cleanup: _stop_with_timeout: (Exception, asyncio.CancelledError)')
            pass
    wander_connections.clear()
    monitor = _music_monitor_mod.music_monitor
    if monitor:
        for ws in list(monitor._connections):
            try:
                await asyncio.wait_for(ws.close(code=1001, reason='Server shutting down'), timeout=2.0)
            except (Exception, asyncio.CancelledError):
                logger.debug('cleanup: _stop_with_timeout: (Exception, asyncio.CancelledError)')
                pass
        monitor._connections.clear()
    try:
        manager = get_global_session_manager()
        await asyncio.wait_for(manager.flush_all_buffers(), timeout=5.0)
    except (Exception, asyncio.CancelledError):
        logger.debug('cleanup: _stop_with_timeout: (Exception, asyncio.CancelledError)')
        pass
    if http_client is not None:
        await _stop_with_timeout('HTTP客户端', http_client.aclose(), timeout=3.0)
        close_http_client()
    if _due_check_task and (not _due_check_task.done()):
        _due_check_task.cancel()
        try:
            await asyncio.wait_for(_due_check_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            logger.debug('cleanup: _stop_with_timeout: (asyncio.TimeoutError, asyncio.CancelledError, Exception)')
            pass
        _due_check_task = None
    cancelled = 0
    cancelled_tasks = []
    try:
        all_running = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and (not t.done())]
        if all_running:
            names = [t.get_name() for t in all_running]
            print(f'待取消的后台任务 ({len(all_running)}): {names}', flush=True)
        for task in all_running:
            task.cancel()
            cancelled_tasks.append(task)
            cancelled += 1
        if cancelled > 0:
            print(f'已取消 {cancelled} 个后台任务', flush=True)
            if cancelled_tasks:
                try:
                    await asyncio.wait(cancelled_tasks, timeout=3.0)
                except asyncio.CancelledError:
                    logger.debug('cleanup: _stop_with_timeout: asyncio.CancelledError')
                    pass
    except (Exception, asyncio.CancelledError):
        logger.debug('cleanup: _stop_with_timeout: (Exception, asyncio.CancelledError)')
        pass
    s = get_scheduler()
    if s is not None:
        s.shutdown(wait=False)
        set_scheduler(None)
    cs = get_context_scheduler()
    if cs is not None:
        await _stop_with_timeout('上下文调度器', cs.close(), timeout=5.0)
        set_context_scheduler(None)
    from context_scheduler_log import close_log
    close_log()
    if profile_manager is not None:
        await _stop_with_timeout('画像管理器', profile_manager.close(), timeout=5.0)
        profile_manager = None
    loop = asyncio.get_running_loop()
    old_executor = getattr(loop, '_default_executor', None)
    if old_executor is not None:
        from concurrent.futures import ThreadPoolExecutor
        new_executor = ThreadPoolExecutor(1)
        loop.set_default_executor(new_executor)
        old_executor.shutdown(wait=False, cancel_futures=True)
        new_executor.shutdown(wait=False, cancel_futures=True)
        print('已释放新旧默认 executor（避免 shutdown 卡死）', flush=True)
    try:
        from import_export import cleanup_stale_imports
        cleanup_stale_imports(max_age_hours=24)
    except Exception:
        logger.exception('unexpected error in _stop_with_timeout')
        pass

class TopicStatusResponse(BaseModel):
    """话题状态响应"""
    is_active: bool
    start_time: Optional[str] = None
    message_count: int = 0
    round_count: int = 0

class ChatRequest(BaseModel):
    message: str
    model: str = 'deepseek'
    use_memory: bool = True
    system_prompt: Optional[str] = None
    session_id: Optional[str] = None
    is_after_divider: bool = False
    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    images: Optional[List[Dict[str, str]]] = None
    image_model: str = 'flash-vision'
    night_mode: bool = False
    music_mode: bool = False
    phone_browse_mode: bool = False
    voice_prosody: Optional[Dict[str, Any]] = None
    voice_attachment: Optional[Dict[str, Any]] = None
    long_text_card: Optional[Dict[str, str]] = None
    book_reference_id: Optional[str] = None
    accepted_resend: bool = False

class ImageModelConfigRequest(BaseModel):
    image_model: str = 'flash-vision'

class ChatResponse(BaseModel):
    duplicate: bool = False
    failure_reason: str = ''
    memory_used: bool = False
    model_used: str
    thinking_chain: Optional[List[ThinkingStep]] = None
    topic_status: Optional[TopicStatusResponse] = None
    topic_ended: bool = False
    divider_index: int = -1
    tool_calls: List[Dict[str, Any]] = []
    need_user_input: bool = False

class SummarySearchRequest(BaseModel):
    query: str
    top_k: int = 5
    bucket_filter: Optional[str] = None

class BucketSummaryRequest(BaseModel):
    bucket: str
    limit: int = 100
    offset: int = 0

class MigrationResponse(BaseModel):
    total_processed: int
    migrated: int
    errors: int

class BucketStats(BaseModel):
    bucket_counts: Dict[str, int]
    importance_averages: Dict[str, float]
    recent_activity: Dict[str, Optional[str]]
from routers.shared_models import ModelApiStatus, SystemStatus

class ToolCallRecord(BaseModel):
    """工具调用记录模型"""
    tool: str
    description: Optional[str] = None
    parameters: Dict[str, Any] = {}
    result: Optional[Any] = None
    extra_data: Optional[Dict[str, Any]] = None
    success: Optional[bool] = None
    delivery: str = ''
    status: str = ''
    error: Optional[str] = None

    @classmethod
    def from_msg_tc(cls, tc):
        """从消息的 toolCall 项创建 ToolCallRecord（兼容 dict 和对象）"""
        if isinstance(tc, dict):
            return cls(tool=tc.get('tool', ''), description=tc.get('description', ''), parameters=tc.get('parameters') or {}, result=tc.get('result'), extra_data=tc.get('extra_data'), success=tc.get('success'), delivery=tc.get('delivery', ''), status=tc.get('status', ''), error=tc.get('error'))
        return cls(tool=getattr(tc, 'tool', ''), description=getattr(tc, 'description', ''), parameters=getattr(tc, 'parameters', {}) or {}, result=getattr(tc, 'result', None), extra_data=getattr(tc, 'extra_data', None), success=getattr(tc, 'success', None), delivery=getattr(tc, 'delivery', ''), status=getattr(tc, 'status', ''), error=getattr(tc, 'error', None))

class FrontendMessage(BaseModel):
    """前端消息模型"""
    id: str
    role: str
    content: str
    timestamp: str
    thinkingChain: Optional[List[ThinkingStep]] = None
    modelSource: Optional[Dict[str, Any]] = None
    toolCalls: Optional[List[ToolCallRecord]] = None
    isWander: Optional[bool] = None
    isSentinel: Optional[bool] = None
    isReminder: Optional[bool] = None
    eventType: Optional[str] = None
    notificationMetadata: Optional[Dict[str, Any]] = None
    images: Optional[List[Dict[str, Any]]] = None
    voiceAttachment: Optional[Dict[str, Any]] = None

class FrontendSession(BaseModel):
    """前端会话模型"""
    id: str
    title: str
    messages: List[FrontendMessage] = []
    createdAt: str
    updatedAt: str
    locked: bool = False

class SessionListResponse(BaseModel):
    """会话列表响应"""
    sessions: List[FrontendSession]
    current_session_id: str

class SessionCreateRequest(BaseModel):
    """创建会话请求"""
    id: Optional[str] = None
    title: Optional[str] = None
    messages: Optional[List[FrontendMessage]] = None

class SessionUpdateRequest(BaseModel):
    """更新会话请求"""
    title: Optional[str] = None
    messages: Optional[List[FrontendMessage]] = None

class ClearSessionsRequest(BaseModel):
    """清除所有会话请求"""
    confirm: bool = False

class RevokeMessageRequest(BaseModel):
    """撤回消息请求"""
    session_id: str
    user_message_id: str
    delete_assistant_reply: bool = True

def convert_msg_to_frontend(msg_dict: dict) -> FrontendMessage:
    """将消息字典转换为FrontendMessage，保留thinkingChain和toolCalls"""
    thinking_chain = None
    if msg_dict.get('thinkingChain'):
        thinking_chain = [ThinkingStep(content=step.get('content', ''), timestamp=step.get('timestamp', '')) for step in msg_dict['thinkingChain']]
    elif msg_dict.get('reasoning_content'):
        thinking_chain = [ThinkingStep(content=msg_dict['reasoning_content'], timestamp=msg_dict.get('timestamp', ''))]
    tool_calls = None
    if msg_dict.get('toolCalls'):
        tool_calls = [ToolCallRecord(tool=tc.get('tool', ''), description=tc.get('description', ''), parameters=tc.get('parameters') or {}, result=tc.get('result'), extra_data=tc.get('extra_data'), success=tc.get('success'), delivery=tc.get('delivery', ''), status=tc.get('status', ''), error=tc.get('error')) for tc in msg_dict['toolCalls']]
    return FrontendMessage(id=msg_dict.get('id', ''), role=msg_dict.get('role', 'user'), content=msg_dict.get('content', ''), timestamp=msg_dict.get('timestamp', ''), thinkingChain=thinking_chain, modelSource=msg_dict.get('modelSource'), toolCalls=tool_calls, eventType=msg_dict.get('eventType'), notificationMetadata=msg_dict.get('notificationMetadata') or msg_dict.get('notification_metadata'), images=msg_dict.get('images'), isWander=msg_dict.get('isWander'), isSentinel=msg_dict.get('isSentinel'), isReminder=msg_dict.get('isReminder'), voiceAttachment=msg_dict.get('voiceAttachment'))
if TASK_SCHEDULER_AVAILABLE:
    from task_scheduler import router as task_router
    app.include_router(task_router)
    print('定时任务管理器路由已注册')
try:
    from ledger_manager import ledger_router
    app.include_router(ledger_router)
    print('记账本路由已注册')
except Exception as e:
    print(f'记账本路由注册失败: {e}')
try:
    from calendar_manager import router as calendar_router
    app.include_router(calendar_router)
    print('日历管理器路由已注册')
except Exception as e:
    print(f'日历管理器路由注册失败: {e}')
from health_tracker import router as health_router
app.include_router(health_router)
print('健康追踪路由已注册')
from routers.import_export_router import router as import_export_router
app.include_router(import_export_router)
print('导入导出路由已注册')
from cognition.retirement import RetiredCognitionMiddleware
app.add_middleware(RetiredCognitionMiddleware)
from routers.persona_router import router as persona_router
app.include_router(persona_router)
print('人设演化路由已注册')
from routers.memory_router import router as memory_router
app.include_router(memory_router)
print('记忆库路由已注册')
from routers.bookmark_router import router as bookmark_router
app.include_router(bookmark_router)
print('书签路由已注册')
from routers.nightmode_router import router as nightmode_router
app.include_router(nightmode_router)
from routers.config_router import router as config_router
app.include_router(config_router)
from api_sources.router import router as api_sources_router
app.include_router(api_sources_router)
from routers.social_feed_router import router as social_feed_router
app.include_router(social_feed_router)
from routers.social_sites_router import router as social_sites_router
app.include_router(social_sites_router)
from social_feed.decor_router import create_decor_router, ui_router as social_decor_ui_router, install_errors as install_decor_errors
app.include_router(create_decor_router(local=True))
app.include_router(social_decor_ui_router)
from social_feed.profile_router import create_profile_router, install_profile_errors, ui_router as social_profile_ui_router
app.include_router(create_profile_router(local=True))
app.include_router(social_profile_ui_router)
install_profile_errors(app)
install_decor_errors(app)
try:
    from routers.commerce_router import router as commerce_router
    app.include_router(commerce_router)
    print('购物域路由已注册')
except Exception as e:
    print(f'购物域路由注册失败: {type(e).__name__}')
try:
    from routers.rift_archive_router import router as rift_archive_router
    app.include_router(rift_archive_router)
    print('裂隙档案路由已注册')
except Exception as e:
    print(f'裂隙档案路由注册失败: {type(e).__name__}')
from routers.phone_browse.router import router as phone_browse_router
app.include_router(phone_browse_router)
from routers.voice_router import router as voice_router
app.include_router(voice_router)
from ambient_listening.router import router as ambient_listening_router
app.include_router(ambient_listening_router)
from routers.mirrow_router import router as mirrow_router
app.include_router(mirrow_router)
from routers.world_book_router import router as world_book_router
app.include_router(world_book_router)
from routers.self_book_router import router as self_book_router
app.include_router(self_book_router)
from cognition.router import router as cognition_router
app.include_router(cognition_router)
from cognition.maintenance_router import router as cognition_maintenance_router
app.include_router(cognition_maintenance_router)
from cognition.other_router import router as other_book_router
app.include_router(other_book_router)
from routers.event_router import router as event_router
app.include_router(event_router)
from routers.wander_router import router as wander_router
app.include_router(wander_router)
from routers.essay_router import router as essay_router
app.include_router(essay_router)
from routers.xhs_posts_router import router as xhs_posts_router
app.include_router(xhs_posts_router)
from xhs_actions.dm_admin import router as xhs_direct_messages_router
app.include_router(xhs_direct_messages_router)
from routers.topic_router import router as topic_router
app.include_router(topic_router)
from routers.covenant_router import router as covenant_router
app.include_router(covenant_router)
from conversation_covenant import observe_accepted_message
from routers.session_router import router as session_router
app.include_router(session_router)
from routers.system_router import router as system_router
app.include_router(system_router)
try:
    from routers.affect_router import router as affect_router
    app.include_router(affect_router)
    print('情绪与开放线索路由已注册')
except Exception as e:
    print(f'情绪与开放线索路由注册失败: {type(e).__name__}')
from routers.message_router import router as message_router
app.include_router(message_router)
from routers.memory_v2_trace_router import router as memory_v2_trace_router
app.include_router(memory_v2_trace_router)
from memory_v2.scene_router import router as memory_v2_scene_router
app.include_router(memory_v2_scene_router)
from habit_radar.router import router as habit_radar_router
app.include_router(habit_radar_router)
from scene_manager.api import router as scene_router
app.include_router(scene_router)
from music_router import router as music_router, setup_music_router
app.include_router(music_router)
from music_system import router as music_system_router
app.include_router(music_system_router)
from silicon_perception.router import router as sentinel_router
app.include_router(sentinel_router)
print('刷手机模式路由已注册')

@app.post('/chat', response_model=ChatResponse)
async def chat(request: Request, message: Optional[str]=Form(None), model: str=Form('deepseek'), use_memory: bool=Form(True), system_prompt: Optional[str]=Form(None), session_id: Optional[str]=Form(None), images: List[UploadFile]=File([]), image_model: str=Form('flash-vision')):
    """处理聊天请求，支持文本和图片"""
    logger.debug(f"chat: session_id={session_id or 'none'} model={model} use_memory={use_memory} images={len(images)} image_model={image_model}")
    is_after_divider = False
    user_message_id = None
    assistant_message_id = None
    voice_prosody = None
    voice_attachment = None
    night_mode = False
    music_mode = False
    phone_browse_mode = False
    long_text_card = None
    book_reference_id = None
    resend_payload = None
    if message is None:
        try:
            raw_body = await request.body()
            encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1']
            body = None
            last_error = None
            for encoding in encodings:
                try:
                    import json
                    body = json.loads(raw_body.decode(encoding))
                    logger.debug(f'Successfully decoded JSON with {encoding} encoding')
                    break
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    last_error = e
                    continue
            if body is None:
                raise ValueError(f'Failed to decode JSON with any encoding: {last_error}')
            json_request = ChatRequest(**body)
            message = json_request.message
            model = json_request.model
            use_memory = json_request.use_memory
            system_prompt = json_request.system_prompt
            if system_prompt:
                set_latest_persona_prompt(system_prompt)
            session_id = json_request.session_id
            is_after_divider = json_request.is_after_divider
            user_message_id = json_request.user_message_id
            book_reference_id = json_request.book_reference_id
            assistant_message_id = json_request.assistant_message_id
            image_model = json_request.image_model
            night_mode = json_request.night_mode
            music_mode = json_request.music_mode
            phone_browse_mode = json_request.phone_browse_mode
            voice_prosody = json_request.voice_prosody
            voice_attachment = json_request.voice_attachment
            long_text_card = json_request.long_text_card
            if json_request.accepted_resend:
                from message_resend_payload import load_accepted_resend
                resend_payload = await asyncio.to_thread(load_accepted_resend, session_id, user_message_id, message, CHAT_IMAGE_DIR)
                message = resend_payload['message']
                voice_attachment, long_text_card = (resend_payload['voice_attachment'], resend_payload['long_text_card'])
                voice_prosody, book_reference_id = (None, None)
            json_images = None if resend_payload else json_request.images
            if json_images and len(json_images) > 0:
                images = []
                for img in json_images:
                    img_data = img.get('data', '')
                    if img_data:
                        if 'base64,' in img_data:
                            b64_data = img_data.split('base64,', 1)[1]
                        else:
                            b64_data = img_data
                        try:
                            raw = base64.b64decode(b64_data)
                            mime = img.get('type', 'image/jpeg')
                            filename = img.get('filename', f"image_{len(images)}.{mime.split('/')[-1]}")
                            images.append(UploadFile(filename=filename, file=io.BytesIO(raw), headers={'content-type': mime}))
                        except Exception:
                            logger.exception('unexpected error in chat')
                            pass
            else:
                images = []
        except Exception as e:
            logger.error(f'Failed to parse request body: {e}')
            raise HTTPException(status_code=400, detail=f'Invalid request body: {str(e)}')
    if long_text_card and (not resend_payload):
        try:
            from long_text_cards import normalize_long_text_card
            long_text_card = normalize_long_text_card(long_text_card)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if not message and len(images) == 0 and (not long_text_card) and (not (resend_payload and resend_payload['images'])):
        raise HTTPException(status_code=400, detail='Message or image is required')
    target_model = model
    text_content_for_deepseek = message or ''
    image_analysis = None
    image_data_urls = resend_payload['image_data_urls'] if resend_payload else []
    if len(images) > 0:
        for _img in images:
            _c = await _img.read()
            _mime = _img.content_type or 'image/jpeg'
            image_data_urls.append(f"data:{_mime};base64,{base64.b64encode(_c).decode('utf-8')}")
            await _img.seek(0)
    saved_image_meta = resend_payload['images'] if resend_payload else None
    if len(images) > 0:
        saved_image_meta = await _save_chat_images_from_upload(images)
        if saved_image_meta:
            logger.debug(f'HTTP saved {len(saved_image_meta)} images')
    final_content_for_memory = ''
    final_content_for_memory = text_content_for_deepseek if text_content_for_deepseek else '[包含图片的消息]' if image_data_urls else ''
    text_for_llm = text_content_for_deepseek or ''
    if long_text_card:
        from long_text_cards import card_for_first_turn
        _card_first_turn = card_for_first_turn(long_text_card)
        text_for_llm = f'{text_for_llm}\n{_card_first_turn}' if text_for_llm else _card_first_turn
    voice_prosody_summary = (voice_prosody or {}).get('summary', '')
    if voice_prosody_summary:
        voice_inject = f'\n\n[系统注：用户发来一条语音。语气分析：{voice_prosody_summary}]'
        text_for_llm = text_for_llm + voice_inject if text_for_llm else voice_inject.strip()
    if session_id and session_id in _pending_music_analysis:
        music_summary, _ = _pending_music_analysis.pop(session_id)
        music_inject = f'\n\n[系统注：用户刚才分享了一首音乐。音乐结构分析：{music_summary}]'
        text_for_llm = text_for_llm + music_inject if text_for_llm else music_inject.strip()
    final_content_for_memory = _sanitize_user_message(final_content_for_memory, image_analysis)
    persist_user_content = final_content_for_memory
    if image_data_urls:
        persist_user_content = f'[图片消息]\n{final_content_for_memory}' if final_content_for_memory else '[图片消息]'
    if resend_payload:
        persist_user_content = resend_payload['content']
    _http_turn_claim = None
    _turn_guard = None
    if behavior_scheduler is not None:
        from chat_turn_guard import get_chat_turn_guard
        _turn_guard = get_chat_turn_guard()
        if _turn_guard.is_mutating(session_id or ''):
            raise HTTPException(409, '消息正在修改，请稍后再发送')
        _http_turn_claim = _turn_guard.claim(session_id=session_id or '', user_message_id=user_message_id or '', content=final_content_for_memory, fallback_anchor=str((long_text_card or {}).get('id') or ''))
        if _http_turn_claim is None:
            logger.info('HTTP duplicate generation skipped | session=%s message_id=%s', session_id, user_message_id)
            return ChatResponse(duplicate=True, model_used='', tool_calls=[], thinking_chain=[], topic_status=None, topic_ended=False, divider_index=-1)
        asyncio.current_task().add_done_callback(lambda _task, guard=_turn_guard, token=_http_turn_claim: guard.fail(token))
    from conversation_covenant import get_conversation_covenant_service
    await get_conversation_covenant_service().cancel_reservation(session_id)
    from long_text_cards import card_message_label
    _accepted_user_text = final_content_for_memory or (card_message_label(long_text_card) if long_text_card else '')
    await observe_accepted_message(_accepted_user_text, session_id, 'HTTP')
    import time as _time
    set_last_user_message_time(_time.monotonic())
    user_msg = None
    topic_result = None
    if session_id and _accepted_user_text:
        active_date = await _get_active_date(session_id)
        from summary import Message as SummaryMessage
        user_msg = SummaryMessage(id=user_message_id or f'user_{now_iso()}', role='user', content=_accepted_user_text, timestamp=now_iso())
        set_latest_session_id(session_id)
        private_last = get_last_private_chat_time()
        night_mode = get_night_mode_active()
        set_music_mode_active(False)
        phone_browse_mode = get_phone_browse_mode()
        if TASK_SCHEDULER_AVAILABLE:
            get_global_task_manager().set_current_session_id(session_id)
        from wander_manager.user_status import check_wakeup
        if check_wakeup():
            if WANDER_MANAGER_AVAILABLE and wander_manager:
                wander_manager.set_user_status('idle', '', source='wakeup')
            else:
                from wander_manager.user_status import set_user_status_combined, UserStatus
                set_user_status_combined(UserStatus.IDLE, '', source='wakeup')
                try:
                    from health_tracker.tracker import get_health_tracker
                    get_health_tracker().on_status_change('sleeping', 'idle', '')
                except Exception:
                    logger.exception('unexpected error in chat')
                    pass
        await _persist_user_msg_early(session_id, user_message_id, persist_user_content, images=saved_image_meta if saved_image_meta else None, voice_attachment=voice_attachment, long_text_card=long_text_card, user_event=None)
        topic_result = None
        if user_msg:
            topic_status = await topic_state_machine.get_topic_status(session_id)
            pre_start_time = topic_status.get('start_time', '') if topic_status else ''
            await topic_state_machine.maybe_end_for_day_change(session_id)
            topic_result = await topic_state_machine.process_message(session_id, user_msg, is_after_divider=is_after_divider)
            _recently_active_sessions.add(session_id)
            if topic_result.get('topic_ended'):
                asyncio.create_task(_on_topic_ended(session_id, 'manual', pre_start_time))
    private_last = get_last_private_chat_time()
    private_away_sec = (datetime.now() - private_last).total_seconds() if private_last else None
    away_info = None
    _activity_interrupt_text = ''
    if wander_manager and _accepted_user_text:
        wander_manager.on_user_reply()
        await wander_manager.settle_runtime_interrupt()
        away_info = wander_manager.get_away_info()
        try:
            _activity_interrupt_text = wander_manager.get_activity_interrupt_text()
        except Exception:
            _activity_interrupt_text = ''
    if _accepted_user_text:
        set_last_private_chat_time(datetime.now())
    if _accepted_user_text and TASK_SCHEDULER_AVAILABLE:
        await get_global_task_manager().on_user_replied()
    messages = []
    memory_used = False
    is_new_topic = False
    inject_yesterday = False
    if topic_result:
        topic_started = topic_result.get('topic_started', False)
        is_first_today = _mark_diary_injected_today()
        is_new_topic = topic_started and session_id is not None and is_first_today
        if is_new_topic:
            global _first_topic_session_id
            _first_topic_session_id = session_id
        inject_yesterday = True
    context_result = None
    if use_memory and get_context_scheduler() is not None:
        try:
            from chat_context_prefetch import prefetch_chat_context_inputs
            user_profile, time_hint = await prefetch_chat_context_inputs(session_id or '', profile_loader=get_global_profile_manager().get_profile, time_hint_loader=_compute_time_awareness_hint, current_timestamp=now_iso(), timeout=5.0)
            if voice_prosody:
                from voice_manager.voice_cochlea import set_current_prosody
                set_current_prosody(voice_prosody)
            context_result = await asyncio.wait_for(get_context_scheduler().schedule_context(user_message=final_content_for_memory, persona=system_prompt or '', user_profile=user_profile, force_retrieve=False, session_id=session_id or '', is_new_topic=is_new_topic, inject_yesterday=inject_yesterday, away_duration_seconds=private_away_sec, away_reason=(away_info['away_reason'] if away_info else '') + _activity_interrupt_text or None, night_mode=night_mode, phone_browse_mode=phone_browse_mode, music_cochlea=music_mode, time_awareness_hint=time_hint, current_message_id=user_message_id, is_wander_active=False), timeout=30.0)
            messages = list(context_result.formatted_messages)
            memory_used = len(context_result.memories) > 0
            logger.debug(f'Context scheduler success: {len(context_result.memories)} memories, {len(messages)} messages')
        except asyncio.TimeoutError:
            logger.warning('Context scheduler timeout, using degraded context')
            memory_used = False
            if system_prompt:
                messages.append(Message(role='system', content=system_prompt))
            if final_content_for_memory:
                messages.append(Message(role='user', content=final_content_for_memory))
        except Exception as e:
            logger.warning(f'Context scheduler failed: {e}, falling back')
            memory_used = False
            if system_prompt:
                messages.append(Message(role='system', content=system_prompt))
    else:
        memory_used = False
        if system_prompt:
            messages.append(Message(role='system', content=system_prompt))
        if use_memory and get_context_scheduler() is None:
            from memory_v2.context_shadow import recall_context_from_environment
            observation, memory_text = await asyncio.to_thread(recall_context_from_environment, final_content_for_memory, reference_date=datetime.now().date(), session_id=session_id)
            if memory_text:
                messages.append(Message(role='system', content=memory_text))
                memory_used = True
        messages.append(Message(role='user', content=final_content_for_memory))
        try:
            from context_builder import record_recipe_call, estimate_tokens
            sys_parts = [m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '') for m in messages if (m.get('role') if isinstance(m, dict) else getattr(m, 'role', '')) == 'system']
            sys_text = '\n'.join(sys_parts)
            record_recipe_call('AI 私聊（降级）', 'degraded', sections={'system_prompt': sys_text[:500], 'reason': 'scheduler 不可用或超时'}, total_chars=len(sys_text), total_tokens=estimate_tokens(sys_text) if estimate_tokens else 0)
        except Exception:
            pass
    target_model = model
    tool_calls = []
    need_user_input = False
    scheduler_result = None
    fallback_image_description = None
    _sop_lock = None
    _sop_lock_owned = False
    scheduler_message_id = user_message_id or getattr(user_msg, 'id', '') or f'user_{now_iso()}'
    try:
        if behavior_scheduler is not None:
            from context_builder.assembly import assemble_messages, split_context_result_for_scheduler
            try:
                system_prompt_for_scheduler, conv_history = split_context_result_for_scheduler(context_result, formatted_messages=messages)
            except (NameError, AttributeError):
                conv_history = assemble_messages(history=messages)
                system_prompt_for_scheduler = ''
                if conv_history and conv_history[0].get('role') == 'system':
                    system_prompt_for_scheduler = str(conv_history.pop(0).get('content') or '')
            conv_history = [msg for msg in conv_history if msg.get('role') in {'system', 'user', 'assistant'} and (not is_ui_only_role(msg.get('role')))]
            if not system_prompt_for_scheduler.strip() and _latest_persona_prompt:
                logger.warning('HTTP: system_prompt_for_scheduler 为空，回退到 _latest_persona_prompt')
                system_prompt_for_scheduler = _latest_persona_prompt
            _sop_lock = get_session_op_lock(session_id)
            await _sop_lock.acquire()
            _sop_lock_owned = True
            scheduler_message_id = user_message_id or getattr(user_msg, 'id', '') or f'user_{now_iso()}'
            try:
                scheduler_result = await behavior_scheduler.process_message(user_message=text_for_llm or final_content_for_memory, conversation_history=conv_history, system_prompt=system_prompt_for_scheduler, night_mode_enabled=night_mode, platform=_request_platform.get(), session_id=session_id, images=image_data_urls if image_data_urls else None, message_id=scheduler_message_id, turn_id=scheduler_message_id, inspection_call_id=getattr(context_result, 'call_id', ''), raw_user_message=final_content_for_memory)
            except Exception:
                _turn_guard.fail(_http_turn_claim)
                raise
            reply_content = scheduler_result.final_response
            tool_calls = scheduler_result.tool_calls
            need_user_input = scheduler_result.need_user_input
            reasoning = scheduler_result.thinking_chain[-1]['content'] if scheduler_result.thinking_chain else None
            intermediate_messages = scheduler_result.intermediate_messages
            thinking_chain = None
            if scheduler_result.thinking_chain:
                thinking_chain = [ThinkingStep(content=step['content'], timestamp=step['timestamp']) for step in scheduler_result.thinking_chain]
            logger.debug(f'Behavior scheduler done: {len(tool_calls)} tools, reply={len(reply_content)} chars, {len(intermediate_messages)} intermediate')
            _is_immediate_checkup = await _emit_immediate_checkup_receipt(session_id, scheduler_message_id, final_content_for_memory, tool_calls)
            if _is_immediate_checkup:
                scheduler_result.ui_hide_sensory_tools = True
                from wander_manager.checkup_receipts import visible_chat_tool_calls
                tool_calls = visible_chat_tool_calls(tool_calls, hide_sensory=True)
        else:
            try:
                from context_builder.assembly import assemble_context_result
                direct_messages = assemble_context_result(context_result, formatted_messages=messages)
            except (NameError, AttributeError):
                direct_messages = list(messages)
            direct_user_content = text_for_llm or final_content_for_memory
            if direct_user_content:
                direct_messages.append(Message(role='user', content=direct_user_content))
            if image_data_urls:
                from visual_dialogue import generate_visual_dialogue
                from behavior_scheduler.vision_envelope import storage_description
                visual_messages = [m if isinstance(m, dict) else m.model_dump() for m in direct_messages]
                llm_response = await generate_visual_dialogue(visual_messages, [{'source_key': f'user_image_{index}', 'label': f'人类伙伴发送的图片{index}', 'image_url': url} for index, url in enumerate(image_data_urls, 1)], turn_text='本轮人类伙伴主动发送的图片。', allow_tools=False)
                fallback_image_description = storage_description(llm_response['vision_envelope'])
            else:
                llm_response = await call_llm_api(direct_messages, target_model)
            reply_content = llm_response['content']
            reasoning = llm_response.get('reasoning')
            intermediate_messages = []
            thinking_chain = None
            if reasoning:
                thinking_step = ThinkingStep(content=reasoning, timestamp=now_iso())
                thinking_chain = [thinking_step]
        if scheduler_result is None:
            from types import SimpleNamespace
            scheduler_result = SimpleNamespace(intermediate_messages=intermediate_messages, tool_calls=tool_calls, thinking_chain=thinking_chain, final_response=reply_content, need_user_input=need_user_input, image_description=fallback_image_description)
        topic_ended = False
        divider_index = -1
        final_topic_status = None
        _tool_summary = None
        if tool_calls:
            _parts = []
            for tc in tool_calls:
                _name = tc.get('tool', '') or tc.get('name', '')
                _desc = (tc.get('description', '') or '')[:60]
                if _name:
                    _parts.append(f'{_name}({_desc})' if _desc else _name)
            if _parts:
                _tool_summary = ', '.join(_parts)
        if session_id:
            if not _sop_lock_owned:
                _sop_lock = get_session_op_lock(session_id)
                await _sop_lock.acquire()
                _sop_lock_owned = True
            active_date = await _get_active_date(session_id)
            push_id = getattr(behavior_scheduler, '_last_push_message_id', '')
            user_msg, assistant_msg = await _persist_chat_result(session_id=session_id, result=scheduler_result, user_message_id=user_message_id, assistant_message_id=assistant_message_id, user_content=persist_user_content, active_date=active_date, voice_attachment=voice_attachment, images=saved_image_meta if saved_image_meta else None, push_message_id=push_id, user_event=None)
            if assistant_msg and str(reply_content or '').strip():
                try:
                    from context_builder import commit_context_result
                    commit_context_result(context_result, reply_persisted=True, assistant_message_id=str(getattr(assistant_msg, 'id', '') or ''))
                except (NameError, Exception):
                    pass
            if image_data_urls and session_id and user_message_id:
                _spawn_async_task(_fill_image_description_async(session_id, user_message_id, persist_user_content, image_data_urls, final_content_for_memory, description=getattr(scheduler_result, 'image_description', None) if scheduler_result else None))
            if assistant_msg:
                assistant_topic_result = await topic_state_machine.process_message(session_id, assistant_msg, is_after_divider=False)
                if assistant_topic_result.get('topic_ended'):
                    topic_ended = True
                    divider_index = assistant_topic_result.get('divider_index', -1)
                    user_ended = topic_result.get('topic_ended', False) if topic_result else False
                    if not user_ended:
                        asyncio.create_task(_on_topic_ended(session_id, 'auto', pre_start_time))
                final_topic_status = assistant_topic_result.get('topic_status')
            elif topic_result:
                final_topic_status = topic_result.get('topic_status')
        from chat_turn_outcome import failure_reason
        if away_info and wander_manager and (not failure_reason(scheduler_result)):
            wander_manager.mark_away_delivered(cycle_id=away_info['cycle_id'])
        if _sop_lock_owned and _sop_lock is not None:
            _sop_lock.release()
            _sop_lock_owned = False
        if session_id and _turn_guard is not None:
            from chat_turn_outcome import finalize_chat_turn
            finalize_chat_turn(_turn_guard, _http_turn_claim, scheduler_result, session_id=session_id, turn_id=scheduler_message_id, persisted=bool(assistant_msg))
        return ChatResponse(failure_reason=failure_reason(scheduler_result), memory_used=memory_used, model_used=target_model, thinking_chain=thinking_chain, topic_status=TopicStatusResponse(**final_topic_status) if final_topic_status else None, topic_ended=topic_ended, divider_index=divider_index, tool_calls=tool_calls, need_user_input=need_user_input)
    finally:
        if _sop_lock_owned and _sop_lock is not None:
            _sop_lock.release()
        if _turn_guard is not None:
            _turn_guard.fail(_http_turn_claim)

@app.websocket('/ws/behavior')
async def websocket_behavior_chat(websocket: WebSocket):
    """WebSocket端点，支持实时状态推送"""
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else None
    if not _NETWORK_ACCESS.allows(client_ip):
        await websocket.send_json({'error': 'Access denied'})
        await websocket.close()
        return
    ws_platform = websocket.query_params.get('platform', 'pc')
    _request_platform.set(ws_platform)
    relay_hub = get_mobile_relay_hub() if ws_platform == 'mobile' else None
    if not behavior_scheduler:
        await websocket.send_json({'error': '行为调度器不可用'})
        await websocket.close()
        return

    async def status_callback(status: str, data: Dict):
        await websocket.send_json({'type': 'status', 'status': status, 'data': data})
    if ws_platform == 'mobile':
        relay_hub.register(websocket, client_kind='webview')
        _bind_mobile_relay(relay_hub)
        set_phone_browse_paused(True)
    else:
        pass
    _ws_queue: asyncio.Queue = asyncio.Queue()

    async def _ws_receiver():
        """后台任务：持续从 WebSocket 读取消息。
        mobile_tool_result 立即处理（绕过主循环阻塞），其余放入队列。
        """
        try:
            while True:
                data = await websocket.receive_json()
                if data.get('type') == 'mobile_tool_result':
                    handled = relay_hub.resolve_tool_result(data.get('request_id', ''), data.get('result', {}), top_success=data.get('success'), error=data.get('error', '')) if relay_hub is not None else False
                    if not handled:
                        behavior_scheduler.handle_mobile_tool_result(data.get('request_id', ''), data.get('success', False), data.get('result', {}), data.get('error', ''))
                else:
                    await _ws_queue.put(data)
        except Exception:
            pass
        finally:
            await _ws_queue.put(None)
    _receiver_task = asyncio.create_task(_ws_receiver())
    _focus_work = None
    _game_chat_work = None
    _active_ws_turn_claim = None
    try:
        while True:
            leave_work(_focus_work)
            _focus_work = None
            from wander_manager import game_chat_gate
            game_chat_gate.leave_chat(_game_chat_work)
            _game_chat_work = None
            data = await _ws_queue.get()
            _active_ws_turn_claim = None
            if data is None:
                break
            if data.get('type') == 'now_playing':
                continue
            logger.debug(f"WS message: platform={ws_platform} type={data.get('type', 'chat')} msg={str(data.get('message', ''))[:60]}")
            message = data.get('message', '')
            try:
                _game_chat_work = game_chat_gate.enter_chat()
            except RuntimeError as error:
                await websocket.send_json({'error': str(error), 'code': 'game_busy'})
                continue
            try:
                _focus_work = enter_work()
            except RuntimeError as error:
                game_chat_gate.leave_chat(_game_chat_work)
                _game_chat_work = None
                await websocket.send_json({'error': str(error), 'code': 'lounge_busy'})
                continue
            session_id = data.get('session_id')
            system_prompt = data.get('system_prompt')
            if system_prompt:
                set_latest_persona_prompt(system_prompt)
            images = data.get('images')
            image_model = data.get('image_model', 'flash-vision')
            user_message_id = data.get('user_message_id')
            assistant_message_id = data.get('assistant_message_id')
            night_mode = data.get('night_mode', False)
            music_mode = data.get('music_mode', False)
            phone_browse_mode = data.get('phone_browse_mode', False)
            voice_prosody = data.get('voice_prosody', {})
            voice_attachment = data.get('voice_attachment')
            long_text_card = data.get('long_text_card')
            resend_payload = None
            if data.get('accepted_resend'):
                from message_resend_payload import load_accepted_resend
                try:
                    resend_payload = await asyncio.to_thread(load_accepted_resend, session_id, user_message_id, message, CHAT_IMAGE_DIR)
                except ValueError as exc:
                    await websocket.send_json({'error': str(exc)})
                    continue
                message, images = (resend_payload['message'], None)
                voice_attachment, long_text_card = (resend_payload['voice_attachment'], resend_payload['long_text_card'])
                voice_prosody = {}
            if long_text_card and (not resend_payload):
                try:
                    from long_text_cards import normalize_long_text_card
                    long_text_card = normalize_long_text_card(long_text_card)
                except ValueError as exc:
                    await websocket.send_json({'error': str(exc)})
                    continue
            night_mode = get_night_mode_active()
            set_music_mode_active(False)
            phone_browse_mode = get_phone_browse_mode()
            import time as _time
            set_last_user_message_time(_time.monotonic())
            if not message and (not images) and (not long_text_card) and (not (resend_payload and resend_payload['images'])):
                await websocket.send_json({'error': '消息或图片不能为空'})
                continue
            image_analysis = ''
            ws_image_data_urls = resend_payload['image_data_urls'] if resend_payload else []
            if images and len(images) > 0:
                ws_image_data_urls = [img['data'] for img in images if img.get('data')]
            ws_saved_image_meta = resend_payload['images'] if resend_payload else None
            if images and len(images) > 0:
                ws_saved_image_meta = _save_chat_images_from_base64(images)
                if ws_saved_image_meta:
                    logger.debug(f'WS saved {len(ws_saved_image_meta)} images')
            final_message = message if message else '[包含图片的消息]' if ws_image_data_urls else ''
            final_message = _sanitize_user_message(final_message, image_analysis)
            ws_persist_content = final_message
            if ws_image_data_urls:
                ws_persist_content = f'[图片消息]\n{final_message}' if final_message != '[包含图片的消息]' else '[图片消息]'
            if resend_payload:
                ws_persist_content = resend_payload['content']
            from chat_turn_guard import get_chat_turn_guard
            _turn_guard = get_chat_turn_guard()
            if _turn_guard.is_mutating(session_id or ''):
                await websocket.send_json({'error': '消息正在修改，请稍后再发送'})
                continue
            _ws_turn_claim = _turn_guard.claim(session_id=session_id or '', user_message_id=user_message_id or '', content=final_message, fallback_anchor=str((long_text_card or {}).get('id') or ''))
            if _ws_turn_claim is None:
                logger.info('WS duplicate generation skipped | session=%s message_id=%s platform=%s', session_id, user_message_id, ws_platform)
                continue
            _active_ws_turn_claim = _ws_turn_claim
            behavior_scheduler.set_status_callback(status_callback)
            from conversation_covenant import get_conversation_covenant_service
            await get_conversation_covenant_service().cancel_reservation(session_id)
            from long_text_cards import card_message_label
            _ws_accepted_user_text = final_message or (card_message_label(long_text_card) if long_text_card else '')
            await observe_accepted_message(_ws_accepted_user_text, session_id, 'WS')
            image_context_inject = None
            if image_analysis:
                image_context_inject = f'[系统注：用户发送了图片，视觉模型分析如下]：{image_analysis}'
            voice_prosody_summary = voice_prosody.get('summary', '')
            voice_context_inject = None
            if voice_prosody_summary:
                voice_context_inject = f'[系统注：用户发来一条语音。语气分析：{voice_prosody_summary}]'
            music_context_inject = None
            if session_id and session_id in _pending_music_analysis:
                music_summary, _ = _pending_music_analysis.pop(session_id)
                music_context_inject = f'[系统注：用户刚才分享了一首音乐。音乐结构分析：{music_summary}]'
            if not _ws_accepted_user_text:
                await websocket.send_json({'error': '消息内容为空'})
                continue
            set_latest_session_id(session_id)
            if TASK_SCHEDULER_AVAILABLE:
                get_global_task_manager().set_current_session_id(session_id)
            ws_private_last = get_last_private_chat_time()
            ws_private_away_sec = (datetime.now() - ws_private_last).total_seconds() if ws_private_last else None
            away_info = None
            _ws_activity_interrupt_text = ''
            if wander_manager and _ws_accepted_user_text:
                wander_manager.on_user_reply()
                await wander_manager.settle_runtime_interrupt()
                away_info = wander_manager.get_away_info()
                try:
                    _ws_activity_interrupt_text = wander_manager.get_activity_interrupt_text()
                except Exception:
                    _ws_activity_interrupt_text = ''
            if _ws_accepted_user_text:
                set_last_private_chat_time(datetime.now())
            if _ws_accepted_user_text and TASK_SCHEDULER_AVAILABLE:
                await get_global_task_manager().on_user_replied()
            from summary import Message as SummaryMessage
            ws_user_msg = SummaryMessage(id=user_message_id or f'user_{now_iso()}', role='user', content=_ws_accepted_user_text, timestamp=now_iso())
            ws_topic_result = None
            if ws_user_msg:
                ws_topic_status = await topic_state_machine.get_topic_status(session_id)
                ws_pre_start = ws_topic_status.get('start_time', '') if ws_topic_status else ''
                await topic_state_machine.maybe_end_for_day_change(session_id)
                ws_topic_result = await topic_state_machine.process_message(session_id, ws_user_msg, is_after_divider=False)
                _recently_active_sessions.add(session_id)
                if ws_topic_result.get('topic_ended'):
                    asyncio.create_task(_on_topic_ended(session_id, 'manual', ws_pre_start))
            await _persist_user_msg_early(session_id, user_message_id, ws_persist_content, images=ws_saved_image_meta if ws_saved_image_meta else None, voice_attachment=voice_attachment, long_text_card=long_text_card, user_event=None)
            conversation_history = []
            if session_id and get_context_scheduler() is not None:
                try:
                    is_first_today = _mark_diary_injected_today()
                    topic_status = await topic_state_machine.get_topic_status(session_id)
                    is_inactive = not topic_status.get('is_active', False) or topic_status.get('message_count', 0) == 0
                    global _first_topic_session_id
                    is_new_topic = is_inactive and is_first_today
                    if is_new_topic:
                        _first_topic_session_id = session_id
                    inject_ws_yesterday = True
                    from chat_context_prefetch import prefetch_chat_context_inputs
                    user_profile, ws_time_hint = await prefetch_chat_context_inputs(session_id or '', profile_loader=get_global_profile_manager().get_profile, time_hint_loader=_compute_time_awareness_hint, current_timestamp=now_iso(), timeout=5.0)
                    context_result = None
                    try:
                        if voice_prosody:
                            from voice_manager.voice_cochlea import set_current_prosody as _set_vp
                            _set_vp(voice_prosody)
                        context_result = await asyncio.wait_for(get_context_scheduler().schedule_context(user_message=final_message, persona=system_prompt or '', user_profile=user_profile, force_retrieve=False, session_id=session_id or '', is_new_topic=is_new_topic, inject_yesterday=inject_ws_yesterday, away_duration_seconds=ws_private_away_sec, away_reason=(away_info['away_reason'] if away_info else '') + _ws_activity_interrupt_text or None, night_mode=night_mode, phone_browse_mode=phone_browse_mode, music_cochlea=music_mode, time_awareness_hint=ws_time_hint, current_message_id=user_message_id, is_wander_active=False), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.warning('WebSocket 上下文调度超时，使用降级上下文')
                        if system_prompt:
                            conversation_history.append({'role': 'system', 'content': system_prompt})
                        raise
                    _ws_formatted = list(context_result.formatted_messages)
                    from context_builder.assembly import split_context_result_for_scheduler
                    system_prompt, conversation_history = split_context_result_for_scheduler(context_result, formatted_messages=_ws_formatted)
                    conversation_history = [msg for msg in conversation_history if msg.get('role') in {'system', 'user', 'assistant'} and (not is_ui_only_role(msg.get('role')))]
                    logger.info(f'WebSocket 上下文调度完成 | 记忆数: {len(context_result.memories)} | 上下文消息: {len(conversation_history)}')
                except Exception as e:
                    logger.warning(f'WebSocket 上下文调度失败，回退手动构建: {e}')
                    conversation_history = []
            if not conversation_history and session_id:
                try:
                    from datetime import datetime as _dt
                    from types import SimpleNamespace
                    unsummarized = []
                    topic_status = await topic_state_machine.get_topic_status(session_id)
                    topic_start_time = topic_status.get('start_time')
                    from event_chronicle import get_global_chronicle
                    from context_builder.conversation_history import fallback_history
                    chronicle = get_global_chronicle()
                    if topic_start_time:
                        from time_utils import filter_topic_history
                        source_rows = chronicle.get_messages_by_session_id(session_id, limit=10000, desc=True)
                        source_rows = filter_topic_history(source_rows, topic_start_time)
                    else:
                        active_day = await _get_active_date(session_id)
                        if not active_day:
                            active_day = chronicle.get_last_active_date() or _dt.now().strftime('%Y-%m-%d')
                        source_rows = [r for r in chronicle.get_messages_by_date(active_day) or [] if r.get('session_id') == session_id]
                    rows = [r for r in source_rows if not user_message_id or r.get('message_id') != user_message_id]
                    for item in fallback_history(rows, session_id):
                        conversation_history.append({key: item[key] for key in ('role', 'name', 'content') if key in item})
                    topic_status = await topic_state_machine.get_topic_status(session_id)
                    is_inactive = not topic_status.get('is_active', False) or topic_status.get('message_count', 0) == 0
                    if is_inactive and _diary_injected_date != datetime.now().strftime('%Y-%m-%d'):
                        _first_topic_session_id = session_id
                        _mark_diary_injected_today()
                        logger.info(f'WebSocket 回退路径注入新话题上下文')
                except Exception as e:
                    logger.warning(f'构建行为调度器对话历史失败: {e}')
            if not (system_prompt or '').strip() and _latest_persona_prompt:
                logger.warning('WebSocket: system_prompt 为空，回退到 _latest_persona_prompt')
                system_prompt = _latest_persona_prompt
            try:
                from wander_manager.user_status import check_wakeup
                if check_wakeup():
                    if WANDER_MANAGER_AVAILABLE and wander_manager:
                        wander_manager.set_user_status('idle', '', source='wakeup')
                    else:
                        from wander_manager.user_status import set_user_status_combined, UserStatus
                        set_user_status_combined(UserStatus.IDLE, '', source='wakeup')
                        try:
                            from health_tracker.tracker import get_health_tracker
                            get_health_tracker().on_status_change('sleeping', 'idle', '')
                        except Exception:
                            logger.exception('unexpected error in _ws_receiver')
                            pass
                user_msg_for_llm = final_message
                if long_text_card:
                    from long_text_cards import card_for_first_turn
                    _ws_card_text = card_for_first_turn(long_text_card)
                    user_msg_for_llm = f'{user_msg_for_llm}\n{_ws_card_text}' if user_msg_for_llm else _ws_card_text
                if image_context_inject:
                    user_msg_for_llm = user_msg_for_llm + '\n\n' + image_context_inject if user_msg_for_llm != '[包含图片的消息]' else image_context_inject
                if voice_context_inject:
                    user_msg_for_llm = user_msg_for_llm + '\n\n' + voice_context_inject if user_msg_for_llm != '[包含图片的消息]' else voice_context_inject
                if music_context_inject:
                    user_msg_for_llm = user_msg_for_llm + '\n\n' + music_context_inject if user_msg_for_llm != '[包含图片的消息]' else music_context_inject
                _sop_lock = get_session_op_lock(session_id)
                await _sop_lock.acquire()
                scheduler_message_id = user_message_id or getattr(ws_user_msg, 'id', '') or f'user_{now_iso()}'
                try:
                    result = await behavior_scheduler.process_message(user_message=user_msg_for_llm, conversation_history=conversation_history, system_prompt=system_prompt, night_mode_enabled=night_mode, platform=_request_platform.get(), session_id=session_id, images=ws_image_data_urls if ws_image_data_urls else None, message_id=scheduler_message_id, turn_id=scheduler_message_id, inspection_call_id=getattr(context_result, 'call_id', ''), raw_user_message=final_message)
                    try:
                        await status_callback('reply_finalizing', {'session_id': session_id, 'user_message_id': user_message_id})
                    except Exception:
                        pass
                    _is_immediate_checkup = await _emit_immediate_checkup_receipt(session_id, scheduler_message_id, final_message, result.tool_calls)
                    if _is_immediate_checkup:
                        result.ui_hide_sensory_tools = True
                    ws_active_date = await _get_active_date(session_id) if session_id else None
                    push_id = getattr(behavior_scheduler, '_last_push_message_id', '')
                    user_msg, assistant_msg = await _persist_chat_result(session_id=session_id, result=result, user_message_id=user_message_id, assistant_message_id=assistant_message_id, user_content=ws_persist_content, active_date=ws_active_date, voice_attachment=voice_attachment, images=ws_saved_image_meta if ws_saved_image_meta else None, push_message_id=push_id, user_event=None)
                    if assistant_msg and str(getattr(result, 'final_response', '') or '').strip():
                        try:
                            from context_builder import commit_context_result
                            commit_context_result(context_result, reply_persisted=True, assistant_message_id=str(getattr(assistant_msg, 'id', '') or ''))
                        except (NameError, Exception):
                            pass
                    if ws_image_data_urls and session_id and user_message_id:
                        _spawn_async_task(_fill_image_description_async(session_id, user_message_id, ws_persist_content, ws_image_data_urls, final_message, description=getattr(result, 'image_description', None)))
                    topic_ended = False
                    divider_index = -1
                    final_topic_status = None
                    from wander_manager.checkup_receipts import visible_chat_tool_calls
                    _visible_ws_tools = visible_chat_tool_calls(result.tool_calls, hide_sensory=bool(getattr(result, 'ui_hide_sensory_tools', False)))
                    if session_id:
                        ws_reasoning_content = None
                        if result.thinking_chain and len(result.thinking_chain) > 0:
                            ws_reasoning_content = result.thinking_chain[0].get('content', '') if isinstance(result.thinking_chain[0], dict) else getattr(result.thinking_chain[0], 'content', '')
                        _ws_tool_summary = None
                        _ws_tool_calls = None
                        if _visible_ws_tools:
                            _parts = []
                            for tc in _visible_ws_tools:
                                _name = tc.get('tool', '') or tc.get('name', '')
                                _desc = (tc.get('description', '') or '')[:60]
                                if _name:
                                    _parts.append(f'{_name}({_desc})' if _desc else _name)
                            if _parts:
                                _ws_tool_summary = ', '.join(_parts)
                            try:
                                _ws_tool_calls = json.dumps(_visible_ws_tools, ensure_ascii=False)
                            except Exception:
                                logger.exception('unexpected error in _ws_receiver')
                                pass
                        if assistant_msg:
                            assistant_topic_result = await topic_state_machine.process_message(session_id, assistant_msg, is_after_divider=False)
                            if assistant_topic_result.get('topic_ended'):
                                topic_ended = True
                                divider_index = assistant_topic_result.get('divider_index', -1)
                                ws_user_ended = ws_topic_result.get('topic_ended', False) if ws_topic_result else False
                                if not ws_user_ended:
                                    asyncio.create_task(_on_topic_ended(session_id, 'auto', ws_pre_start))
                            if not final_topic_status:
                                final_topic_status = assistant_topic_result.get('topic_status')
                        elif ws_topic_result:
                            final_topic_status = ws_topic_result.get('topic_status')
                    from chat_turn_outcome import failure_reason
                    if away_info and wander_manager and (not failure_reason(result)):
                        wander_manager.mark_away_delivered(cycle_id=away_info['cycle_id'])
                finally:
                    _sop_lock.release()
                leave_work(_focus_work)
                _focus_work = None
                game_chat_gate.leave_chat(_game_chat_work)
                _game_chat_work = None
                behavior_scheduler.clear_status_callback_if(status_callback)
                from chat_turn_outcome import finalize_chat_turn
                turn_status = finalize_chat_turn(_turn_guard, _ws_turn_claim, result, session_id=session_id, turn_id=scheduler_message_id, persisted=bool(assistant_msg))
                _active_ws_turn_claim = None
                try:
                    await websocket.send_json({'type': 'result', 'turn_status': turn_status, 'failure_reason': failure_reason(result), 'session_id': session_id, 'user_message_id': user_message_id, 'tool_calls': _visible_ws_tools, 'checkup_receipt_only': bool(getattr(result, 'ui_hide_sensory_tools', False)), 'need_user_input': result.need_user_input, 'thinking_chain': result.thinking_chain, 'pending_intent_check': getattr(result, 'pending_intent_check', False), 'topic_status': final_topic_status, 'topic_ended': topic_ended, 'divider_index': divider_index})
                except Exception:
                    logger.warning('WS cannot send final result (WS closed), frontend will reload session')
            except Exception:
                _turn_guard.fail(_ws_turn_claim)
                _active_ws_turn_claim = None
                raise
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        logger.info('WebSocket disconnected')
    except Exception as e:
        import traceback
        logger.error(f'WebSocket error: {e}')
        traceback.print_exc()
        try:
            await websocket.send_json({'error': str(e)})
        except Exception:
            logger.exception('unexpected error in _ws_receiver')
            pass
    finally:
        if _active_ws_turn_claim:
            try:
                from chat_turn_guard import get_chat_turn_guard
                get_chat_turn_guard().fail(_active_ws_turn_claim)
            except Exception:
                logger.exception('failed to release interrupted chat turn claim')
        leave_work(_focus_work)
        from wander_manager import game_chat_gate
        game_chat_gate.leave_chat(_game_chat_work)
        if _receiver_task and (not _receiver_task.done()):
            _receiver_task.cancel()
            try:
                await asyncio.wait_for(_receiver_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                logger.debug('cleanup: _ws_receiver: (asyncio.TimeoutError, asyncio.CancelledError, Exception)')
                pass
        behavior_scheduler.clear_status_callback_if(status_callback)
        if ws_platform == 'mobile':
            relay_hub.unregister(websocket, 'behavior_ws_exit')
            _unbind_mobile_relay_if_empty(relay_hub)
            set_phone_browse_paused(False)

@app.post('/api/voice/stt')
async def voice_stt_endpoint(file: UploadFile=File(...)):
    """语音 STT — REST 风格（AionsHome 同款），POST FormData WAV → 三路并行分析 → JSON 结果"""
    import hashlib as _hashlib
    audio_bytes = await file.read()
    voice_hash = _hashlib.md5(audio_bytes).hexdigest()[:12]
    user_voice_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'voice_manager', 'output', 'user')
    _os.makedirs(user_voice_dir, exist_ok=True)
    voice_filepath = _os.path.join(user_voice_dir, f'{voice_hash}.wav')
    with open(voice_filepath, 'wb') as f:
        f.write(audio_bytes)
    from voice_manager.stt_engine import transcribe as stt_transcribe
    from voice_manager.auditory_cortex import analyze_prosody
    from voice_manager.voice_cochlea import run_cochlea_sync
    import concurrent.futures
    loop = asyncio.get_running_loop()
    stt_task = loop.run_in_executor(None, stt_transcribe, audio_bytes)
    prosody_task = loop.run_in_executor(None, analyze_prosody, audio_bytes)
    cochlea_task = loop.run_in_executor(None, run_cochlea_sync, audio_bytes, voice_hash)
    text, prosody, cochlea = await asyncio.gather(stt_task, prosody_task, cochlea_task)
    from voice_manager.voice_cochlea import classify_tone
    tone_label = classify_tone(prosody or {}) if prosody else '平静'
    return {'text': (text or '').strip(), 'voice_path': f'/voice/user/{voice_hash}.wav', 'voice_duration': prosody.get('duration_sec', 0) if prosody else cochlea.get('duration_sec', 0), 'tone_label': tone_label, 'prosody': prosody or {}, 'cochlea': {'ok': cochlea.get('ok'), 'spectrogram_path': cochlea.get('spectrogram_path', ''), 'summary_json': cochlea.get('summary_json', {}), 'pitch_contour': cochlea.get('pitch_contour', []), 'duration_sec': cochlea.get('duration_sec', 0)} if cochlea else {}}

@app.websocket('/ws/voice')
async def websocket_voice_stt(websocket: WebSocket):
    """语音识别 WebSocket — 接收前端录音 base64 → faster-whisper STT → 返回文本"""
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else None
    if not _NETWORK_ACCESS.allows(client_ip):
        await websocket.send_json({'error': 'Access denied'})
        await websocket.close()
        return
    logger.info('语音STT WebSocket客户端已连接')
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            import json as _json
            try:
                payload = _json.loads(raw)
            except Exception:
                continue
            msg_type = payload.get('type', '')
            if msg_type == 'ping':
                await websocket.send_text('{"type":"pong"}')
                continue
            if msg_type == 'stt':
                audio_b64 = payload.get('audio', '')
                if not audio_b64:
                    await websocket.send_text('{"type":"stt_error","message":"音频数据为空"}')
                    continue
                import base64 as _b64
                import tempfile
                import os as _os
                try:
                    audio_bytes = _b64.b64decode(audio_b64)
                except Exception:
                    await websocket.send_text('{"type":"stt_error","message":"音频解码失败"}')
                    continue
                try:
                    from voice_manager.stt_engine import transcribe as stt_transcribe
                    from voice_manager.auditory_cortex import analyze_prosody
                    import concurrent.futures
                    import hashlib as _hashlib
                    user_voice_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'voice_manager', 'output', 'user')
                    _os.makedirs(user_voice_dir, exist_ok=True)
                    voice_hash = _hashlib.md5(audio_bytes).hexdigest()[:12]
                    voice_filename = f'{voice_hash}.wav'
                    voice_filepath = _os.path.join(user_voice_dir, voice_filename)
                    with open(voice_filepath, 'wb') as f:
                        f.write(audio_bytes)
                    from voice_manager.voice_cochlea import run_cochlea
                    loop = asyncio.get_running_loop()
                    stt_task = loop.run_in_executor(None, stt_transcribe, audio_bytes)
                    prosody_task = loop.run_in_executor(None, analyze_prosody, audio_bytes)
                    cochlea_task = run_cochlea(audio_bytes, voice_hash)
                    text, prosody, cochlea = await asyncio.gather(stt_task, prosody_task, cochlea_task)
                    result_text = (text or '').strip()
                    voice_duration = prosody.get('duration_sec', 0) if prosody else 0
                    response = {'type': 'stt_result', 'text': result_text, 'voice_path': f'/voice/user/{voice_filename}', 'voice_duration': voice_duration, 'prosody': prosody or {}, 'cochlea': {'ok': cochlea.get('ok'), 'spectrogram_b64': cochlea.get('spectrogram_b64', ''), 'spectrogram_path': cochlea.get('spectrogram_path', ''), 'summary_json': cochlea.get('summary_json', {}), 'pitch_contour': cochlea.get('pitch_contour', []), 'duration_sec': cochlea.get('duration_sec', 0)} if cochlea else {}}
                    await websocket.send_text(_json.dumps(response, ensure_ascii=False))
                    logger.info(f"STT 完成: {result_text[:50]}... | prosody: {(prosody.get('summary', 'N/A') if prosody else 'N/A')}")
                except Exception as e:
                    logger.error(f'STT 识别失败: {e}')
                    await websocket.send_text(_json.dumps({'type': 'stt_error', 'message': f'识别失败: {str(e)}'}, ensure_ascii=False))
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        logger.debug('cleanup: websocket_voice_stt: WebSocketDisconnect')
        pass
    except Exception as e:
        logger.error(f'语音STT WebSocket异常: {e}')
    finally:
        logger.info('语音STT WebSocket客户端已断开')

@app.websocket('/ws/music')
async def websocket_music_push(websocket: WebSocket):
    """音乐播放状态推送WebSocket（持久连接，用于推送播放中断等事件）"""
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else None
    if not _NETWORK_ACCESS.allows(client_ip):
        await websocket.send_json({'error': 'Access denied'})
        await websocket.close()
        return
    monitor = _music_monitor_mod.music_monitor
    if monitor:
        monitor.add_connection(websocket)
    logger.info(f'音乐WebSocket客户端已连接')
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == 'ping':
                    await websocket.send_text('pong')
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text('ping')
                except Exception:
                    break
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        logger.debug('cleanup: websocket_music_push: WebSocketDisconnect')
        pass
    except Exception as e:
        logger.error(f'音乐WebSocket异常: {e}')
    finally:
        if monitor:
            monitor.remove_connection(websocket)
        logger.info(f'音乐WebSocket客户端已断开')

@app.websocket('/ws/wander')
async def websocket_wander_push(websocket: WebSocket):
    """漫想主动消息推送WebSocket（持久连接，用于向前端推送漫想消息）
    2026-06-03: 同时用作手机工具中继通道（解决 behavior WS 仅聊天时存在的问题）"""
    ws_platform = websocket.query_params.get('platform', 'pc')
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else None
    if not _NETWORK_ACCESS.allows(client_ip):
        await websocket.send_json({'error': 'Access denied'})
        await websocket.close()
        return
    wander_connections.add(websocket)
    logger.info(f'漫想WebSocket客户端已连接 (platform={ws_platform})，当前连接数: {len(wander_connections)}')
    relay_hub = get_mobile_relay_hub() if ws_platform == 'mobile' else None
    relay_entry = None
    if ws_platform == 'mobile':
        requested_kind = websocket.query_params.get('client_kind', 'webview')
        if requested_kind not in {'native_service', 'webview'}:
            requested_kind = 'webview'
        relay_entry = relay_hub.register(websocket, client_kind=requested_kind, capabilities=())
    if ws_platform == 'mobile':
        _bind_mobile_relay(relay_hub)
        _spawn_async_task(_flush_mobile_delivery(relay_hub.send_tool))
        logger.info('[WANDER_RELAY] 手机中继已激活（wander WS 直连）')
        if TASK_SCHEDULER_AVAILABLE:
            try:
                from task_scheduler import get_global_task_manager as _get_task_manager
                _spawn_async_task(_get_task_manager().flush_pending_alarm_cancels())
            except Exception as _e:
                logger.debug(f'[CHECKUP] 闹钟撤销补发启动失败: {_e}')
        if SENTINEL_AVAILABLE:
            try:
                s = get_sentinel()
                if s and s._running:
                    asyncio.create_task(s._tick(force=True))
                    logger.info('[WANDER_RELAY] 手机重连 → 触发哨兵 force tick')
            except Exception:
                pass
    if wander_manager:
        try:
            status = wander_manager.get_status()
            await websocket.send_json({'type': 'wander_status', 'status': status})
        except Exception as e:
            logger.error(f'漫想WebSocket推送初始状态失败: {e}')
            wander_connections.discard(websocket)
            if ws_platform == 'mobile':
                relay_hub.unregister(websocket, 'initial_status_failed')
                _unbind_mobile_relay_if_empty(relay_hub)
            return
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == 'ping':
                    if ws_platform == 'mobile':
                        relay_hub.touch(websocket, heartbeat=True)
                    await websocket.send_text('pong')
                elif data == 'pong':
                    if ws_platform == 'mobile':
                        relay_hub.touch(websocket, heartbeat=True)
                elif data.startswith('{'):
                    try:
                        import json as _wjson
                        msg = _wjson.loads(data)
                        if ws_platform == 'mobile':
                            msg_type = msg.get('type', '')
                            if msg_type == 'relay_hello':
                                relay_entry = relay_hub.update_hello(websocket, client_kind=msg.get('client_kind'), capabilities=msg.get('capabilities') or ())
                                if relay_entry is not None:
                                    await websocket.send_json({'type': 'relay_hello_ack', 'connection_id': relay_entry.connection_id, 'server_time': time.time()})
                                    from ambient_listening import get_ambient_listening_service
                                    await websocket.send_json(get_ambient_listening_service().mobile_config())
                            elif msg_type == 'relay_heartbeat':
                                relay_hub.touch(websocket, heartbeat=True)
                                await websocket.send_json({'type': 'relay_heartbeat_ack', 'server_time': time.time()})
                                from ambient_listening import get_ambient_listening_service
                                await websocket.send_json(get_ambient_listening_service().mobile_config())
                            else:
                                relay_hub.touch(websocket, tool_result=msg_type == 'mobile_tool_result')
                        if msg.get('type') == 'now_playing':
                            pass
                        elif msg.get('type') == 'wifi_state_change':
                            try:
                                from silicon_perception.collection.wifi import handle_wifi_relay_event
                                handle_wifi_relay_event(msg)
                            except Exception as e:
                                logger.debug(f'wifi_state_change 处理异常: {e}')
                        elif msg.get('type') == 'notification_state_change':
                            from silicon_perception.collection.notifications import handle_notification_relay_event
                            handle_notification_relay_event(msg)
                        elif msg.get('type') == 'mobile_tool_result':
                            rid = msg.get('request_id', '')
                            handled = relay_hub.resolve_tool_result(rid, msg.get('result', {}), top_success=msg.get('success'), error=msg.get('error', '')) if relay_hub is not None else False
                            if not handled and behavior_scheduler:
                                behavior_scheduler.handle_mobile_tool_result(rid, msg.get('success', False), msg.get('result', {}), msg.get('error', ''))
                        elif msg.get('type') == 'checkup_alarm_event':
                            from task_scheduler.mobile_event_bridge import handle_checkup_alarm_event
                            _spawn_async_task(handle_checkup_alarm_event(msg, websocket))
                        elif msg.get('type') == 'ambient_audio_segment':
                            from ambient_listening import get_ambient_listening_service
                            ack = await get_ambient_listening_service().receive_segment(msg)
                            await websocket.send_json(ack)
                        elif msg.get('type') == 'ambient_checkpoint':
                            from ambient_listening import get_ambient_listening_service
                            get_ambient_listening_service().record_mobile_checkpoint(msg)
                        raw = msg.get('result', {}) or {}
                        if isinstance(raw, dict) and raw.get('event') == 'phone_activity_report':
                            try:
                                from wander_manager.user_status import should_wake_from_sleeping
                                if should_wake_from_sleeping('phone_activity'):
                                    logger.info('[phone_activity] 唤醒触发: sleeping → idle')
                                    if WANDER_MANAGER_AVAILABLE and wander_manager:
                                        wander_manager.set_user_status('idle', '', source='wakeup')
                            except Exception as e:
                                logger.debug(f'phone_activity_report 处理异常: {e}')
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text('ping')
                except Exception:
                    break
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        logger.debug('cleanup: _wander_mobile_sender: WebSocketDisconnect')
        pass
    except Exception as e:
        logger.error(f'漫想WebSocket异常: {e}')
    finally:
        wander_connections.discard(websocket)
        if ws_platform == 'mobile':
            relay_hub.unregister(websocket, 'ws_handler_exit')
            still_connected = relay_hub.has_live_connection()
            _unbind_mobile_relay_if_empty(relay_hub)
            if still_connected:
                logger.info('[WANDER_RELAY] wander WS断开但仍有其他mobile连接，动态切换中')
            else:
                logger.info('[WANDER_RELAY] 手机工具中继已断开（无其他mobile连接）')

def _save_chat_images_from_base64(images_data: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """将 base64 图片数据保存到 static/chat_images/，返回图片元数据列表。

    Args:
        images_data: [{"filename": "...", "type": "image/...", "data": "data:image/...;base64,..."}]
    Returns:
        [{"id": "...", "url": "/static/chat_images/...", "filename": "...", "size": N, "type": "..."}]
    """
    import uuid, base64 as _b64, re as _re, time as _time
    results = []
    for img in images_data:
        try:
            img_id = str(uuid.uuid4())[:12]
            filename = img.get('filename', 'image.png')
            mime_type = img.get('type', 'image/png')
            data_url = img.get('data', '')
            b64_data = data_url
            if ',' in data_url:
                b64_data = data_url.split(',', 1)[1]
            raw = _b64.b64decode(b64_data)
            ext = os.path.splitext(filename)[1] or '.png'
            safe_name = f'{int(_time.time())}_{img_id}{ext}'
            filepath = os.path.join(CHAT_IMAGE_DIR, safe_name)
            with open(filepath, 'wb') as f:
                f.write(raw)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                results.append({'id': img_id, 'url': f'/static/chat_images/{safe_name}', 'filename': filename, 'size': len(raw), 'type': mime_type})
                print(f'[IMAGE] ✅ 已保存: {safe_name} ({len(raw)} bytes)', flush=True)
            else:
                print(f'[IMAGE] ❌ 写入后文件不存在或为空: {filepath}', flush=True)
        except Exception as e:
            print(f"[IMAGE] ❌ 保存图片失败 ({img.get('filename', '?')}): {e}", flush=True)
    return results

async def _save_chat_images_from_upload(images: list) -> List[Dict[str, Any]]:
    """将 UploadFile 图片保存到 static/chat_images/，返回图片元数据列表。

    Args:
        images: List[UploadFile]
    Returns:
        [{"id": "...", "url": "/static/chat_images/...", "filename": "...", "size": N, "type": "..."}]
    """
    import uuid
    results = []
    for img in images:
        try:
            img_id = str(uuid.uuid4())[:12]
            filename = getattr(img, 'filename', 'image.png') or 'image.png'
            mime_type = getattr(img, 'content_type', 'image/png') or 'image/png'
            await img.seek(0)
            content = await img.read()
            if not content:
                print(f'[IMAGE] 警告：图片 {filename} 内容为空（可能已被消费且不支持 seek）', flush=True)
                continue
            ext = os.path.splitext(filename)[1] or '.png'
            import time as _img_time
            safe_name = f'{int(_img_time.time())}_{img_id}{ext}'
            filepath = os.path.join(CHAT_IMAGE_DIR, safe_name)
            with open(filepath, 'wb') as f:
                f.write(content)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                results.append({'id': img_id, 'url': f'/static/chat_images/{safe_name}', 'filename': filename, 'size': len(content), 'type': mime_type})
                print(f'[IMAGE] ✅ 已保存: {safe_name} ({len(content)} bytes)', flush=True)
            else:
                print(f'[IMAGE] ❌ 写入后文件不存在或为空: {filepath}', flush=True)
        except Exception as e:
            print(f"[IMAGE] ❌ 保存上传图片失败 ({getattr(img, 'filename', '?')}): {e}", flush=True)
    return results

def _cleanup_voice_file(voice_path: str):
    """清理语音文件，文件不存在时不报错。"""
    try:
        if voice_path.startswith('/voice/'):
            relative = voice_path.lstrip('/')
            abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)
            if os.path.exists(abs_path):
                os.remove(abs_path)
                print(f'[VOICE CLEANUP] 已删除: {abs_path}', flush=True)
    except FileNotFoundError:
        logger.debug('file not found in _cleanup_voice_file')
        pass
    except Exception as e:
        print(f'[VOICE CLEANUP] 删除失败 {voice_path}: {e}', flush=True)

def _sanitize_user_message(text: str, image_analysis: Optional[str]) -> str:
    """防御性清洗：确保 image_analysis 不会残留在用户消息正文中。

    当前代码已将 image_analysis 与 final_content_for_memory 分离（image_analysis
    旧摘要只用于兼容清洗；现役主动图片由统一视觉轮分析，不污染用户消息正文）。
    此函数是安全网：防止前端回传脏数据或未来代码回归导致泄漏。
    """
    if not image_analysis or not text:
        return text
    for marker in ('[系统注：', '图片描述：', '视觉模型分析'):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx].rstrip()
    return text
_IMAGE_STORAGE_DESC_PROMPT = '这是用户（人类伙伴）在同一条消息里发来的一张或多张图片，以及她配上的文字。请你根据文字内容，对图片进行客观描述：图片里有哪些信息和元素？根据人类伙伴发来的文字内容，有哪些信息是值得重点关注的？如果有多张图片，按图片1、图片2依次对应说明，不要混在一起。用几句话精简描述重点即可，不要用列表或过多细节。'

async def _describe_image_for_storage(image_data_urls: Any, user_text: str) -> str:
    """用 GLM 为一批图片生成一个客观存储描述。失败返回空串。

    异步任务，超时放宽（GLM 读超时 300s），不阻塞消息生成。
    """
    try:
        urls = [image_data_urls] if isinstance(image_data_urls, str) else list(image_data_urls or [])
        urls = [url for url in urls if isinstance(url, str) and url]
        if not urls:
            return ''
        import httpx
        from llm_client import call_llm_api_visual, http_client, set_http_client
        _old = http_client
        _tmp = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        set_http_client(_tmp)
        try:
            image_parts = [{'type': 'image_url', 'image_url': {'url': url}} for url in urls]
            messages = [{'role': 'user', 'content': [*image_parts, {'type': 'text', 'text': f"{_IMAGE_STORAGE_DESC_PROMPT}\n用户配文：{user_text or '（无）'}"}]}]
            result = await call_llm_api_visual(messages, 'glm')
        finally:
            set_http_client(_old)
            await _tmp.aclose()
        content = (result.get('content') or '').strip() if isinstance(result, dict) else str(result or '').strip()
        return content
    except Exception as e:
        logger.warning(f'[图片描述] 生成失败（保持占位）: {e}')
        return ''

async def _fill_image_description_async(session_id: str, message_id: str, base_content: str, image_data_urls: Any, user_text: str, description: Optional[str]=None):
    """补充 [图片消息] 描述到已存消息。

    正常视觉轮直接复用 Flash envelope；没有可用字段的旧/降级路径才调用
    GLM。失败保持占位。
    """
    try:
        desc = str(description or '').strip()
        if not desc:
            desc = await _describe_image_for_storage(image_data_urls, user_text)
        if not desc:
            return
        desc = ' '.join(desc.split())
        from image_message_content import with_image_description
        new_content = with_image_description(base_content, desc)
        try:
            smgr = get_global_session_manager()
            if smgr:
                session = await smgr.get_session(session_id)
                if session:
                    updated = []
                    for m in getattr(session, 'messages', []) or []:
                        mid = getattr(m, 'id', '') if hasattr(m, 'id') else m.get('id', '') if isinstance(m, dict) else ''
                        if mid == message_id:
                            if hasattr(m, 'model_dump'):
                                d = m.model_dump()
                                d['content'] = new_content
                                updated.append(d)
                            elif isinstance(m, dict):
                                m['content'] = new_content
                                updated.append(m)
                            else:
                                updated.append(m)
                        else:
                            updated.append(m.model_dump() if hasattr(m, 'model_dump') else m)
                    await smgr.update_session(session_id, {'messages': updated}, merge=False)
        except Exception as e:
            logger.warning(f'[图片描述] session 更新失败: {e}')
        try:
            from summary import conversation_manager
            await conversation_manager.edit_message(session_id, message_id, new_content)
        except Exception as e:
            logger.warning(f'[图片描述] conversation 更新失败: {e}')
        logger.debug(f'[图片描述] 已补充到消息 {message_id}: {desc[:60]}')
    except Exception as e:
        logger.warning(f'[图片描述] 补充失败（保持占位）: {e}')

async def _persist_user_msg_early(session_id: str, user_message_id: str, user_content: str, images: List[Dict[str, Any]]=None, voice_attachment: Dict[str, Any]=None, long_text_card: Dict[str, str]=None, user_event: Dict[str, Any]=None):
    """提前持久化用户消息到 session_manager（LLM 调用之前）。
    确保即使 LLM 超时/WebSocket 断开，用户消息也不会丢失。"""
    import json as _json
    if not session_id or not user_message_id:
        return
    try:
        smgr = get_global_session_manager()
        if not smgr:
            return
        messages = []
        message_added = False
        if long_text_card:
            session = await smgr.get_session(session_id)
            if not session:
                return
            messages = list(getattr(session, 'messages', []) or [])
            existing_ids = {getattr(m, 'id', '') if hasattr(m, 'id') else m.get('id', '') for m in messages}
            if user_message_id not in existing_ids:
                if long_text_card.get('kind') == 'music':
                    try:
                        from music_system.catalog import record_user_card
                        enriched_card = record_user_card(long_text_card, user_message_id)
                        long_text_card.clear()
                        long_text_card.update(enriched_card)
                    except Exception as exc:
                        logger.warning('music share catalogue update failed: %s', type(exc).__name__)
                msg_dict = {'id': user_message_id, 'role': 'user', 'content': user_content, 'timestamp': now_iso(), 'longTextCard': long_text_card}
                if images:
                    msg_dict['images'] = images
                if voice_attachment:
                    msg_dict['voiceAttachment'] = voice_attachment
                messages.append(msg_dict)
                await smgr.update_session(session_id, {'messages': messages})
                message_added = True
        else:
            msg_dict = {'id': user_message_id, 'role': 'user', 'content': user_content, 'timestamp': now_iso()}
            if images:
                msg_dict['images'] = images
            if voice_attachment:
                msg_dict['voiceAttachment'] = voice_attachment
            if user_event:
                msg_dict['eventType'] = user_event['event_type']
                msg_dict['toolCalls'] = user_event['tool_calls']
            append_message = getattr(smgr, 'append_message', None)
            if append_message:
                message_added = await append_message(session_id, msg_dict)
            else:
                session = await smgr.get_session(session_id)
                if not session:
                    return
                messages = list(getattr(session, 'messages', []) or [])
                existing_ids = {getattr(m, 'id', '') if hasattr(m, 'id') else m.get('id', '') for m in messages}
                if user_message_id not in existing_ids:
                    messages.append(msg_dict)
                    await smgr.update_session(session_id, {'messages': messages})
                    message_added = True
        if message_added:
            if images or user_event:
                await smgr.flush_session(session_id)
        elif images:
            if not messages:
                session = await smgr.get_session(session_id)
                messages = list(getattr(session, 'messages', []) or []) if session else []
            updated = False
            for m in messages:
                mid = m.get('id', '') if isinstance(m, dict) else getattr(m, 'id', '')
                if mid == user_message_id:
                    if isinstance(m, dict) and (not m.get('images')):
                        m['images'] = images
                        updated = True
                    break
            if updated:
                await smgr.update_session(session_id, {'messages': messages})
                await smgr.flush_session(session_id)
    except Exception as e:
        logger.error(f'用户消息提前写入失败: {e}', exc_info=True)

async def _persist_chat_result(session_id: str, result, user_message_id: str, assistant_message_id: str, user_content: str, *, active_date: str=None, voice_attachment: Dict[str, Any]=None, images: List[Dict[str, Any]]=None, push_message_id: str='', user_event: Dict[str, Any]=None):
    """统一持久化聊天结果到三层存储 (session_manager + conversation_manager V2 + SQLite)。

    处理: 用户消息(已提前写) + 中间轮次消息 + 最终助手消息 + toolCalls/voice 剥离 + thinkingChain
    所有时间戳使用 UTC。
    返回: (user_msg, assistant_msg) 元组 (供 topic_state_machine 使用)，失败返回 (None, None)。
    """

    def _safe_json_dumps(obj):
        """安全 JSON 序列化，失败返回 None（不抛异常中断持久化）。"""
        try:
            import json as _json
            return _json.dumps(obj, ensure_ascii=False)
        except Exception:
            return None
    from summary import conversation_manager
    if not session_id:
        return (None, None)
    from affect.sidecar import parse_affect_sidecar
    from visible_reply import clean_visible_reply
    intermediate_messages = getattr(result, 'intermediate_messages', []) or []
    _safe_intermediate_messages = []
    for _im in intermediate_messages:
        if not isinstance(_im, dict):
            continue
        _im_copy = dict(_im)
        _im_copy['content'] = clean_visible_reply(parse_affect_sidecar(_im_copy.get('content', '')).clean_content)
        _safe_intermediate_messages.append(_im_copy)
    intermediate_messages = _safe_intermediate_messages
    _seen_im = set()
    _deduped = []
    for im in intermediate_messages:
        c = im.get('content', '')
        if c and c not in _seen_im:
            _seen_im.add(c)
            _deduped.append(im)
    intermediate_messages = _deduped
    raw_tool_calls = getattr(result, 'tool_calls', []) or []
    from wander_manager.checkup_receipts import visible_chat_tool_calls
    tool_calls = visible_chat_tool_calls(raw_tool_calls, hide_sensory=bool(getattr(result, 'ui_hide_sensory_tools', False)))
    _rift_event_type = ''
    try:
        _rift_tool_used = any((str(tc.get('tool', '') or tc.get('name', '')).startswith('rift_') for tc in tool_calls if isinstance(tc, dict)))
        from rift_archive import get_rift_archive_service
        _rift_related = get_rift_archive_service().should_inject(user_content)
        if _rift_tool_used or _rift_related:
            _rift_event_type = 'rift_game'
    except Exception:
        pass
    from miband.notices import detach_receipts
    try:
        tool_calls = await detach_receipts(session_id, user_message_id or assistant_message_id, tool_calls, _emit_ui_notification)
        result.tool_calls = tool_calls
    except Exception:
        logger.warning('Independent band receipt not confirmed; retaining original attachment')
    _cleaned_tool_calls = strip_voice_base64(tool_calls)
    import json as _json
    tool_calls_json = _json.dumps(_cleaned_tool_calls, ensure_ascii=False) if _cleaned_tool_calls else None
    from tool_call_utils import build_voice_marker, apply_voice_marker
    _voice_marker = build_voice_marker(raw_tool_calls)
    reasoning = None
    if getattr(result, 'thinking_chain', None):
        reasoning = (result.thinking_chain[0].get('content', '') if isinstance(result.thinking_chain[0], dict) else getattr(result.thinking_chain[0], 'content', '')) if result.thinking_chain else None
    _final_sidecar_parse = parse_affect_sidecar(getattr(result, 'final_response', '') or '')
    final_response = clean_visible_reply(_final_sidecar_parse.clean_content)
    from chat_turn_outcome import needs_tool_receipt
    _tool_receipt_only = needs_tool_receipt(result, final_response, intermediate_messages, _cleaned_tool_calls)
    _has_final = bool(final_response and final_response.strip())
    if tool_calls or not _has_final:
        print(f"[PERSIST-DIAG] tool_calls={([t.get('tool') for t in tool_calls] if tool_calls else '空')} final_len={len(final_response or '')} _has_final={_has_final} im_count={len(intermediate_messages)}", flush=True)
    user_msg = None
    assistant_msg = None
    try:
        if active_date is None:
            active_date = await _get_active_date(session_id)
        user_msg = await conversation_manager.add_message(session_id, 'user', user_content, user_message_id, is_wander=False, is_sentinel=False, is_reminder=False, active_date=active_date, event_type=user_event['event_type'] if user_event else _rift_event_type, tool_calls=_safe_json_dumps(user_event['tool_calls']) if user_event else None, images=_safe_json_dumps(images) if images else None, voice_attachment=_safe_json_dumps(voice_attachment) if voice_attachment else None)
        if user_event:
            from event_chronicle import get_global_chronicle
            stored_user = get_global_chronicle().get_message_by_msg_id(user_message_id) or {}
            await conversation_manager.v2_manager.upsert_message(session_id, 'user', user_content, user_message_id, is_wander=False, is_sentinel=False, is_reminder=False, active_date=active_date, event_type=user_event['event_type'], tool_calls=_safe_json_dumps(user_event['tool_calls']), images=_safe_json_dumps(images) if images else None, voice_attachment=_safe_json_dumps(voice_attachment) if voice_attachment else None, timestamp_override=stored_user.get('timestamp'), require_persistence=True)
        if _rift_event_type and (not user_event):
            await conversation_manager.v2_manager.upsert_message(session_id, 'user', user_content, user_message_id, is_wander=False, is_sentinel=False, is_reminder=False, active_date=active_date, event_type=_rift_event_type, images=_safe_json_dumps(images) if images else None, voice_attachment=_safe_json_dumps(voice_attachment) if voice_attachment else None)
        _user_committed = False
        _tool_summary = None
        if tool_calls:
            _parts = []
            for tc in tool_calls:
                _name = tc.get('tool', '') or tc.get('name', '')
                _desc = (tc.get('description', '') or '')[:60]
                if _name:
                    _parts.append(f'{_name}({_desc})' if _desc else _name)
            if _parts:
                _tool_summary = ', '.join(_parts)
        _has_final = bool(final_response and final_response.strip()) or _tool_receipt_only
        _n_im = len(intermediate_messages)
        for i, im in enumerate(intermediate_messages):
            im_id = im.get('id') or f"msg_{session_id}_{im.get('timestamp', '')}"
            _im_thinking = im.get('thinking')
            _im_thinking_chain = _safe_json_dumps([{'content': _im_thinking, 'timestamp': im.get('timestamp', now_iso())}]) if _im_thinking else None
            _im_attach = not _has_final and i == _n_im - 1 and bool(tool_calls)
            _saved_im = await conversation_manager.add_message(session_id, 'assistant', apply_voice_marker(im['content'], _voice_marker) if _im_attach else im['content'], im_id, reasoning_content=_im_thinking, is_wander=False, is_sentinel=False, is_reminder=False, tool_summary=_tool_summary if _im_attach else None, tool_calls=tool_calls_json if _im_attach else None, active_date=active_date, event_type=_rift_event_type, thinking_chain_json=_im_thinking_chain, timestamp_override=im.get('timestamp'))
            if _saved_im is not None:
                from api_sources.message_sources import save as save_message_source
                await asyncio.to_thread(save_message_source, session_id, im_id, getattr(result, 'message_sources', {}).get(im_id))
        if _has_final:
            _thinking_chain_json_val = _safe_json_dumps(result.thinking_chain) if getattr(result, 'thinking_chain', None) else None
            assistant_msg = await conversation_manager.add_message(session_id, 'assistant', apply_voice_marker(_strip_wander_prefix(final_response), _voice_marker), push_message_id or assistant_message_id, reasoning_content=reasoning, is_wander=False, is_sentinel=False, is_reminder=False, tool_summary=_tool_summary, tool_calls=tool_calls_json, active_date=active_date, event_type=_rift_event_type, thinking_chain_json=_thinking_chain_json_val, require_persistence=True)
            if assistant_msg is not None:
                from api_sources.message_sources import save as save_message_source
                await asyncio.to_thread(save_message_source, session_id, push_message_id or assistant_message_id, getattr(result, 'model_source', None))
            try:
                from affect import get_affect_service
                _affect_service = get_affect_service()
                _assistant_id = str(push_message_id or assistant_message_id or getattr(assistant_msg, 'id', '') or (assistant_msg.get('id', '') if isinstance(assistant_msg, dict) else ''))
                _user_id = str(user_message_id or '')
                if _assistant_id and _user_id and (assistant_msg is not None) and (not _tool_receipt_only) and _user_committed and (not getattr(result, 'failure_reason', '')):
                    try:
                        _affect_service.observe_assistant_message_v2(content=final_response, source_id=_assistant_id, session_id=session_id, occurred_at=ts if 'ts' in locals() else now_iso())
                    except Exception:
                        logger.debug('affect assistant episode observation skipped', exc_info=True)
                    _sidecars = list(getattr(result, 'affect_sidecars', []) or [])
                    from behavior_scheduler.affect_capture import has_tool_protocol
                    if _final_sidecar_parse.accepted and (not has_tool_protocol(_final_sidecar_parse.clean_content)):
                        _sidecars.append({'payload': _final_sidecar_parse.payload, 'valid': True, 'origin': 'final'})
                    _final_payloads = []
                    for _entry in _sidecars:
                        if not isinstance(_entry, dict) or _entry.get('origin') != 'final':
                            continue
                        if _entry.get('eligible') is False or not _entry.get('valid') or (not isinstance(_entry.get('payload'), dict)):
                            continue
                        _final_payloads.append(_entry['payload'])
                    seen_payloads = set()
                    for _payload in _final_payloads:
                        _key = _json.dumps(_payload, ensure_ascii=False, sort_keys=True)
                        if _key in seen_payloads:
                            continue
                        seen_payloads.add(_key)
                        _affect_service.commit_bound_sidecar(payload=_payload, user_message_id=_user_id, assistant_message_id=_assistant_id, session_id=session_id)
            except Exception:
                logger.warning('affect final sidecar commit skipped', exc_info=True)
    except Exception as e:
        print(f'[PERSIST] conversation_manager 写入失败: {e}', flush=True)
    try:
        smgr = get_global_session_manager()
        if not smgr:
            return (user_msg, assistant_msg)
        session = await smgr.get_session(session_id)
        if not session:
            return (user_msg, assistant_msg)
        messages = list(getattr(session, 'messages', []) or [])
        existing_ids = {getattr(m, 'id', '') if hasattr(m, 'id') else m.get('id', '') for m in messages}
        ts = now_iso()
        if user_event and user_message_id in existing_ids:
            messages = [{**m, 'eventType': user_event['event_type'], 'toolCalls': user_event['tool_calls']} if isinstance(m, dict) and m.get('id') == user_message_id else type(m).model_validate({**m.model_dump(), 'eventType': user_event['event_type'], 'toolCalls': user_event['tool_calls']}) if getattr(m, 'id', None) == user_message_id and hasattr(m, 'model_dump') else m for m in messages]
        if user_message_id and user_message_id not in existing_ids:
            msg_dict = {'id': user_message_id, 'role': 'user', 'content': user_content, 'timestamp': ts}
            if voice_attachment:
                msg_dict['voiceAttachment'] = voice_attachment
            if images:
                msg_dict['images'] = images
            if user_event:
                msg_dict['eventType'] = user_event['event_type']
                msg_dict['toolCalls'] = user_event['tool_calls']
            messages.append(msg_dict)
            existing_ids.add(user_message_id)
        _has_final = bool(final_response and final_response.strip()) or _tool_receipt_only
        _n_im = len(intermediate_messages)
        for i, im in enumerate(intermediate_messages):
            im_id = im.get('id') or f"msg_{session_id}_{im.get('timestamp', '')}"
            if im_id not in existing_ids:
                im_dict = {'id': im_id, 'role': 'assistant', 'content': apply_voice_marker(im['content'], _voice_marker) if not _has_final and i == _n_im - 1 and _cleaned_tool_calls else im['content'], 'timestamp': im.get('timestamp', ts)}
                if im.get('thinking'):
                    im_dict['thinkingChain'] = [{'content': im['thinking'], 'timestamp': im.get('timestamp', ts)}]
                if not _has_final and i == _n_im - 1 and _cleaned_tool_calls:
                    im_dict['toolCalls'] = _cleaned_tool_calls
                messages.append(im_dict)
                existing_ids.add(im_id)
        final_msg_id = push_message_id or assistant_message_id
        if final_msg_id and final_msg_id not in existing_ids and _has_final:
            am_dict = {'id': final_msg_id, 'role': 'assistant', 'content': apply_voice_marker(_strip_wander_prefix(final_response), _voice_marker), 'timestamp': ts}
            if reasoning:
                am_dict['thinkingChain'] = [{'content': reasoning, 'timestamp': ts}]
            if _cleaned_tool_calls:
                am_dict['toolCalls'] = _cleaned_tool_calls
            messages.append(am_dict)
        await smgr.update_session(session_id, {'messages': messages})
    except Exception as e:
        print(f'[PERSIST] session_manager 写入失败: {e}', flush=True)
    try:
        _trace_assistant_ids = [str(im.get('id') or f"msg_{session_id}_{im.get('timestamp', '')}") for im in intermediate_messages if str(im.get('id') or f"msg_{session_id}_{im.get('timestamp', '')}").strip()]
        _trace_final_id = str(push_message_id or assistant_message_id if _has_final else '').strip()
        if _trace_final_id:
            _trace_assistant_ids.append(_trace_final_id)
        if user_message_id and _trace_assistant_ids:
            from memory_v2.trace_store import get_default_trace_store
            get_default_trace_store().bind_turn_messages_by_user(user_message_id=user_message_id, session_id=session_id, assistant_message_ids=tuple(_trace_assistant_ids), final_assistant_message_id=_trace_final_id)
    except Exception:
        logger.debug('memory recall turn binding skipped', exc_info=True)
    if user_message_id and assistant_msg is not None and bool(str(final_response or '').strip()):
        try:
            from open_loops import get_open_loop_store
            get_open_loop_store().settle_fading_offer(str(user_message_id))
        except Exception:
            logger.debug('open loop fading settlement skipped', exc_info=True)
    return (user_msg, None if _tool_receipt_only else assistant_msg)

async def _persist_special_msg(session_id: str, msg_id: str, content: str, role: str='assistant', *, is_wander: bool=False, is_sentinel: bool=False, is_reminder: bool=False, is_music_cochlea: bool=False, tool_calls: list=None, event_type: str=None, thinking: str=None, metadata: dict=None, model_source: dict=None, images: list=None, replace_existing: bool=False, timestamp: str=None):
    """持久化单条特殊消息（漫想/哨兵/提醒/音乐耳蜗）到 session_manager。"""
    if not session_id or not msg_id:
        return
    from api_sources.message_sources import save as save_message_source, sanitize
    model_source = sanitize(model_source)
    if model_source:
        await asyncio.to_thread(save_message_source, session_id, msg_id, model_source)
    try:
        smgr = get_global_session_manager()
        if not smgr:
            return
        session = await smgr.get_session(session_id)
        if not session:
            return
        messages = list(getattr(session, 'messages', []) or [])
        existing_ids = {getattr(m, 'id', '') if hasattr(m, 'id') else m.get('id', '') for m in messages}
        if msg_id not in existing_ids:
            msg_dict = {'id': msg_id, 'role': role, 'content': content, 'timestamp': timestamp or now_iso(), 'modelSource': model_source}
            if is_wander:
                msg_dict['isWander'] = True
            if is_sentinel:
                msg_dict['isSentinel'] = True
            if is_reminder:
                msg_dict['isReminder'] = True
            if is_music_cochlea:
                msg_dict['isMusicCochlea'] = True
            if tool_calls:
                msg_dict['toolCalls'] = tool_calls
            if event_type:
                msg_dict['eventType'] = event_type
            if metadata:
                msg_dict['notificationMetadata'] = metadata
            if images:
                msg_dict['images'] = images
            if thinking:
                msg_dict['thinkingChain'] = [{'content': thinking, 'timestamp': now_iso()}]
            messages.append(msg_dict)
            await smgr.update_session(session_id, {'messages': messages})
        elif replace_existing:
            for index, existing in enumerate(messages):
                existing_id = getattr(existing, 'id', '') if hasattr(existing, 'id') else existing.get('id', '')
                if existing_id != msg_id:
                    continue
                msg_dict = existing.model_dump(exclude_none=True) if hasattr(existing, 'model_dump') else dict(existing)
                msg_dict.update({'role': role, 'content': content})
                if is_wander:
                    msg_dict['isWander'] = True
                else:
                    msg_dict.pop('isWander', None)
                if tool_calls is not None:
                    msg_dict['toolCalls'] = tool_calls
                if event_type is not None:
                    msg_dict['eventType'] = event_type
                if thinking:
                    msg_dict['thinkingChain'] = [{'content': thinking, 'timestamp': now_iso()}]
                messages[index] = msg_dict
                await smgr.update_session(session_id, {'messages': messages})
                break
    except Exception as e:
        print(f'[PERSIST] 特殊消息写入失败: {e}', flush=True)

async def _emit_ui_notification(session_id: str, source_id: str, results: list[dict] | None=None, *, event_type: str='wish_board_update', content: str | None=None, metadata: dict | None=None):
    """Runtime callback for a generic, persistent UI-only notification."""
    from notification_service import NotificationService, wish_board_notification_details
    if event_type == 'wish_board_update' and results is not None:
        details = wish_board_notification_details(results, metadata)
        if details is None:
            return {'mutated': False, 'reason': 'no_wish_mutation'}
        content, metadata = details
    session_id = session_id or get_active_session_id()
    if not session_id:
        return {'mutated': False, 'reason': 'no_active_session'}
    service = NotificationService(conversation_manager=conversation_manager, persist_special=_persist_special_msg, session_manager=get_global_session_manager(), connections=lambda: wander_connections)
    return await service.emit(session_id=session_id, event_type=event_type, source_id=source_id, content=content, metadata=metadata)

async def _recent_ambient_user_messages() -> list[dict]:
    """Bounded authoritative chat lookup used only for ambient message-echo matching."""
    session_id = get_active_session_id() or get_latest_session_id()
    if not session_id:
        return []
    from event_chronicle import get_global_chronicle
    return await asyncio.to_thread(get_global_chronicle().get_messages_by_session_id, session_id, 30, True)

async def _write_ambient_observation_event(payload: dict) -> str | None:
    """Commit the observation E before any derived AI message M is generated."""
    event_id = str(payload.get('event_id') or '')
    session_id = get_active_session_id() or get_latest_session_id()
    if not event_id or not session_id:
        return None
    fact = {'event_id': event_id, 'batch_id': str(payload.get('batch_id') or ''), 'event_description': str(payload.get('event_description') or '')[:180], 'summary': str(payload.get('summary') or '')[:500], 'participation_reason': str(payload.get('reason') or '')[:240], 'occurred_start': str(payload.get('occurred_start') or ''), 'occurred_end': str(payload.get('occurred_end') or ''), 'learned_at': str(payload.get('learned_at') or ''), 'speaker': payload.get('speaker') if isinstance(payload.get('speaker'), dict) else {'entity_id': 'unknown', 'confidence': 0.0, 'basis': 'unknown'}, 'speaker_observations': [item for item in payload.get('speaker_observations') or [] if isinstance(item, dict)][:80], 'device_background': [item for item in payload.get('device_background') or [] if isinstance(item, dict)][:12]}
    from context_builder.notification_projection import ambient_event_brief
    event_content = ambient_event_brief(fact)
    result = await _emit_ui_notification(session_id, event_id, event_type='ambient_listening_observation', content=event_content, metadata={'event_id': event_id, 'items': [{'tool': 'ambient_listening_observation', 'status': 'success', 'description': 'AI通过已授权的环境监听形成了一条观察事件', 'extra_data': {'ambient_event': fact}}]})
    if not result.get('mutated') and result.get('reason') != 'already_persisted':
        return None
    message_id = str(result.get('message_id') or f'notification:ambient_listening_observation:{event_id}')
    try:
        from affect import get_affect_service
        get_affect_service().observe_observation_event_v2(content=event_content, source_id=message_id, session_id=session_id, occurred_at=str(fact.get('learned_at') or now_iso()))
    except Exception:
        logger.debug('ambient observation affect source skipped', exc_info=True)
    return message_id

async def _send_ambient_note(note: str, source_id: str, session_id: str) -> bool:
    """Reuse the existing band note action and its independent real receipt."""
    if not note or not session_id:
        return False
    from behavior_scheduler.band_touch_tool import BandTouchTool
    from behavior_scheduler.base_tool import ToolStatus
    result = await BandTouchTool().execute(note=note[:80], pattern='single')
    call = {'tool': 'band_touch', 'parameters': {'action': 'note', 'pattern': 'single', 'note': note[:80]}, 'status': result.status.value, 'success': result.status == ToolStatus.SUCCESS, 'result': result.content, 'error': result.error or ''}
    from miband.notices import detach_receipts
    receipts = await detach_receipts(session_id, f'ambient:{source_id}', [call], _emit_ui_notification)
    return bool(receipts and (receipts[0].get('extra_data') or {}).get('band_notice_id'))

def _immediate_checkup_items(tool_calls: list[dict] | None) -> list[dict]:
    """Turn only actually executed sensory calls into receipt items."""
    from wander_manager.checkup_receipts import sensory_tool_items
    return sensory_tool_items(tool_calls)

async def _emit_immediate_checkup_receipt(session_id: str, turn_id: str, user_text: str, tool_calls: list[dict] | None) -> bool:
    """One durable parent receipt for an immediate chat-initiated checkup."""
    text = str(user_text or '')
    from wander_manager.checkup_receipts import is_checkup_request_text
    if not is_checkup_request_text(text):
        return False
    if behavior_scheduler and (behavior_scheduler._infer_future_time(text) or any((word in text for word in ('等会', '过会', '待会', '晚点', '一会', '之后再', '稍后')))):
        return False
    items = _immediate_checkup_items(tool_calls)
    if not items:
        return False
    conclusion = 'uncertain'
    if items and all((item['status'] == 'success' for item in items)):
        conclusion = 'confirmed'
    await _emit_ui_notification(session_id, f'chat:{turn_id}', event_type='checkup_observation', metadata={'intent': 'general', 'origin': 'chat', 'conclusion': conclusion, 'items': items})
    return True
from wander_manager.delivery_gate import ConfirmedDeliveryGate

async def _record_wander_activity_event(payload: dict) -> bool:
    """Persist one settled Wander activity as a mount-only timeline event."""
    session_id = str(payload.get('session_id') or '')
    activity_id = str(payload.get('activity_id') or '')
    if not session_id or not activity_id:
        return False
    from conversation_manager_v2 import resolve_active_date
    from summary import conversation_manager
    import json as _json_event
    message_id = f'activity_event_{activity_id}'
    from event_chronicle import get_global_chronicle
    existing = get_global_chronicle().get_message_by_msg_id(message_id)
    if existing:
        return existing.get('session_id') == session_id and existing.get('event_type') == 'wander_activity'
    occurred_at = str(payload.get('occurred_at') or now_iso())
    summary = str(payload.get('summary') or '').strip()
    calls = payload.get('tool_calls') or []
    try:
        await conversation_manager.v2_manager.add_message(session_id, 'event', summary, message_id=message_id, event_type='wander_activity', tool_calls=_json_event.dumps(calls, ensure_ascii=False), active_date=str(payload.get('active_date') or resolve_active_date(session_id)), calendar_date=occurred_at[:10], timestamp_override=occurred_at, require_persistence=True)
    except Exception:
        logger.exception('漫想活动事件写入失败: %s', activity_id)
        return False
    for ws in list(wander_connections):
        try:
            await ws.send_json({'type': 'chat_reload', 'session_id': session_id})
        except Exception:
            wander_connections.discard(ws)
    return True

async def _record_music_library_event(payload: dict) -> bool:
    """Persist a verified music mutation as a source-linked timeline event."""
    session_id = get_active_session_id() or get_latest_session_id()
    event_id = str(payload.get('event_id') or '')
    if not session_id or not event_id:
        return False
    from summary import conversation_manager
    import json as _json_event
    occurred_at = str(payload.get('occurred_at') or now_iso())
    metadata = [{'tool': 'music_library', 'source_event_id': payload.get('source_event_id'), 'playlist_id': payload.get('playlist_id'), 'node_id': payload.get('node_id'), 'actor_id': payload.get('actor_id')}]
    try:
        from event_chronicle import get_global_chronicle
        from music_system.projection_date import event_active_date
        source_date, date_basis = event_active_date({**payload, 'occurred_at': occurred_at}, session_id, get_global_chronicle())
        metadata[0]['active_date_basis'] = date_basis
        await conversation_manager.v2_manager.add_message(session_id, 'event', str(payload.get('content') or ''), message_id=event_id, event_type='music_library_event', tool_calls=_json_event.dumps(metadata, ensure_ascii=False), active_date=source_date, calendar_date=occurred_at[:10], timestamp_override=occurred_at, require_persistence=True)
    except Exception:
        logger.exception('音乐歌单事件投影失败: %s', event_id)
        return False
    for ws in list(wander_connections):
        try:
            await ws.send_json({'type': 'chat_reload', 'session_id': session_id})
        except Exception:
            wander_connections.discard(ws)
    return True

@ordinary
@ConfirmedDeliveryGate()
async def _push_wander_message(payload: dict) -> bool:
    """广播漫想主动消息到所有WebSocket客户端，同时将带标记的消息存入conversation_manager供LLM上下文使用"""
    message = payload.get('message', '')
    tool_calls = payload.get('tool_calls', [])
    event_type = payload.get('event_type', '')
    source = payload.get('activity_source')
    plain_lounge_followup = bool(payload.get('plain_assistant') is True and event_type == 'visit_lounge_followup' and (source in {'ui_action', 'visitor', 'autonomy'}) and str(payload.get('event_id') or '').startswith('lounge_followup_'))
    plain_workshop_followup = bool(payload.get('plain_assistant') is True and event_type == 'workshop_share_followup' and (source == 'workshop') and str(payload.get('event_id') or '').startswith('workshop_share_'))
    plain_followup = plain_workshop_followup
    from wander_manager.message_classification import classify_message
    record_role, visible_event_type = classify_message(event_type, tool_calls, plain_followup=plain_followup)
    if record_role == 'assistant':
        from visible_reply import clean_visible_reply
        message = clean_visible_reply(message)
        if not message.strip() and (not tool_calls):
            return False
    replace_lounge_followup = bool(payload.get('replace_existing') is True and plain_lounge_followup)
    replace_authoritative = plain_workshop_followup and payload.get('replace_existing') is True
    is_wander = not (event_type == 'visit_lounge' and source in {'ui_action', 'visitor'} or event_type in {'lounge_board', 'lounge_board_incoming'} or plain_followup)
    record_is_wander = is_wander and record_role == 'assistant'
    if is_wander:
        from shared_state import should_suppress_proactive
        if should_suppress_proactive():
            return False
    event_id = payload.get('event_id', '')
    reasoning = payload.get('reasoning', '') or ''
    try:
        from wander_manager.chat_attachment_boundary import sanitize_chat_image_metadata
        images = [safe for item in payload.get('images') or [] if (safe := sanitize_chat_image_metadata(item)) is not None][:4]
    except Exception:
        images = []
    connection_count = len(wander_connections)
    log_preview = '<board event>' if event_type in {'lounge_board', 'lounge_board_incoming'} else message[:80]
    print(f'[PUSH_WANDER] 开始推送消息, 连接数={connection_count}, event_id={event_id}, message={log_preview}', flush=True)
    if tool_calls:
        print(f"[PUSH_WANDER] 工具调用: {[tc['tool'] for tc in tool_calls]}", flush=True)
    target_session = payload.get('session_id') or _latest_session_id
    if not target_session:
        try:
            import json as _json
            _idx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions', 'sessions_index.json')
            if os.path.exists(_idx_path):
                with open(_idx_path, 'r', encoding='utf-8') as _f:
                    _idx = _json.load(_f)
                target_session = _idx.get('current_session_id') if isinstance(_idx, dict) else None
        except Exception:
            logger.exception('unexpected error in _push_wander_message')
            pass
    if target_session:
        try:
            from miband.notices import detach_receipts
            tool_calls = await detach_receipts(target_session, f'wander_{event_id}' if event_id else 'wander_share', tool_calls, _emit_ui_notification)
            from summary import topic_state_machine
            await topic_state_machine.maybe_end_for_day_change(target_session)
            from summary import ensure_topic_exists
            await ensure_topic_exists(target_session)
            status = await topic_state_machine.get_topic_status(target_session)
            if not status.get('is_active'):
                from summary import Message
                _wm = Message(id=f'wander_{event_id}' if event_id else f'wander_{int(datetime.now().timestamp() * 1000)}', role='assistant', content=message[:200] if message else '', timestamp=datetime.now().isoformat())
                await topic_state_machine.start_new_topic(target_session, _wm)
            from summary import conversation_manager
            from conversation_manager_v2 import resolve_active_date
            import json as _json_w
            _wander_tc = strip_voice_base64(tool_calls) if tool_calls else None
            _wander_tc_json = _json_w.dumps(_wander_tc, ensure_ascii=False) if _wander_tc else None
            if is_wander and should_suppress_proactive():
                return False
            message_id = f'wander_{event_id}' if event_id else f'wander_{int(datetime.now().timestamp() * 1000)}'
            write = conversation_manager.v2_manager.upsert_message if replace_authoritative else conversation_manager.v2_manager.add_message
            kwargs = dict(message_id=message_id, reasoning_content=reasoning or None, is_wander=record_is_wander, is_sentinel=False, is_reminder=False, event_type=visible_event_type, tool_calls=_wander_tc_json, tool_summary=build_tool_summary(tool_calls), active_date=resolve_active_date(target_session), images=_json_w.dumps(images, ensure_ascii=False) if images else None)
            if not replace_authoritative:
                kwargs['require_persistence'] = True
            from proactive_expression import stored_content
            persisted_message = stored_content(message, tool_calls)
            await write(target_session, record_role, persisted_message, **kwargs)
            if event_type == 'ambient_listening_reply':
                try:
                    from affect import get_affect_service
                    get_affect_service().observe_assistant_message_v2(content=message, source_id=message_id, session_id=target_session, occurred_at=now_iso())
                except Exception:
                    logger.debug('ambient reply affect source skipped', exc_info=True)
            if replace_authoritative:
                from event_chronicle import get_global_chronicle
                saved = get_global_chronicle().get_message_by_msg_id(message_id)
                expected = {'session_id': target_session, 'role': record_role, 'content': persisted_message, 'event_type': visible_event_type}
                if not saved or any(((saved.get(key) or '') != value for key, value in expected.items())):
                    raise RuntimeError('message_commit_not_confirmed')
            print(f'[PUSH_WANDER] 已保存到conversation_manager, session={target_session}', flush=True)
        except Exception as e:
            logger.warning('漫想权威消息保存失败: %s', type(e).__name__)
            return False
    else:
        print(f'[PUSH_WANDER] 无活跃会话ID，跳过conversation_manager保存', flush=True)
        return False
    if target_session:
        msg_id = f'wander_{event_id}' if event_id else f'wander_{int(datetime.now().timestamp() * 1000)}'
        if payload.get('_expression_delivery') is not None:
            payload['_expression_delivery'].finish(True)
    try:
        from behavior_scheduler.tools import push_mobile_notification as _pmn
        import asyncio as _asyncio_pmn
        _asyncio_pmn.ensure_future(_pmn('AI💭' if is_wander else 'AI', message[:120]))
    except Exception:
        logger.exception('unexpected error in _push_wander_message')
        pass
    if not wander_connections:
        logger.info('漫想消息已持久化，等待客户端读取')
        return True
    ws_payload = {'type': 'wander_push', 'role': record_role, 'session_id': target_session, 'isWander': record_is_wander, 'message': message, 'timestamp': now_iso(), 'toolCalls': tool_calls, 'eventType': visible_event_type, 'eventId': event_id, 'reasoning': reasoning, 'images': images, 'modelSource': payload.get('model_source')}
    stale = set()
    for ws in list(wander_connections):
        try:
            await ws.send_json(ws_payload)
            logger.info(f'漫想消息已推送: {message[:60]}...')
            print(f'[PUSH_WANDER] 消息发送成功', flush=True)
        except Exception as e:
            print(f'[PUSH_WANDER] 发送失败: {e}', flush=True)
            stale.add(ws)
    wander_connections.difference_update(stale)
    if stale:
        logger.info(f'已清理 {len(stale)} 个断开的漫想连接')
    return True
from workshop.api import router as _workshop_router
from workshop.service import get_service as _workshop_service
app.include_router(_workshop_router)
_workshop_service().sender = _emit_ui_notification
_workshop_service().share_sender = _push_wander_message

async def _broadcast_wander_status(status_dict: dict):
    """广播漫想状态变化到所有WebSocket客户端"""
    if not wander_connections:
        return
    payload = {'type': 'wander_status', 'status': status_dict}
    stale = set()
    for ws in list(wander_connections):
        try:
            await ws.send_json(payload)
        except Exception:
            stale.add(ws)
    wander_connections.difference_update(stale)

def _on_wander_status_change_sync(status_dict: dict):
    """同步包装：在同步上下文中触发的状态广播"""
    try:
        asyncio.create_task(_broadcast_wander_status(status_dict))
    except RuntimeError as e:
        logger.error(f'创建状态广播任务失败: {e}')

@ordinary
async def _push_sentinel_message(payload: dict):
    """推送哨兵告警消息到前端 + 存入 conversation_manager 上下文

    消息格式：
    - 消息 ID: sentinel_{rule_id}_{iso_timestamp}
    - 存储前缀: [🛡️ 哨兵提醒]
    - isSentinel: true
    """
    from shared_state import should_suppress_proactive
    if should_suppress_proactive():
        return False
    from visible_reply import clean_visible_reply
    message = clean_visible_reply(payload.get('message', ''))
    if not message.strip():
        return False
    rule_id = payload.get('rule_id', '')
    priority = payload.get('priority', '')
    timestamp = payload.get('timestamp', now_iso())
    reasoning = payload.get('reasoning')
    event_id = f'sentinel_{rule_id}_{timestamp}'
    async with _pushed_event_ids_lock:
        if event_id in _pushed_sentinel_ids:
            print(f'[PUSH_SENTINEL] 事件 {event_id} 已推送过，跳过', flush=True)
            return _pushed_sentinel_ids[event_id] is True
        from silicon_perception.delivery import new_chat_since
        if new_chat_since(payload.get('generation_started_at')):
            return False
        _pushed_sentinel_ids[event_id] = None
        if len(_pushed_sentinel_ids) > 200:
            oldest = list(_pushed_sentinel_ids.keys())[:100]
            for k in oldest:
                del _pushed_sentinel_ids[k]
    print(f'[PUSH_SENTINEL] 告警推送: rule={rule_id}, priority={priority}, msg={message[:80]}', flush=True)
    tool_calls = []
    band_request = payload.get('band_touch_request')
    band_parse_error = str(payload.get('band_touch_parse_error') or '')
    if band_request is not None or band_parse_error:
        try:
            from behavior_scheduler.band_touch_tool import BandTouchTool
            from proactive_band_touch import execute_request
            band_call, _ = await execute_request(BandTouchTool(), band_request, band_parse_error)
            if band_call is not None:
                tool_calls.append(band_call)
        except Exception as exc:
            logger.warning('[PUSH_SENTINEL] 手环动作执行边界失败: %s', type(exc).__name__)
    checkup_request = payload.get('checkup_request')
    checkup_parse_error = str(payload.get('checkup_parse_error') or '')
    if checkup_request is not None and (not checkup_parse_error):
        try:
            from proactive_checkup import schedule_request
            checkup_call, _ = await schedule_request(checkup_request)
            if checkup_call is not None:
                tool_calls.append(checkup_call)
        except Exception as exc:
            logger.warning('[PUSH_SENTINEL] 定时查岗安排失败: %s', type(exc).__name__)
    target_session = payload.get('session_id') or _latest_session_id
    if not target_session:
        try:
            import json as _json
            _idx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions', 'sessions_index.json')
            if os.path.exists(_idx_path):
                with open(_idx_path, 'r', encoding='utf-8') as _f:
                    _idx = _json.load(_f)
                target_session = _idx.get('current_session_id') if isinstance(_idx, dict) else None
        except Exception:
            logger.exception('unexpected error in _push_sentinel_message')
            pass
    from proactive_expression import ExpressionDelivery
    expression_delivery = ExpressionDelivery()
    if target_session:
        try:
            if tool_calls:
                from miband.notices import detach_receipts
                tool_calls = await detach_receipts(target_session, event_id, tool_calls, _emit_ui_notification)
            from summary import ensure_topic_exists, topic_state_machine
            await ensure_topic_exists(target_session)
            status = await topic_state_machine.get_topic_status(target_session)
            if not status.get('is_active'):
                from summary import Message
                _sm = Message(id=event_id or f'sentinel_{int(datetime.now().timestamp() * 1000)}', role='assistant', content=message[:200] if message else '', timestamp=datetime.now().isoformat())
                await topic_state_machine.start_new_topic(target_session, _sm)
            from summary import conversation_manager
            from conversation_manager_v2 import resolve_active_date
            import json as _sentinel_json
            tool_calls.extend(await expression_delivery.prepare(payload.get('expression_requests', []), target_session))
            _sentinel_calls = strip_voice_base64(tool_calls) if tool_calls else None
            from proactive_expression import stored_content
            await conversation_manager.v2_manager.add_message(target_session, 'assistant', stored_content(message, tool_calls), message_id=event_id, is_wander=False, is_sentinel=True, is_reminder=False, reasoning_content=reasoning, tool_calls=_sentinel_json.dumps(_sentinel_calls, ensure_ascii=False) if _sentinel_calls else None, tool_summary=build_tool_summary(tool_calls), event_type=rule_id, active_date=resolve_active_date(target_session), require_persistence=True, timestamp_override=timestamp)
            _pushed_sentinel_ids[event_id] = True
            print(f'[PUSH_SENTINEL] 已保存到conversation_manager, session={target_session}', flush=True)
        except Exception as e:
            expression_delivery.finish(False)
            print(f'[PUSH_SENTINEL] 保存到conversation_manager失败: {e}', flush=True)
            return False
        except asyncio.CancelledError:
            expression_delivery.finish(False)
            raise
    else:
        print(f'[PUSH_SENTINEL] 无活跃会话ID，跳过conversation_manager保存', flush=True)
        return False
    if target_session:
        try:
            await _persist_special_msg(target_session, event_id, message, is_sentinel=True, tool_calls=tool_calls, thinking=reasoning, model_source=payload.get('model_source'))
        except Exception as exc:
            logger.warning('Sentinel cache projection failed: %s', type(exc).__name__)
        finally:
            expression_delivery.finish(True)
    try:
        from behavior_scheduler.tools import push_mobile_notification as _pmn
        import asyncio as _asyncio_pmn
        _asyncio_pmn.ensure_future(_pmn('AI🛡️', message[:120]))
    except Exception:
        logger.exception('unexpected error in _push_sentinel_message')
        pass
    if not wander_connections:
        print('[PUSH_SENTINEL] 消息已持久化，等待客户端读取', flush=True)
        return True
    ws_payload = {'type': 'sentinel_push', 'modelSource': payload.get('model_source'), 'message': message, 'timestamp': timestamp, 'ruleId': rule_id, 'priority': priority, 'eventId': event_id, 'toolCalls': tool_calls}
    if reasoning:
        ws_payload['reasoning'] = reasoning
    stale = set()
    for ws in list(wander_connections):
        try:
            await ws.send_json(ws_payload)
            print(f'[PUSH_SENTINEL] 消息发送成功', flush=True)
        except Exception as e:
            print(f'[PUSH_SENTINEL] 发送失败: {e}', flush=True)
            stale.add(ws)
    wander_connections.difference_update(stale)
    return True
_music_dir = os.path.join(os.path.dirname(__file__), 'voice_manager', 'output', 'music')
os.makedirs(_music_dir, exist_ok=True)
app.mount('/voice/music', StaticFiles(directory=_music_dir), name='voice_music')
from ui_action_messages import configure_invitation as configure_ui_actions
configure_ui_actions(conversation_manager, _persist_special_msg, get_global_session_manager, lambda: wander_connections)
from routers.optional_home_router import router as optional_home_router
app.include_router(optional_home_router)
if __name__ == '__main__':
    port = int(os.getenv('PORT', 8005))
    host = os.getenv('HOST', '127.0.0.1')
    import uvicorn
    import asyncio
    try:
        uvicorn.run(app, host=host, port=port, loop='backend_event_loop:create_loop', reload=False, workers=1, log_level='info', timeout_graceful_shutdown=30)
    except asyncio.CancelledError:
        pass
