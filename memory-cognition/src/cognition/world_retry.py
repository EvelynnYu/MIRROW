"""Explicit, backed-up retry of one world's active day, not other lanes."""
import asyncio
import json
import shutil
from datetime import datetime
from uuid import uuid4
from . import books, maintenance
from .maintenance_sources import sources


async def retry(source_date, session_id, rows, llm):
    datetime.strptime(source_date, '%Y-%m-%d')
    evidence = sources(rows)
    if not evidence: raise books.BookError('这一天没有可用私聊材料')
    run_id = maintenance.digest({'date':source_date,'session':session_id,'messages':evidence,'pipeline':'factual-v1'})
    target = books.ROOT / 'backups' / ('world_retry_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8])
    target.mkdir(parents=True)
    manifest = []
    with books.LOCK:
        paths = [*books.folder('world').rglob('*.md'), *maintenance.folder().rglob('*.json')]
        for path in paths:
            relative = path.relative_to(books.ROOT)
            dest = target / 'originals' / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            if dest.read_bytes() != path.read_bytes(): raise books.BookError('备份期间材料变化，停止重试')
            manifest.append({'path':relative.as_posix(),'revision':books.revision(path.read_text('utf-8'))})
        books.atomic_text(target/'manifest.json',json.dumps(manifest,ensure_ascii=False))
    calls = 0
    async def traced(prompt):
        nonlocal calls
        calls += 1
        books.atomic_text(target/f'call_{calls}_input.txt',prompt)
        result = await llm(prompt)
        books.atomic_text(target/f'call_{calls}_output.txt',result if isinstance(result,str) else json.dumps(result,ensure_ascii=False))
        return result
    try:
        from .daily_memory import recall
        try: memory = await recall(evidence)
        except Exception: memory = []
        result = await maintenance.run(rows,traced,source_date=source_date,session_id=session_id,factual=True,memory=memory)
        # Reconcile only this lane in the same evidence-bound aggregate.
        rid = maintenance.digest({'pipeline':'daily-v1','date':source_date,'session':session_id,'evidence':evidence})
        path = maintenance.folder()/'daily_runs'/(rid+'.json')
        if path.exists():
            state=json.loads(path.read_text('utf-8'))
            state.setdefault('lanes',{})['world']=result['status']
            state.get('lane_errors',{}).pop('world',None)
            state['items']=list(dict.fromkeys([*state.get('items',[]),*result.get('items',[])]))
            state['status']='error' if any(v in {'error','running','interrupted'} for v in state['lanes'].values()) else \
                'completed_with_rejections' if any(v == 'completed_with_rejections' for v in state['lanes'].values()) else 'completed'
            state['world_retried_at']=datetime.now().astimezone().isoformat()
            books.atomic_text(path,json.dumps(state,ensure_ascii=False))
        counts={}
        for iid in result.get('items',[]):
            item=maintenance.get(iid); counts[item['status']]=counts.get(item['status'],0)+1
        summary={'status':result['status'],'counts':counts,'calls':calls,'backup':target.name}
    except Exception as exc:
        path=maintenance.folder()/'runs'/(run_id+'.json')
        state=json.loads(path.read_text('utf-8')) if path.exists() else {}
        summary={'status':'error','error_type':type(exc).__name__,'phase':state.get('phase'),
                 'detail':state.get('error_detail','重试失败'),'proposal_index':state.get('proposal_index'),'calls':calls,'backup':target.name}
    books.atomic_text(target/'result.json',json.dumps(summary,ensure_ascii=False))
    return summary
