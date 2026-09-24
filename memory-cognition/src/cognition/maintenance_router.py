"""UI maintenance controls. Reading never starts an analysis."""
import asyncio
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Body, HTTPException
from . import maintenance as service
from .router import safe_call

router = APIRouter(prefix="/api/cognition/maintenance", tags=["cognition"])
_key = secrets.token_bytes(32)


def signature(plan):
    return hmac.new(_key, service.digest(plan).encode(), hashlib.sha256).hexdigest()


@router.get("")
def listing():
    report = safe_call(service.record_report)
    items = report["records"]
    from .scp_daily import status as scp_status
    return {"records": [{**i, "item_revision": service.digest(i),
                          "evidence_warning": (
                              "只有 Agent 的音乐感想，没有设备或歌单操作回执；这条建议不能作为外部事实采纳。"
                              if service.music_note_only_world(i, i.get("plan", {}).get("domain")) else "")}
                         for i in items], "scp": safe_call(scp_status),
            "run": safe_call(service.status), "damaged_records": report["damaged_records"]}


@router.get("/scp-status")
def scp_status():
    from .scp_daily import status
    return safe_call(status)


@router.post("/{rid}/preview")
def preview(rid: str, options: dict):
    plan = safe_call(service.preview, rid, options)
    return {"plan": plan, "token": signature(plan)}


@router.post("/{rid}/decide")
def decide(rid: str, data: dict):
    if data.get("action") == "reject":
        return safe_call(service.decide, rid, "reject", expected=data.get("item_revision", ""))
    plan = data.get("plan")
    if not isinstance(plan, dict) or plan.get("id") != rid or not hmac.compare_digest(str(data.get("token", "")), signature(plan)):
        raise HTTPException(409, "预览已变化或后端已重启，请重新预览")
    return safe_call(service.decide, rid, "apply", plan=plan, expected=plan.get("item_revision", ""),
                     new_entity_confirmed=data.get("new_entity_confirmed") is True)


async def analyse_current(source_date: str = ""):
    """跑一天的认知整理。给 source_date 可补跑指定活跃日（如失败留下的 backlog），
    省略则跑最新活跃日。四路已改串行，整轮耗时比并发长，故放宽等待上限。"""
    from datetime import datetime
    from shared_state import get_active_session_id
    from event_chronicle import get_global_chronicle
    from llm_client import call_llm_flash_for_long_text
    session = get_active_session_id()
    if not session:
        raise HTTPException(409, "尚无主会话")
    if source_date:
        try:
            datetime.strptime(source_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise HTTPException(422, "日期格式无效") from None
    else:
        rows = await asyncio.to_thread(get_global_chronicle().get_messages_by_session_id, session, 1, True)
        if not rows or not rows[0].get("active_date"):
            raise HTTPException(409, "尚无可分析的活跃日")
        source_date = rows[0]["active_date"]
    try:
        result = await asyncio.wait_for(
            service.run_for_date(source_date, session, call_llm_flash_for_long_text), timeout=1800)
        return {"status": result["status"], "items_count": len(result.get("items", [])),
                "source_date": source_date}
    except Exception:
        raise HTTPException(503, "分析未全部完成，请到自动维护查看状态；已写入结果不会撤销") from None


@router.post("/run")
async def run(data: dict = Body(default=None)):
    return await analyse_current(str((data or {}).get("source_date") or ""))


@router.post('/run-world')
async def retry_world(data: dict):
    from datetime import datetime
    from shared_state import get_active_session_id
    from event_chronicle import get_global_chronicle
    from llm_client import call_llm_flash_for_long_text
    from .world_retry import retry
    source_date = data.get('source_date', '')
    try: datetime.strptime(source_date, '%Y-%m-%d')
    except (ValueError, TypeError): raise HTTPException(422, '日期格式无效') from None
    session = get_active_session_id()
    if not session: raise HTTPException(409, '尚无主会话')
    rows = await asyncio.to_thread(get_global_chronicle().get_messages_by_date, source_date)
    rows = [r for r in rows if r.get('session_id') == session]
    return await retry(source_date, session, rows, call_llm_flash_for_long_text)


@router.post('/preview-other-day')
async def preview_other_day(data: dict):
    from datetime import datetime
    from shared_state import get_active_session_id
    from event_chronicle import get_global_chronicle
    from llm_client import call_llm_flash_for_long_text
    from .other_preview import preview
    source_date = data.get('source_date', '')
    try: datetime.strptime(source_date, '%Y-%m-%d')
    except (ValueError, TypeError): raise HTTPException(422, '日期格式无效') from None
    session = get_active_session_id()
    if not session: raise HTTPException(409, '尚无主会话')
    rows = await asyncio.to_thread(get_global_chronicle().get_messages_by_date, source_date)
    rows = [r for r in rows if r.get('session_id') == session]
    if not rows: raise HTTPException(409, '所选日期没有主会话材料')
    return await preview(rows, call_llm_flash_for_long_text, source_date=source_date, session_id=session)
