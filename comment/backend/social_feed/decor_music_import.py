"""Owner-requested NetEase import, separate from device playback and music memory.

Only complete, verified audio becomes a media candidate. Import never saves the
home design, starts a player, or sends account credentials to the media CDN.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from lounge_visits.network_guard import pin_request, public_destination
from .decor_media import MAX_UPLOAD, audio_bytes, persist_media
from .decor_store import DecorError

_IMPORT_LOCK = asyncio.Semaphore(1)


def song_id(source: str) -> str:
    text = str(source or '').strip()
    if re.fullmatch(r'[0-9]{1,20}', text):
        return text
    try:
        url = urlsplit(text)
        fragment = urlsplit(url.fragment)
        if (url.scheme not in {'http', 'https'} or url.hostname not in {'music.163.com', 'y.music.163.com'}
                or url.username or url.password or url.port not in {None, 80, 443}):
            raise ValueError()
        if url.path.rstrip('/') != '/song' and fragment.path.rstrip('/') != '/song':
            raise ValueError()
        values = parse_qs(url.query).get('id') or parse_qs(fragment.query).get('id') or []
        if len(values) != 1 or not re.fullmatch(r'[0-9]{1,20}', values[0]):
            raise ValueError()
        return values[0]
    except ValueError:
        raise DecorError('invalid_import_song') from None


def media_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        if (url.scheme not in {'http', 'https'} or url.username or url.password
                or url.port not in {None, 443} or url.fragment
                or not (host == 'music.126.net' or host.endswith('.music.126.net'))):
            raise ValueError()
        # CDN credentials are never forwarded; HTTP provider links use TLS.
        return urlunsplit(('https', host, url.path, url.query, ''))
    except (TypeError, ValueError):
        raise DecorError('music_import_unavailable') from None


async def track_source(identifier: str, provider=None) -> dict:
    if provider is None:
        try:
            from music_system.service import get_service
            provider = get_service().provider
        except (ImportError, AttributeError):
            raise DecorError('music_import_not_configured') from None

    def read():
        from pyncm.apis import track
        return (track.GetTrackDetail([int(identifier)]),
                track.GetTrackAudio([int(identifier)], bitrate=128000, encodeType='mp3'))

    try:
        # Reuse v2's bounded, scoped account session, not the legacy global login.
        details, response = await provider._call(read)
        song = next(row for row in details.get('songs', []) if str(row.get('id')) == identifier)
        audio = next(row for row in response.get('data', []) if str(row.get('id')) == identifier)
        duration = int(song.get('dt') or song.get('duration') or 0)
        if response.get('code') != 200 or audio.get('code') != 200 or not audio.get('url'):
            raise DecorError('music_import_unavailable')
        if audio.get('freeTrialInfo') is not None:
            raise DecorError('music_import_preview_only')
        if duration <= 0 or duration > 600000:
            raise DecorError('music_import_duration_unsupported')
        if audio.get('time') is not None and int(audio['time']) < duration - 2000:
            raise DecorError('music_import_preview_only')
        if int(audio.get('size') or 0) > MAX_UPLOAD:
            raise DecorError('media_too_large')
        return {'id': identifier, 'title': str(song.get('name') or '未命名歌曲')[:200],
                'artist': ' / '.join(str(a.get('name') or '') for a in song.get('ar', []))[:200],
                'duration_ms': duration, 'url': media_url(audio['url'])}
    except DecorError:
        raise
    except Exception:
        # Provider exceptions can contain cookies/URLs. They never leave here.
        raise DecorError('music_import_unavailable') from None


async def download_audio(url: str, *, client_factory=httpx.AsyncClient, resolve=public_destination) -> bytes:
    for _ in range(4):
        url = media_url(url)
        expected = urlsplit(url)
        try:
            address = await resolve(url)
            async def pin(request):
                pin_request(request, expected, address)
            async with client_factory(timeout=15, follow_redirects=False, trust_env=False,
                                      event_hooks={'request': [pin]}) as client:
                async with client.stream('GET', url, headers={'Accept-Encoding': 'identity'}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get('location')
                        if not location:
                            raise DecorError('music_import_unavailable')
                        # The transport URL is IP-pinned; relative redirects
                        # belong to the original validated CDN origin.
                        url = str(httpx.URL(url).join(location))
                        continue
                    if response.status_code != 200:
                        raise DecorError('music_import_unavailable')
                    if int(response.headers.get('content-length') or 0) > MAX_UPLOAD:
                        raise DecorError('media_too_large')
                    data = bytearray()
                    async for part in response.aiter_bytes(65536):
                        data.extend(part)
                        if len(data) > MAX_UPLOAD:
                            raise DecorError('media_too_large')
                    if not data:
                        raise DecorError('music_import_unavailable')
                    return bytes(data)
        except DecorError:
            raise
        except (ValueError, OSError, httpx.HTTPError):
            raise DecorError('music_import_unavailable') from None
    raise DecorError('music_import_unavailable')


def verify_duration(raw: bytes, expected_ms: int) -> float:
    executable = shutil.which('ffprobe')
    if not executable:
        raise DecorError('audio_probe_unavailable')
    with TemporaryDirectory(prefix='mirrow-music-check-') as folder:
        source = Path(folder) / 'input'
        source.write_bytes(raw)
        try:
            result = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            seconds = float(result.stdout.strip())
            if result.returncode or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            raise DecorError('invalid_decor_audio') from None
        if abs(seconds * 1000 - expected_ms) > 2000 or seconds > 600:
            raise DecorError('music_import_incomplete')
        return seconds


@dataclass(frozen=True)
class ImportedMusic:
    data: bytes
    track: dict
    duration_seconds: float


async def prepare_music(source: str, *, provider=None) -> ImportedMusic:
    try:
        async with asyncio.timeout(90):
            facts = await track_source(song_id(source), provider)
            raw = await download_audio(facts['url'])
            await asyncio.to_thread(verify_duration, raw, facts['duration_ms'])
            converted, _, _ = await asyncio.to_thread(audio_bytes, raw)
            seconds = await asyncio.to_thread(verify_duration, converted, facts['duration_ms'])
            return ImportedMusic(converted, {key: facts[key] for key in ('id', 'title', 'artist')}, seconds)
    except TimeoutError:
        raise DecorError('music_import_timeout') from None


async def import_music(store, source: str) -> dict:
    if _IMPORT_LOCK.locked():
        raise DecorError('music_import_busy')
    async with _IMPORT_LOCK:
        result = await prepare_music(source)
        saved = await asyncio.to_thread(persist_media, store, 'aning', result.data, 'mp3', 'audio/mpeg')
        return saved | {'track': result.track, 'duration_seconds': result.duration_seconds}
