"""Application lifecycle and polling. The browser is not the playback authority."""
import asyncio
import logging
from . import devices, daily
from .service import configure, get_service
from .store import now_iso
from .material_preparation import schedule as schedule_material_preparation
from .material_preparation import stop as stop_material_preparation

log = logging.getLogger(__name__)
_tasks = []
_library_event_writer = None

async def _observe():
    while True:
        session = get_service().status().get("session")
        if session:
            try:
                metadata = await devices.adapter("observe", session["device"])
            except Exception:
                metadata = {"available": False}
            try:
                service = get_service()
                await service.observe(session["device"], metadata, session["id"])
                await service.expire_paused(session["id"])
                current = get_service().status().get("session")
                if current and current["status"] == "playing" and current["song"].get("id"):
                    schedule_material_preparation(current["song"])
            except Exception:
                log.info("音乐状态更新未完成，等待设备重新确认")
        await asyncio.sleep(2)

async def _daily():
    while True:
        try:
            for day in daily.days():
                if day < now_iso()[:10]:
                    try: await daily.run(day)
                    except Exception: log.info("一天的音乐整理未完成，其他日期继续独立处理")
        except Exception:
            log.info("每日音乐整理未完成，保留收据等待重试")
        await asyncio.sleep(3600)

async def _library_events():
    from .event_projection import project_pending
    while True:
        if _library_event_writer is not None:
            try:
                await project_pending(get_service().store, _library_event_writer)
            except Exception:
                log.exception("音乐歌单事件投影未完成，保留来源收据等待重试")
        await asyncio.sleep(3)

async def start(llm, on_library_event=None):
    global _library_event_writer
    if _tasks: return
    _library_event_writer = on_library_event
    configure(devices.adapter)
    store = get_service().store
    if store.material("system:library_event_projection") is None:
        # Existing historical rows include pre-fix no-op track claims.  They
        # remain searchable at their original source, but are not rewritten as
        # new conversation events merely because this projector was installed.
        store.save_material("system:library_event_projection",
                            {"last_id": store.latest_library_event_id()})
    if not store.material("system:activation"):
        store.save_material("system:activation", {"date":now_iso()[:10]})
    daily.configure(llm)
    # Old global mode no longer owns observation or delivery.
    from shared_state import set_music_mode_active
    set_music_mode_active(False)
    _tasks.extend([asyncio.create_task(_observe()), asyncio.create_task(_daily()),
                   asyncio.create_task(_library_events())])

async def stop():
    from .sharing import stop as stop_sharing
    from .materials import stop as stop_materials
    # Stop the observation producer before clearing its enrichment scheduler.
    tasks = list(_tasks)
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    _tasks.clear()
    await stop_sharing()
    await stop_material_preparation()
    await stop_materials()
    try: await devices.stop()
    except Exception: pass
