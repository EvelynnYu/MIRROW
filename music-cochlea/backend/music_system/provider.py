"""NetEase metadata/account boundary. Scoped pyncm sessions never replace its global session."""
from __future__ import annotations
import asyncio
import base64
import io
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import httpx
from .errors import AccountRequired, MusicNotFound, MusicSystemError

_HOSTS = {"music.163.com", "y.music.163.com", "163cn.tv"}
_ID = re.compile(r"^[0-9]{1,20}$")

def valid_id(value):
    if not _ID.fullmatch(str(value or "")):
        raise MusicNotFound("歌曲或歌单 ID 无效")
    return str(value)

def song_view(raw):
    album = raw.get("al") or raw.get("album") or {}
    artists = raw.get("ar") or raw.get("artists") or []
    artist = raw.get("artist") or " / ".join(str(x.get("name", "")) if isinstance(x, dict) else str(x) for x in artists)
    sid = valid_id(raw.get("id"))
    name = str(raw.get("name") or "")
    translations = [str(t) for t in (raw.get("tns") or raw.get("transNames") or []) if isinstance(t, str) and t.strip()]
    aliases = [name + " (" + t + ")" for t in translations[:10]]
    return {"id": sid, "name": str(raw.get("name") or ""), "artist": str(artist or "未知"),
            "album": str(album.get("name", "")) if isinstance(album, dict) else str(album),
            "cover": str(raw.get("cover") or (album.get("picUrl", "") if isinstance(album, dict) else "")),
            "title_aliases": aliases,
            "duration": int(raw.get("duration") or raw.get("dt") or 0), "link": f"https://music.163.com/song?id={sid}"}

def _url(value):
    p = urlparse(value)
    if p.scheme not in {"http", "https"} or p.hostname not in _HOSTS or p.username or p.password or p.port not in {None, 80, 443}:
        raise MusicNotFound("只支持网易云音乐的标准歌曲和歌单链接")
    return p

class NetEaseProvider:
    def __init__(self, cookie_path=None):
        self.cookie_path = Path(cookie_path) if cookie_path else Path(__file__).resolve().parents[2] / "data" / "music_cookies.json"
        self._lock = threading.RLock()
        self._session = None
        self._uid = None
        self._qr = None

    def _ensure_session(self):
        if self._session is None:
            import pyncm
            class BoundedSession(pyncm.Session):
                def request(self, method, url, *args, **kwargs):
                    kwargs.setdefault("timeout", (5, 15))
                    return super().request(method, url, *args, **kwargs)
            self._session = BoundedSession()
            self._session.trust_env = False
            if self.cookie_path.exists():
                saved = json.loads(self.cookie_path.read_text(encoding="utf-8"))
                self._session.cookies.update(saved.get("cookies") or {})

    async def _call(self, function, *args):
        def scoped():
            with self._lock:
                self._ensure_session()
                with self._session:
                    return function(*args)
        try:
            return await asyncio.to_thread(scoped)
        except MusicSystemError:
            raise
        except Exception:
            raise MusicNotFound("网易云暂未完成请求，请检查网络或重新授权后重试") from None

    def _account(self):
        from pyncm.apis import login
        result = login.GetCurrentLoginStatus()
        account = (result.get("data") or result).get("account") or {}
        uid = account.get("id")
        if not uid or not self._session.cookies.get("MUSIC_U"):
            self._uid = None
            raise AccountRequired("网易云账号尚未授权或已失效，请在音乐中枢扫码登录")
        self._uid = int(uid)
        return self._uid

    async def search(self, query, limit=12):
        if not query.strip() or len(query) > 200: return []
        def run():
            from pyncm.apis import cloudsearch
            r = cloudsearch.GetSearchResult(query.strip(), limit=min(limit, 30))
            if r.get("code") != 200: raise MusicNotFound("网易云搜索暂不可用")
            return [song_view(x) for x in (r.get("result") or {}).get("songs", [])]
        return await self._call(run)

    async def song(self, song_id):
        sid = valid_id(song_id)
        def run():
            from pyncm.apis import track
            songs = track.GetTrackDetail([int(sid)]).get("songs") or []
            if not songs: raise MusicNotFound("没有找到这首歌")
            return song_view(songs[0])
        return await self._call(run)

    async def resolve(self, text):
        if len(text or "") > 2048: raise MusicNotFound("链接过长")
        links = re.findall(r"https?://[^\s<>\u3000]+", text or "")
        if not links: raise MusicNotFound("请粘贴网易云歌曲或歌单链接")
        value = links[0].rstrip('）)。,，；;！!」】')
        _url(value)
        target = self._link_target(value)
        if not target: target = self._link_target(await self._restricted_follow(value))
        if not target: raise MusicNotFound("未识别到歌曲或歌单 ID")
        kind, sid = target
        return {"kind": kind, kind: await (self.song(sid) if kind == "song" else self.playlist(sid))}

    @staticmethod
    def _link_target(value):
        p = _url(value)
        frag = urlparse(p.fragment)
        path = p.path + "/" + frag.path
        kind = "playlist" if re.search(r"(?:^|/)playlist(?:/|$)", path) else "song" if re.search(r"(?:^|/)song(?:/|$)", path) else None
        sid = (parse_qs(p.query).get("id") or parse_qs(frag.query).get("id") or [""])[0]
        if kind and _ID.fullmatch(sid): return kind, sid
        return None

    async def _restricted_follow(self, value):
        try:
            async with asyncio.timeout(18):
                async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                    for _ in range(5):
                        _url(value)
                        async with client.stream("GET", value) as r:
                            if r.status_code in {301,302,303,307,308}:
                                value = str(r.url.join(r.headers.get("location", "")))
                                continue
                            # Bounded body inspection for NetEase's own short-link landing pages.
                            body = bytearray()
                            async for chunk in r.aiter_bytes(4096):
                                body.extend(chunk)
                                if len(body) >= 16384: break
                            for candidate in re.findall(r"https?://[^\s\"<>]+", body.decode("utf-8", errors="ignore")):
                                try:
                                    if self._link_target(candidate): return candidate
                                except MusicNotFound: pass
                            return str(r.url)
        except MusicSystemError: raise
        except Exception: raise MusicNotFound("网易云短链接解析失败，请改用完整歌曲链接") from None
        raise MusicNotFound("链接跳转次数过多")

    def _playlist(self, playlist_id):
        from pyncm.apis import playlist, track
        raw = playlist.GetPlaylistInfo(int(valid_id(playlist_id))).get("playlist")
        if not raw: raise MusicNotFound("歌单不可读取，私密歌单需要授权")
        ids = [x["id"] for x in raw.get("trackIds", [])]
        songs = []
        for offset in range(0, len(ids), 500):
            songs.extend(track.GetTrackDetail(ids[offset:offset+500]).get("songs") or [])
        if len(songs) != len(ids): raise MusicNotFound("歌单歌曲未完整读取，请稍后重试")
        item = self._playlist_view(raw)
        item["songs"] = [song_view(x) for x in songs] if ids else [song_view(x) for x in raw.get("tracks", [])]
        return item

    async def playlist(self, playlist_id):
        valid_id(playlist_id)
        return await self._call(self._playlist, playlist_id)

    @staticmethod
    def _playlist_view(raw):
        pid = valid_id(raw.get("id"))
        return {"id": pid, "name": str(raw.get("name") or ""), "cover": str(raw.get("coverImgUrl") or ""),
                "track_count": int(raw.get("trackCount") or 0), "privacy": raw.get("privacy"),
                "link": f"https://music.163.com/playlist?id={pid}"}

    async def playlists(self):
        def run():
            from pyncm.apis import user
            uid = self._account()
            items, offset = [], 0
            while True:
                result = user.GetUserPlaylists(uid, offset=offset, limit=100)
                if result.get("code") != 200: raise MusicNotFound("网易云未返回账号歌单")
                batch = result.get("playlist") or []
                for raw in batch:
                    owner = raw.get("userId") or (raw.get("creator") or {}).get("userId")
                    items.append({**self._playlist_view(raw), "owned": str(owner or "") == str(uid)})
                if not result.get("more") or not batch: break
                offset += len(batch)
            return items
        return await self._call(run)

    async def create_playlist(self, name, privacy=False):
        if not name.strip() or len(name.strip()) > 100: raise MusicNotFound("歌单名需要 1 到 100 个字符")
        def run():
            self._account()
            from pyncm.apis import playlist
            # NetEase private playlist uses 10; pyncm multiplies this argument by 1.
            r = playlist.SetCreatePlaylist(name.strip(), 10 if privacy else 0)
            raw = r.get("playlist")
            if not raw: raise MusicNotFound("网易云未确认创建，请同步歌单检查后再重试")
            item = self._playlist(raw["id"])
            item["owned"] = True
            if privacy and item.get("privacy") != 10:
                raise MusicNotFound("歌单已创建，但网易云未确认私密属性，请在网易云检查；勿重复新建")
            return item
        return await self._call(run)

    async def delete_playlist(self, playlist_id):
        """Delete one playlist owned by the logged-in account and verify absence."""
        pid = valid_id(playlist_id)
        def run():
            uid = self._account()
            from pyncm.apis import playlist, user

            def account_rows():
                rows, offset = [], 0
                while True:
                    response = user.GetUserPlaylists(uid, offset=offset, limit=100)
                    if response.get("code") != 200:
                        raise MusicNotFound("网易云未返回账号歌单")
                    batch = response.get("playlist") or []
                    rows.extend(batch)
                    if not response.get("more") or not batch:
                        return rows
                    offset += len(batch)

            before = account_rows()
            raw = next((item for item in before if str(item.get("id")) == pid), None)
            if raw is None:
                raise MusicNotFound("这张歌单已不在当前网易云账号中")
            owner = raw.get("userId") or (raw.get("creator") or {}).get("userId")
            if str(owner or "") != str(uid):
                raise MusicNotFound("只能删除 K 自己创建的网易云歌单")
            response = playlist.SetRemovePlaylist([int(pid)])
            if not isinstance(response, dict) or response.get("code") not in {200, 201}:
                raise MusicNotFound("网易云未确认删除歌单")
            if any(str(item.get("id")) == pid for item in account_rows()):
                raise MusicNotFound("网易云仍返回这张歌单，删除尚未确认")
            return {**self._playlist_view(raw), "owned": True}
        return await self._call(run)

    async def rename_playlist(self, playlist_id, name):
        pid = valid_id(playlist_id)
        clean = str(name or "").strip()
        if not clean or len(clean) > 100:
            raise MusicNotFound("歌单名需要 1 到 100 个字符")
        def run():
            self._account()
            from pyncm.apis import WeapiCryptoRequest

            @WeapiCryptoRequest
            def update_name():
                return "/api/playlist/update/name", {"id": pid, "name": clean}

            response = update_name()
            if not isinstance(response, dict) or response.get("code") != 200:
                raise MusicNotFound("网易云未确认歌单改名，请刷新后重试")
            item = self._playlist(pid)
            if item.get("name") != clean:
                raise MusicNotFound("网易云返回的歌单名尚未更新，请刷新核对")
            return item
        return await self._call(run)

    async def change_tracks(self, playlist_id, song_ids, operation):
        valid_id(playlist_id)
        if operation not in {"add","remove"} or not song_ids: raise MusicNotFound("歌单修改参数无效")
        song_ids = [valid_id(x) for x in song_ids]
        def run():
            self._account()
            from pyncm.apis import playlist
            before = self._playlist(playlist_id)
            present = {s["id"] for s in before["songs"]}
            changes = [x for x in song_ids if (x not in present if operation == "add" else x in present)]
            if not changes: return {**before, "_changed_song_ids": []}
            playlist.SetManipulatePlaylistTracks(changes, int(playlist_id), "add" if operation == "add" else "del")
            item = self._playlist(playlist_id)
            actual = {s["id"] for s in item["songs"]}
            if any((x not in actual if operation == "add" else x in actual) for x in changes):
                raise MusicNotFound("网易云尚未确认歌曲修改，请刷新核对后再重试")
            return {**item, "_changed_song_ids": changes}
        return await self._call(run)

    async def login_start(self):
        def run():
            try:
                self._account()
                self._qr = None
                return {"status": "success"}
            except AccountRequired:
                pass
            import qrcode
            from pyncm.apis import login
            result = login.LoginQrcodeUnikey()
            key = result.get("unikey")
            if not key: raise MusicNotFound("无法生成登录二维码")
            self._qr = (key, time.monotonic())
            url = login.GetLoginQRCodeUrl(key)
            buf = io.BytesIO(); qrcode.make(url).save(buf, format="PNG")
            return {"qr_image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "status": "waiting"}
        return await self._call(run)

    async def login_check(self):
        def run():
            from pyncm.apis import login
            if not self._qr:
                try:
                    self._account()
                    return {"status": "success"}
                except AccountRequired:
                    return {"status": "expired"}
            if time.monotonic() - self._qr[1] > 180: return {"status": "expired"}
            result = login.LoginQrcodeCheck(self._qr[0])
            status = {800:"expired",801:"waiting",802:"scanned",803:"success"}.get(result.get("code"),"waiting")
            if status == "success":
                self._account()
                # Save only cookies, never return credentials or provider payloads.
                from cognition.books import atomic_text
                atomic_text(self.cookie_path, json.dumps({"cookies": self._session.cookies.get_dict()}, ensure_ascii=False))
                self._qr = None
            return {"status": status}
        return await self._call(run)
