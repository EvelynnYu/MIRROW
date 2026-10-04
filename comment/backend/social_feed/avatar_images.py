"""Small, metadata-free images for the authenticated public wall."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps, UnidentifiedImageError


AVATAR_DIR = Path(__file__).resolve().parents[1] / 'events' / 'social_wall_avatars'
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000


def avatar_path(actor: str) -> Path:
    return AVATAR_DIR / (hashlib.sha256(actor.encode('utf-8')).hexdigest() + '.png')


def normalized_png(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError('invalid_avatar_image')
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {'PNG', 'JPEG'} or source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError('invalid_avatar_image')
            image = ImageOps.exif_transpose(source)
            image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            clean = Image.new('RGBA', image.size)
            clean.paste(image.convert('RGBA'))
            output = BytesIO()
            clean.save(output, format='PNG', optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('invalid_avatar_image') from exc


def save_avatar(actor: str, raw: bytes) -> Path:
    """Atomically replace one wall-only avatar after stripping image metadata."""
    image = normalized_png(raw)
    target = avatar_path(actor)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix='avatar-', suffix='.tmp', delete=False) as staged:
        staged.write(image)
        staged_path = Path(staged.name)
    try:
        staged_path.replace(target)
    finally:
        staged_path.unlink(missing_ok=True)
    return target
