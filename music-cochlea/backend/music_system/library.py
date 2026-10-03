"""K's playlist shelf supplies explicit candidates and recoverable collection receipts."""
from .service import get_service
from .materials import prepare


async def sync_selected(selections):
    """Read the remote catalogue, then bind only explicit choices, atomically."""
    from .errors import MusicNotFound
    service = get_service()
    ids = [p["id"] for p in selections]
    if not ids or len(ids) != len(set(ids)):
        raise MusicNotFound("请选择歌单，且不要重复选择")
    items = {p["id"]: p for p in await service.provider.playlists()}
    if any(pid not in items for pid in ids):
        raise MusicNotFound("部分歌单已不在账号列表，请重新读取后选择")
    previous = {item["id"] for item in service.store.bindings()}
    resolved = [{**p, "name": items[p["id"]]["name"]} for p in selections]
    service.store.bind_playlists(resolved)
    for item in resolved:
        if item["id"] not in previous:
            service.store.library_event(
                "bound", item["id"], item["subject"], {"name": item["name"], "source": "sync"}
            )
    return service.store.bindings()

async def create(name, subject, privacy=True, *, source="ui", reason="", node_id=""):
    """One local creation intent per shelf/name; uncertain retries reconcile, never re-create."""
    import hashlib
    from .errors import MusicNotFound
    service = get_service()
    key = "playlist-create:" + hashlib.sha256((subject + ":" + name.strip()).encode()).hexdigest()
    receipt = service.store.material(key)
    if receipt:
        if receipt.get("playlist"):
            return receipt["playlist"]
        found = [p for p in await service.provider.playlists() if p["name"] == name.strip()]
        if len(found) != 1:
            raise MusicNotFound("上次创建未确认，请先在网易云核对并用链接导入，未重复创建")
        item = await service.provider.playlist(found[0]["id"])
    else:
        service.store.save_material(key,{"status":"pending"})
        item = await service.provider.create_playlist(name,privacy)
    service.store.bind_playlist(item["id"],subject,item["name"])
    service.store.save_material("playlist:" + item["id"],item)
    service.store.save_material(key,{"status":"completed","playlist":item})
    service.store.library_event(
        "created", item["id"], subject,
        {"name": item["name"], "privacy": bool(privacy), "source": source,
         "reason": str(reason or "")[:300], "node_id": str(node_id or "")},
    )
    return item

async def rename(playlist_id, name, *, source="ui", reason="", node_id=""):
    """Rename remotely, re-read, then refresh MIRROW's local projections."""
    service = get_service()
    binding = next((item for item in service.store.bindings() if item["id"] == str(playlist_id)), None)
    if not binding:
        from .errors import MusicNotFound
        raise MusicNotFound("歌单尚未放入 MIRROW")
    item = await service.provider.rename_playlist(str(playlist_id), name)
    service.store.bind_playlist(item["id"], binding["subject"], item["name"])
    service.store.save_material("playlist:" + item["id"], item)
    service.store.library_event(
        "renamed", item["id"], binding["subject"],
        {"before": binding["name"], "after": item["name"], "source": source,
         "reason": str(reason or "")[:300], "node_id": str(node_id or "")},
    )
    return item


async def delete(playlist_id, source="ui", *, reason="", node_id=""):
    """Delete one K-owned NetEase playlist after snapshotting its last known contents.

    This is intentionally different from :func:`unbind`: deletion affects the
    provider account, so only the ``k`` shelf is eligible and an active shared
    queue can never be deleted underneath the playback authority.
    """
    from .errors import MusicNotFound
    service = get_service()
    playlist_id = str(playlist_id)
    receipt_key = "playlist-delete:" + playlist_id
    previous = service.store.material(receipt_key) or {}
    if previous.get("status") == "completed":
        return previous.get("playlist") or {"id": playlist_id, "deleted": True}
    binding = next((item for item in service.store.bindings() if item["id"] == playlist_id), None)
    if not binding:
        raise MusicNotFound("歌单尚未放入 MIRROW")
    if binding["subject"] != "k":
        raise MusicNotFound("K 只能删除自己唱片架里的网易云歌单")
    active = service.status().get("session")
    if active and str(active.get("playlist_id") or "") == playlist_id:
        raise MusicNotFound("这张歌单正在共同播放，请先结束一起听再删除")
    if previous.get("status") == "pending":
        try:
            still_present = any(
                str(item.get("id") or "") == playlist_id
                for item in await service.provider.playlists()
            )
        except Exception as error:
            raise MusicNotFound("上次删除结果尚未确认，请联网核对后再试") from error
        if not still_present:
            snapshot = previous.get("playlist") or {"id": playlist_id, "name": binding["name"], "songs": []}
            completed = {**snapshot, "deleted": True}
            service.store.unbind_playlist(playlist_id)
            service.store.save_material(receipt_key, {
                "status": "completed", "playlist": completed,
                "snapshot": snapshot, "source": previous.get("source") or source,
            })
            service.store.library_event(
                "deleted", playlist_id, "k",
                {"name": binding["name"], "track_count": len(snapshot.get("songs") or []),
                 "source": previous.get("source") or source, "reconciled": True,
                 "reason": str(reason or "")[:300], "node_id": str(node_id or "")},
            )
            return completed
    try:
        snapshot = await service.provider.playlist(playlist_id)
    except Exception:
        snapshot = service.store.material("playlist:" + playlist_id) or {
            "id": playlist_id, "name": binding["name"], "songs": [],
        }
    service.store.save_material(receipt_key, {
        "status": "pending", "playlist": snapshot, "source": source,
    })
    try:
        removed = await service.provider.delete_playlist(playlist_id)
    except Exception as error:
        service.store.save_material(receipt_key, {
            "status": "error", "playlist": snapshot, "source": source,
            "error": type(error).__name__,
        })
        raise
    service.store.unbind_playlist(playlist_id)
    service.store.save_material(receipt_key, {
        "status": "completed", "playlist": {**removed, "deleted": True},
        "snapshot": snapshot, "source": source,
    })
    service.store.library_event(
        "deleted", playlist_id, "k",
        {"name": binding["name"], "track_count": len(snapshot.get("songs") or []),
         "source": source, "reason": str(reason or "")[:300], "node_id": str(node_id or "")},
    )
    return {**removed, "deleted": True}


async def change_tracks(playlist_id, song_ids, operation, source="ui", *, reason="", node_id="", song=None):
    """Mutate one bound playlist and refresh every local projection together."""
    from .errors import MusicNotFound
    service = get_service()
    playlist_id = str(playlist_id)
    binding = next((item for item in service.store.bindings() if item["id"] == playlist_id), None)
    if not binding:
        raise MusicNotFound("歌单尚未放入 MIRROW")
    if operation not in {"add", "remove"}:
        raise MusicNotFound("不支持的歌单操作")
    normalized = [str(song_id) for song_id in song_ids if str(song_id)]
    if not normalized:
        raise MusicNotFound("请选择歌曲")
    item = await service.provider.change_tracks(playlist_id, normalized, operation)
    changed_ids = item.pop("_changed_song_ids", None)
    if changed_ids is None:
        # Older adapters do not expose a before/after receipt.  Keep their
        # compatibility result, but never claim an independently verified change.
        changed_ids = []
    service.store.save_material("playlist:" + playlist_id, item)
    service.store.bind_playlist(playlist_id, binding["subject"], item.get("name") or binding["name"])
    if changed_ids:
        track = song if isinstance(song, dict) and str(song.get("id") or "") in changed_ids else {}
        service.store.library_event(
            "tracks_added" if operation == "add" else "tracks_removed",
            playlist_id, binding["subject"],
            {"name": item.get("name") or binding["name"], "song_ids": changed_ids,
             "song_title": str(track.get("name") or "")[:160],
             "song_artist": str(track.get("artist") or "")[:120],
             "source": source, "reason": str(reason or "")[:300],
             "node_id": str(node_id or ""), "verified_change": True},
        )
    return {**item, "change_applied": bool(changed_ids)}


def set_wander_default(playlist_id, selected=True):
    from .errors import MusicNotFound
    service = get_service()
    try:
        binding = service.store.set_wander_default(str(playlist_id), bool(selected))
    except ValueError as error:
        raise MusicNotFound("只有 K 的唱片架可以设为漫想默认收藏处") from error
    service.store.library_event(
        "wander_default_changed", binding["id"], binding["subject"],
        {"selected": bool(selected), "name": binding["name"]},
    )
    return binding


def unbind(playlist_id):
    """Remove a local shelf projection without touching the NetEase account."""
    from .errors import MusicNotFound
    service = get_service()
    playlist_id = str(playlist_id)
    binding = next((item for item in service.store.bindings() if item["id"] == playlist_id), None)
    if not binding:
        raise MusicNotFound("歌单尚未放入 MIRROW")
    if not service.store.unbind_playlist(playlist_id):
        raise MusicNotFound("歌单尚未放入 MIRROW")
    service.store.library_event(
        "unbound", playlist_id, binding["subject"], {"name": binding["name"]}
    )
    return binding

async def candidates():
    """All explicitly bound playlists are readable music choices; ownership stays attached."""
    service = get_service()
    result_by_id = {}
    for binding in service.store.bindings():
        if binding["subject"] not in {"owner", "k", "shared"}: continue
        cached = service.store.material("playlist:" + binding["id"])
        if not cached:
            try:
                cached = await service.provider.playlist(binding["id"])
                service.store.save_material("playlist:" + binding["id"],cached)
            except Exception: continue
        for song in cached.get("songs", []):
            sid = str(song["id"])
            membership = {"id": binding["id"], "name": binding["name"],
                          "subject": binding["subject"]}
            existing = result_by_id.get(sid)
            if existing:
                existing["playlist_names"].append(binding["name"])
                existing["playlist_ids"].append(binding["id"])
                existing["playlist_memberships"].append(membership)
                continue
            result_by_id[sid] = {
                "title": song["name"], "artist": song["artist"],
                "fingerprint": "netease:" + sid, "netease_song_id": sid,
                "duration_sec": song["duration"] / 1000, "music_shelf": True,
                "source": "MIRROW 已绑定的网易云歌单",
                "playlist_names": [binding["name"]], "playlist_ids": [binding["id"]],
                "playlist_memberships": [membership],
            }
    return list(result_by_id.values())


async def wander_catalog(max_playlists=8, max_songs=8):
    """Bounded factual catalogue for a listen-music node review."""
    service = get_service()
    result = []
    for binding in [item for item in service.store.bindings()
                    if item["subject"] in {"k", "shared"}][:max_playlists]:
        cached = service.store.material("playlist:" + binding["id"])
        if not cached:
            try:
                cached = await service.provider.playlist(binding["id"])
                service.store.save_material("playlist:" + binding["id"], cached)
            except Exception:
                cached = {"songs": []}
        songs = [
            {"id": str(song.get("id") or ""), "name": str(song.get("name") or ""),
             "artist": str(song.get("artist") or "")}
            for song in (cached.get("songs") or [])[:max_songs]
            if song.get("id")
        ]
        result.append({
            "id": binding["id"], "name": binding["name"], "subject": binding["subject"],
            "wander_default": bool(binding.get("wander_default")),
            "track_count": len(cached.get("songs") or []), "songs": songs,
        })
    return result


async def apply_wander_action(node_id, payload, action, reason=""):
    """Execute one idempotent K/shared-library intention for a music node."""
    from .errors import MusicNotFound
    service = get_service()
    key = "wander-library-action:" + str(node_id)
    existing = service.store.material(key)
    if existing and existing.get("status") in {"completed", "error", "rejected"}:
        return existing
    value = dict(action or {})
    kind = str(value.get("action") or "").strip()
    allowed = {
        "create_playlist", "rename_playlist", "delete_playlist",
        "add_current_song", "remove_song", "set_default_playlist",
    }
    if kind not in allowed:
        receipt = {"status": "rejected", "action": kind, "reason": "不支持的歌单行动"}
        service.store.save_material(key, receipt)
        return receipt
    playlist_id = str(value.get("playlist_id") or "")
    name = str(value.get("name") or "").strip()[:100]
    song_id = str(value.get("song_id") or "")
    if kind == "add_current_song":
        song_id = str((payload or {}).get("netease_song_id") or "")
    reason = str(value.get("reason") or reason or "")[:300]
    receipt = {
        "status": "pending", "action": kind, "playlist_id": playlist_id,
        "song_id": song_id, "name": name, "reason": str(reason or "")[:300],
    }
    service.store.save_material(key, receipt)
    try:
        if kind == "create_playlist":
            if not name:
                raise MusicNotFound("新建 K 歌单需要名字")
            item = await create(name, "k", privacy=True, source="wander", reason=reason, node_id=node_id)
            playlist_id = item["id"]
            receipt["playlist_subject"] = "k"
        else:
            binding = next((item for item in service.store.bindings()
                            if item["id"] == playlist_id), None)
            if not binding:
                raise MusicNotFound("目标歌单未绑定到 MIRROW")
            if binding["subject"] == "owner":
                raise MusicNotFound("漫想不能自主编辑使用者的个人歌单")
            if binding["subject"] == "shared" and kind in {"delete_playlist", "set_default_playlist"}:
                raise MusicNotFound("共有歌单可编辑曲目和名字，不可自主删除或设为 K 的默认收藏处")
            receipt["playlist_subject"] = binding["subject"]
            if kind == "rename_playlist":
                if not name:
                    raise MusicNotFound("歌单新名字不能为空")
                item = await rename(playlist_id, name, source="wander", reason=reason, node_id=node_id)
            elif kind == "delete_playlist":
                item = await delete(playlist_id, source="wander", reason=reason, node_id=node_id)
            elif kind in {"add_current_song", "remove_song"}:
                if not song_id or not song_id.isdigit():
                    raise MusicNotFound("歌单行动缺少可核验的歌曲 ID")
                if kind == "add_current_song" and playlist_id in {
                    str(value) for value in (payload or {}).get("playlist_ids") or []
                }:
                    receipt.update(
                        status="completed", playlist_name=binding["name"],
                        changed=False, outcome="already_present_at_selection",
                    )
                    service.store.save_material(key, receipt)
                    return receipt
                item = await change_tracks(
                    playlist_id, [song_id],
                    "add" if kind == "add_current_song" else "remove",
                    source="wander", reason=reason, node_id=node_id,
                    song={"id": song_id, "name": (payload or {}).get("title"),
                          "artist": (payload or {}).get("artist")},
                )
            else:
                item = set_wander_default(playlist_id, True)
        receipt.update(
            status="completed", playlist_id=str(item.get("id") or playlist_id),
            playlist_name=str(item.get("name") or name),
        )
        if kind in {"add_current_song", "remove_song"}:
            receipt["changed"] = bool(item.get("change_applied"))
            if kind == "add_current_song" and not receipt["changed"]:
                receipt["outcome"] = "already_present_after_provider_read"
    except Exception as error:
        receipt.update(status="error", error=str(error)[:300])
    service.store.save_material(key, receipt)
    return receipt

async def material_for_candidate(song):
    service = get_service()
    actual = await service.provider.song(song["netease_song_id"])
    material = await prepare(actual)
    return {
        **song,
        "lyrics_snippet": material.get("lyrics_excerpt") or material.get("lyrics", ""),
        "melody_summary": material.get("melody_summary", ""),
        "duration_sec": (actual.get("duration") or 0) / 1000,
    }

async def collect(node_id, payload, reason):
    service = get_service()
    receipt = service.store.material("collection:" + node_id)
    if receipt and receipt.get("status") == "completed": return receipt
    sid = payload.get("netease_song_id")
    targets = [p for p in service.store.bindings() if p["subject"] == "k"]
    preferred = [p for p in targets if p.get("wander_default")]
    target = preferred[0] if len(preferred) == 1 else (targets[0] if len(targets) == 1 else None)
    if not sid or not target:
        receipt = {"status":"pending","reason":"需要指定一张 K 的漫想默认歌单","song_id":sid}
    else:
        try:
            receipt = await apply_wander_action(
                node_id, payload,
                {"action": "add_current_song", "playlist_id": target["id"]},
                reason,
            )
        except Exception:
            receipt = {"status":"error","reason":"网易云未确认收藏，歌曲感想已保留","song_id":sid}
    service.store.save_material("collection:" + node_id,receipt)
    return receipt
