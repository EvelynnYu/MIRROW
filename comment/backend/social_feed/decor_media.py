"""Bounded uploaded media; sanitized frames/audio, content addressed storage."""
import hashlib
import io
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile

from PIL import Image, ImageOps, UnidentifiedImageError
from .decor_store import DecorError

MAX_UPLOAD = 10 * 1024 * 1024


def image_bytes(raw):
    if not raw or len(raw) > MAX_UPLOAD:
        raise DecorError('media_too_large')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            count = getattr(source, 'n_frames', 1)
            if source.format not in {'PNG','JPEG','WEBP','GIF'} or source.width * source.height > 16_000_000 or count > 120 or source.width * source.height * count > 64_000_000:
                raise DecorError('image_dimensions_exceeded')
            frames, durations = [], []
            for index in range(count):
                source.seek(index)
                frame = ImageOps.exif_transpose(source).convert('RGBA')
                frame.thumbnail((1600, 1600) if count == 1 else (700, 700))
                clean = Image.new('RGBA', frame.size); clean.paste(frame)
                frames.append(clean)
                durations.append(max(40, min(2000, int(source.info.get('duration',100)))))
            output = io.BytesIO()
            if count > 1:
                frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2)
                extension, mime = 'gif','image/gif'
            else:
                frames[0].save(output, format='PNG', optimize=True)
                extension, mime = 'png','image/png'
            if output.tell() > MAX_UPLOAD:
                raise DecorError('media_too_large')
            return output.getvalue(), extension, mime
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        if isinstance(exc, DecorError):
            raise
        raise DecorError('invalid_decor_image') from None


def audio_bytes(raw):
    if not raw or len(raw) > MAX_UPLOAD:
        raise DecorError('media_too_large')
    if raw[:4] == b'RIFF' and raw[8:12] == b'WAVE':
        input_format = 'wav'
    elif raw[:3] == b'ID3' or len(raw)>1 and raw[0]==255 and raw[1]&224==224:
        input_format = 'mp3'
    else:
        raise DecorError('invalid_decor_audio')
    executable = shutil.which('ffmpeg')
    if not executable:
        raise DecorError('audio_converter_unavailable')
    # No filenames or external protocols supplied by the client are executed.
    with TemporaryDirectory(prefix='mirrow-music-') as folder:
        source, target = Path(folder)/'input', Path(folder)/'output.mp3'
        source.write_bytes(raw)
        try:
            result = subprocess.run([executable, '-v','error','-nostdin','-protocol_whitelist','file,pipe',
                '-f',input_format,'-i',str(source),'-map','0:a:0','-vn','-t','600','-map_metadata','-1',
                '-c:a','libmp3lame','-b:a','128k',str(target)], capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode or not target.exists() or target.stat().st_size > MAX_UPLOAD:
                raise DecorError('invalid_decor_audio')
            return target.read_bytes(), 'mp3','audio/mpeg'
        except (subprocess.TimeoutExpired, OSError):
            raise DecorError('invalid_decor_audio') from None


def save_media(store, actor, raw, audio=False):
    data, ext, mime = audio_bytes(raw) if audio else image_bytes(raw)
    return persist_media(store,actor,data,ext,mime)


def persist_media(store, actor, data, ext, mime):
    """Internal sanitized result only (upload decoder or bounded GIF builder)."""
    if not data or len(data)>MAX_UPLOAD:
        raise DecorError('media_too_large')
    identifier = hashlib.sha256(actor.encode()+b'\0'+data).hexdigest()+'.'+ext
    store.media_dir.mkdir(parents=True, exist_ok=True)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT COALESCE(SUM(size),0) FROM assets WHERE owner=?',(actor,)).fetchone()[0]
        if used + len(data) > 200 * 1024 * 1024 and not db.execute('SELECT 1 FROM assets WHERE id=?',(identifier,)).fetchone():
            raise DecorError('media_quota_exceeded')
        with NamedTemporaryFile(dir=store.media_dir, delete=False) as temp:
            temp.write(data); temp_path = Path(temp.name)
        try:
            temp_path.replace(store.media_dir / identifier)
        finally:
            temp_path.unlink(missing_ok=True)
        db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?)',(identifier, actor, mime, len(data)))
    return {'id':identifier,'mime':mime,'size':len(data)}
