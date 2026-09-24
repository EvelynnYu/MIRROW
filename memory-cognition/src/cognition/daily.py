"""Independent subjective, factual and signalled SCP lanes."""
import asyncio
import json
from datetime import datetime
from . import books, maintenance, autonomous, scp_daily, other_daily
from .maintenance_sources import sources


async def _lane(step):
    """跑一路整理：异常收敛为返回值，CancelledError 照常上抛。"""
    try:
        return await step()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        return exc


async def run_daily(rows, llm, *, source_date, session_id, progress_cb=None,
                    memory_lookup=None, other_evidence=None):
    """Settle one canonical day; optional sources are supplied by the host.

    ``memory_lookup`` is an async read-only callback receiving attributed
    evidence. ``other_evidence`` contains already-attributed observations from
    another trusted surface, never raw HTTP payloads.
    """
    evidence = sources(rows)
    other_evidence = [*evidence, *(other_evidence or [])]
    rid = maintenance.digest({'pipeline':'daily-v2','date':source_date,'session':session_id,
                              'evidence':evidence, 'other_evidence':other_evidence})
    path = maintenance.folder() / 'daily_runs' / (rid+'.json')
    state={'id':rid,'source_date':source_date,'status':'running','instance':maintenance._instance,'started_at':datetime.now().astimezone().isoformat()}
    books.atomic_text(path,json.dumps(state,ensure_ascii=False))
    memory=[]
    if memory_lookup is not None:
        try:
            memory=await memory_lookup(evidence)
        except Exception:
            state['memory_status']='unavailable'
    # 串行执行四路：并发的多个大请求曾把 Flash 挤到 504 超时（2026-09-11 前夜
    # 四路连同 todo_profile 全灭）。串行后单路失败只影响自身，其余照常落地。
    # 每路结束戳一次心跳，避免长耗时被 _stall_watchdog 误判卡住。
    results=[]
    for step in (
        lambda: maintenance.run(rows,llm,source_date=source_date,session_id=session_id,factual=True,memory=memory),
        lambda: autonomous.run(rows,llm,source_date=source_date,session_id=session_id,memory=memory,scope='self'),
        lambda: other_daily.run_evidence(other_evidence,llm,source_date=source_date,session_id=session_id),
        lambda: scp_daily.run(evidence,llm,source_date=source_date,session_id=session_id),
    ):
        results.append(await _lane(step))
        if progress_cb:
            progress_cb()
    errors=any(isinstance(r,BaseException) for r in results)
    partial=any(isinstance(r,dict) and r.get('status') == 'completed_with_rejections' for r in results)
    state.update(status='error' if errors else 'completed_with_rejections' if partial else 'completed',items=[x for r in results if isinstance(r,dict) for x in r.get('items',[])],
                 lanes={name: 'error' if isinstance(result,BaseException) else result.get('status') for name,result in zip(('world','self','other','scp'),results)},
                 completed_at=datetime.now().astimezone().isoformat())
    if errors: state['message']='部分整理未完成；已保存的内容保留，可重试。'
    elif partial: state['message']='可核实的候选已完成；少量无法逐条核实的候选已隔离。'
    if partial:
        state['lane_rejections'] = {
            name: {'proposal_count': result.get('rejected_proposal_count', 0),
                   'review_count': result.get('discarded_review_count', 0)}
            for name, result in zip(('world','self','other','scp'), results)
            if isinstance(result, dict) and result.get('status') == 'completed_with_rejections'}
    if errors:
        state['lane_errors'] = {name: {'type': type(result).__name__,
            'message': '整理请求超时' if isinstance(result, TimeoutError) or getattr(result, 'status_code', None) == 504 else
                       '模型返回无法解析' if isinstance(result, json.JSONDecodeError) else
                       '资料或版本校验未通过' if isinstance(result, books.BookError) else '整理调用失败，保留材料等待重试'}
            for name, result in zip(('world','self','other','scp'), results) if isinstance(result, BaseException)}
    books.atomic_text(path,json.dumps(state,ensure_ascii=False))
    if errors: raise books.BookError('部分认知整理失败，查看运行状态')
    state['world_items']=results[0].get('items',[])
    return state
