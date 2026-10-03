"""HTTP surface for the music centre.  Registered by the application composition root."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .errors import MusicSystemError
from .service import get_service

router = APIRouter(prefix="/api/music/v2", tags=["music"])


class ResolveBody(BaseModel):
    text: str = Field(max_length=2048)


class PlaylistImportBody(ResolveBody):
    subject: Literal["owner", "k", "shared"]


class PlaylistCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    subject: Literal["owner", "k", "shared"]
    privacy: bool = False


class PlaylistSelection(BaseModel):
    id: str = Field(pattern=r"^[0-9]{1,20}$")
    subject: Literal["owner", "k", "shared"]


class PlaylistSyncBody(BaseModel):
    selections: list[PlaylistSelection] = Field(min_length=1, max_length=500)


class PlaylistSubjectBody(BaseModel):
    subject: Literal["owner", "k", "shared"]


class PlaylistDefaultBody(BaseModel):
    selected: bool = True


class PlaylistRenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PlaylistTracksBody(BaseModel):
    song_ids: list[str] = Field(min_length=1, max_length=100)
    operation: Literal["add", "remove"]


class PlayBody(BaseModel):
    song_id: str | None = None
    playlist_id: str | None = None
    device: Literal["mobile", "computer"]
    mode: Literal["single", "list", "loop", "one"] = "single"
    subject: Literal["owner", "k", "shared"] = "shared"


class MaterialBody(BaseModel):
    song_id: str = Field(pattern=r"^[0-9]{1,20}$")


class ControlBody(BaseModel):
    action: Literal["pause", "resume", "next", "end"]
    session_id: str


class SessionPatch(BaseModel):
    session_id: str
    quiet: bool | None = None
    mode: Literal["single", "list", "loop", "one"] | None = None
    pause_timeout_seconds: int | None = Field(default=None, ge=0, le=86400)
    follow_external: bool | None = None


def _error(error: MusicSystemError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


@router.get("/status")
async def status():
    return get_service().status()


@router.get("/cards/{generation_id}")
async def card_status(generation_id: str):
    from .sharing import status
    try:
        return await status(generation_id)
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/history")
async def history():
    from .daily import history
    return history()


class DailyBody(BaseModel):
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


@router.post("/daily/retry")
async def retry_daily(body: DailyBody):
    from .daily import run
    try:
        return await run(body.date)
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=200)):
    try:
        return {"songs": await get_service().search_songs(q)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/shared-songs")
async def shared_songs(q: str = Query(default="", max_length=200), limit: int = Query(default=100, ge=1, le=100)):
    return {"songs": get_service().store.search_shared_songs(q, limit)}


@router.post("/material")
async def song_material(body: MaterialBody):
    try:
        from .materials import prepare, project, schedule_analysis, analysis_pending
        song = await get_service().provider.song(body.song_id)
        material = await prepare(song)
        pending = schedule_analysis(song)
        return {"song": project(song, material), "analysis_pending": pending or analysis_pending(body.song_id)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/material/{song_id}")
async def material_status(song_id: str):
    if not song_id.isdigit() or len(song_id) > 20:
        raise HTTPException(status_code=400, detail="歌曲 ID 无效")
    from .materials import analysis_pending, project
    material = get_service().store.material(song_id) or {}
    return {
        "song": project({"id": song_id, "name": "", "artist": "", "duration": 0}, material),
        "analysis_pending": analysis_pending(song_id),
    }


@router.post("/resolve")
async def resolve(body: ResolveBody):
    try:
        return await get_service().provider.resolve(body.text)
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/playlists/import")
async def import_playlist(body: PlaylistImportBody):
    try:
        resolved = await get_service().provider.resolve(body.text)
        if resolved["kind"] != "playlist":
            raise HTTPException(status_code=400, detail="请提供网易云歌单链接")
        playlist = resolved["playlist"]
        service = get_service()
        existed = any(item["id"] == str(playlist["id"]) for item in service.store.bindings())
        binding = service.store.bind_playlist(playlist["id"], body.subject, playlist["name"])
        service.store.save_material("playlist:" + playlist["id"], playlist)
        if not existed:
            service.store.library_event(
                "bound", playlist["id"], body.subject,
                {"name": playlist["name"], "source": "link"},
            )
        return {"playlist": playlist, "binding": binding}
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/playlists")
async def playlists():
    try:
        return {"playlists": await get_service().provider.playlists(), "bindings": get_service().store.bindings()}
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/playlists/{playlist_id}")
async def playlist(playlist_id: str):
    try:
        service = get_service()
        item = await service.provider.playlist(playlist_id)
        service.store.save_material("playlist:" + str(playlist_id), item)
        return {"playlist": item, "songs": item.get("songs", [])}
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/playlists/sync")
async def sync_playlists(body: PlaylistSyncBody):
    try:
        from .library import sync_selected
        return {"playlists": await sync_selected([p.model_dump() for p in body.selections])}
    except MusicSystemError as error:
        raise _error(error) from error


@router.patch("/playlists/{playlist_id}/binding")
async def change_binding(playlist_id: str, body: PlaylistSubjectBody):
    service = get_service()
    old = next((p for p in service.store.bindings() if p["id"] == playlist_id), None)
    if not old:
        raise HTTPException(404, "歌单尚未放入 MIRROW")
    binding = service.store.bind_playlist(playlist_id, body.subject, old["name"])
    service.store.library_event(
        "binding_changed", playlist_id, body.subject,
        {"name": old["name"], "before": old["subject"], "after": body.subject},
    )
    return {"binding": binding}


@router.delete("/playlists/{playlist_id}/binding")
async def remove_binding(playlist_id: str):
    try:
        from .library import unbind
        return {"removed": unbind(playlist_id)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(playlist_id: str):
    try:
        from .library import delete
        return {"playlist": await delete(playlist_id, source="ui")}
    except MusicSystemError as error:
        raise _error(error) from error


@router.patch("/playlists/{playlist_id}/wander-default")
async def change_wander_default(playlist_id: str, body: PlaylistDefaultBody):
    try:
        from .library import set_wander_default
        return {"binding": set_wander_default(playlist_id, body.selected)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.patch("/playlists/{playlist_id}")
async def rename_playlist(playlist_id: str, body: PlaylistRenameBody):
    try:
        from .library import rename
        return {"playlist": await rename(playlist_id, body.name)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/account/login")
async def login():
    try:
        return await get_service().provider.login_start()
    except MusicSystemError as error:
        raise _error(error) from error


@router.get("/account/login")
async def login_status():
    try:
        return await get_service().provider.login_check()
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/playlists")
async def create_playlist(body: PlaylistCreateBody):
    try:
        from .library import create
        item = await create(body.name, body.subject, body.privacy)
        binding = next(
            p for p in get_service().store.bindings() if p["id"] == str(item["id"])
        )
        return {"playlist": item, "binding": binding}
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/playlists/{playlist_id}/tracks")
async def change_tracks(playlist_id: str, body: PlaylistTracksBody):
    try:
        from .library import change_tracks as mutate_tracks
        item = await mutate_tracks(playlist_id, body.song_ids, body.operation, source="ui")
        return {"playlist": item, "songs": item.get("songs", [])}
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/play")
async def play(body: PlayBody):
    try:
        if bool(body.song_id) == bool(body.playlist_id):
            raise HTTPException(status_code=400, detail="请选择一首歌或一张歌单")
        service = get_service()
        if body.song_id:
            song = await service.provider.song(body.song_id)
            queue = [song]
        else:
            item = await service.provider.playlist(body.playlist_id or "")
            queue = item.get("songs") or []
            if not queue:
                raise HTTPException(status_code=400, detail="这个歌单没有可播放歌曲")
            song = queue[0]
        return {"session": await service.start_song(
            song, body.device, body.mode, queue, body.subject, playlist_id=body.playlist_id
        )}
    except MusicSystemError as error:
        raise _error(error) from error


@router.post("/control")
async def control(body: ControlBody):
    try:
        return {"session": await get_service().control(body.action, body.session_id)}
    except MusicSystemError as error:
        raise _error(error) from error


@router.patch("/session")
async def patch_session(body: SessionPatch):
    try:
        return {"session": await get_service().update_session(
            body.session_id, body.quiet, body.mode, body.pause_timeout_seconds,
            body.follow_external,
        )}
    except MusicSystemError as error:
        raise _error(error) from error
