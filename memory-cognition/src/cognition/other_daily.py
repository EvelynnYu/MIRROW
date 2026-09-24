"""Daily other-book orchestration; preview and commit share one analysis engine."""
import asyncio
import json
from . import books, maintenance, autonomous
from .maintenance_sources import sources
from .other_history import searcher

_lock = asyncio.Lock()


async def analyse_rows(rows, llm, *, source_date, session_id):
    evidence = sources(rows)
    return await analyse_evidence(evidence, llm, source_date=source_date, session_id=session_id)


async def analyse_evidence(evidence, llm, *, source_date, session_id):
    """Analyse already-attributed internal evidence without remapping speakers."""
    from .other_analysis import analyse
    from .other_observations import related
    evidence = [{**e, 'active_date': source_date} for e in evidence]
    if not evidence:
        return {'entries': [], 'entities': [], 'snapshot': books.catalog('other'), 'evidence': [], 'observations': [], 'diagnostics': {'calls': 0}}
    return await analyse(evidence, llm, source_date=source_date, session_id=session_id,
                         search=searcher(session_id, source_date), observations=related(evidence, session_id, source_date))


async def run(rows, llm, *, source_date, session_id):
    evidence = sources(rows)
    return await run_evidence(
        evidence, llm, source_date=source_date, session_id=session_id,
        _analyse=lambda: analyse_rows(rows, llm, source_date=source_date, session_id=session_id),
    )


async def run_evidence(evidence, llm, *, source_date, session_id, _analyse=None):
    """Commit private-chat plus trusted external-interaction evidence."""
    if not evidence: return {'status': 'no_evidence', 'items': []}
    rid = maintenance.digest({'pipeline':'other-daily-v1','date':source_date,'session':session_id,'evidence':evidence})
    path = maintenance.folder() / 'other_runs' / (rid + '.json')
    async with _lock:
        state = json.loads(path.read_text('utf-8')) if path.exists() else {}
        if state.get('status') == 'completed': return state
        state.update(id=rid, source_date=source_date, session_id=session_id, status='running')
        def checkpoint(): books.atomic_text(path, json.dumps(state, ensure_ascii=False))
        checkpoint()
        try:
            if 'analysis' not in state:
                state['analysis'] = await (_analyse() if _analyse else analyse_evidence(
                    evidence, llm, source_date=source_date, session_id=session_id))
                checkpoint()
            result = state['analysis']
            snapshot = result['snapshot']
            if isinstance(snapshot, list): snapshot = {'self': [], 'other': snapshot}
            committed = await autonomous.run_evidence(result['evidence'], llm, source_date=source_date,
                session_id=session_id, origin='other_daily', scope='other', prepared={'snapshot': snapshot,
                'output': {'entities':result.get('entities',[]), 'entries':result['entries']}})
            state.update(status='completed', items=committed.get('items', []))
            checkpoint()
            return {'status':'completed','items':state['items']}
        except books.RevisionConflict:
            state.pop('analysis',None)
            state.update(status='error', error_type='RevisionConflict'); checkpoint(); raise
        except BaseException as exc:
            # 记下具体原因：只留 error_type 会让提交阶段的失败无从定位（2026-09-11）
            state.update(status='error', error_type=type(exc).__name__, error_detail=str(exc)[:300]); checkpoint(); raise
