"""Bounded retries of an uncommitted LLM phase; metadata-only receipts.

The caller's stack retains the original material and completed business stages.
Receipts preserve the budget when the same job/phase is invoked again; they do
not constitute a second source of business facts or store any prompt/output.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing
from .policies import category_for, route_role
from .scope import current_job, current_snapshot, phase_status

DB_PATH=Path(__file__).resolve().parents[1]/"events"/"api_retry_jobs.db"


def record(job_id, phase_id, *, state, rounds, expires_at, retry_after=0, path=DB_PATH):
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=3)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS phases(job_id TEXT,phase_id TEXT,state TEXT,rounds INTEGER,expires_at REAL,retry_after REAL,PRIMARY KEY(job_id,phase_id))")
        db.execute("INSERT INTO phases VALUES(?,?,?,?,?,?) ON CONFLICT(job_id,phase_id) DO UPDATE SET state=excluded.state,rounds=excluded.rounds,expires_at=excluded.expires_at,retry_after=excluded.retry_after",(job_id,phase_id,state,rounds,expires_at,retry_after))


def previous(job_id,phase_id,path=DB_PATH):
    if not path.exists():return None
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro",uri=True,timeout=3)) as db:
            return db.execute("SELECT state,rounds,expires_at,retry_after FROM phases WHERE job_id=? AND phase_id=?",(job_id,phase_id)).fetchone()
    except sqlite3.Error:
        return None


async def run_recoverable(messages, *, route, sources=None, task_id=None, max_rounds=None,
                          expires_at=None, receipt_path=None, **kwargs):
    from .runner import run_structured_once
    from .config import resolve_chain
    from wander_manager.flash_structured import FlashJsonResult
    if sources is None:
        sources,timeout=await asyncio.to_thread(resolve_chain,route)
        kwargs.setdefault("timeout_seconds",timeout)
    snapshot=current_snapshot()
    if snapshot is None or not sources or not sources[0].get("selection",{}).get("managed",sources[0].get("selection",{}).get("inherited")) or route_role(route)!="internal" or category_for(route,snapshot[0].assignments) not in snapshot[0].policies:
        return await run_structured_once(messages,route=route,sources=sources,task_id=task_id,**kwargs)
    policy=snapshot[0].policies[category_for(route,snapshot[0].assignments)]
    job_id=current_job() or task_id or uuid.uuid4().hex
    phase_material = {"messages": messages, "revision": snapshot[2]}
    if kwargs.get("image_batch"):
        phase_material["image_hashes"] = [hashlib.sha256(image.encode("ascii")).hexdigest()
                                          for image in kwargs["image_batch"]]
    digest=hashlib.sha256(json.dumps(phase_material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    phase_id=route+":"+digest
    path=receipt_path or DB_PATH
    saved=await asyncio.to_thread(previous,job_id,phase_id,path)
    from . import background_cost
    if route in background_cost.ROUTES:
        return await background_cost.run(messages, sources=sources, route=route, job_id=job_id,
            phase_id=phase_id, saved=saved, path=path, max_rounds=max_rounds, expires_at=expires_at, **kwargs)
    if saved and saved[0] == "refusal":
        result = FlashJsonResult(route=route,status="refusal",error="previous_phase_refusal",routing_managed=True,task_id=job_id)
        result.phase_id = phase_id
        phase_status(route,result.status,phase_id)
        return result
    rounds=saved[1] if saved and saved[0] not in {"ok","cancelled","not_dispatched"} else 0
    deadline=expires_at or (saved[2] if saved and saved[0] not in {"ok","cancelled","not_dispatched"} else time.time()+min(policy.expiry_seconds,120 if route=="sentinel_first_layer" else policy.expiry_seconds))
    limit=min(max_rounds or policy.retry_rounds,policy.retry_rounds)
    result=FlashJsonResult(route=route,status="expired" if time.time()>=deadline else "error",error="retry_budget_exhausted",routing_managed=True,task_id=job_id)
    from .dispatch_state import combine
    request_dispatched = False if not saved or saved[0] == 'not_dispatched' else None
    while rounds<limit and time.time()<deadline:
        rounds+=1
        await asyncio.to_thread(record,job_id,phase_id,state="running",rounds=rounds,expires_at=deadline,path=path)
        try:
            async with asyncio.timeout(max(.001,deadline-time.time())):
                result=await run_structured_once(messages,route=route,sources=sources,task_id=job_id,**kwargs)
                request_dispatched = combine(request_dispatched, getattr(result, 'request_dispatched', None))
        except asyncio.CancelledError:
            await asyncio.to_thread(record,job_id,phase_id,state="cancelled",rounds=rounds,expires_at=deadline,path=path)
            raise
        except TimeoutError:
            request_dispatched = combine(request_dispatched, None)
            result.status,result.error="expired","task_expired"
        phase_status(route,result.status,phase_id)
        stop=result.status in {"ok","refusal","configuration_error","expired"} or rounds>=limit
        # A wrong node contract across every supplier is not a network retry.
        if result.error in {"invalid_observation","invalid_plan_envelope","invalid_tool_calls"}:stop=True
        delay=policy.retry_delay_seconds*2**(rounds-1)
        if time.time()+delay>=deadline:stop=True
        await asyncio.to_thread(record,job_id,phase_id,state=result.status if stop else "waiting",rounds=rounds,expires_at=deadline,retry_after=0 if stop else time.time()+delay,path=path)
        if stop:break
        await asyncio.sleep(delay)
    result.phase_id = phase_id
    result.request_dispatched = request_dispatched
    phase_status(route,result.status,phase_id)
    await asyncio.to_thread(record,job_id,phase_id,
        state='not_dispatched' if request_dispatched is False else result.status,
        rounds=0 if request_dispatched is False else rounds,expires_at=deadline,path=path)
    return result
