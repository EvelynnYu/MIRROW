"""Prepare a visual transmission copy; originals and attachment facts stay intact."""
import base64
import binascii
import io
import logging
import math

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

_BATCH_PNG_BYTES = 2 * 1024 * 1024


def prepare_image_data_url(value: str, *, compact_png: bool = False) -> str:
    """Bound photos, retain lossless screenshot detail, never fetch remote URLs."""
    if not isinstance(value, str) or not value.startswith('data:image/') or ';base64,' not in value:
        return value
    try:
        header, encoded = value.split(';base64,', 1)
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            fmt = source.format
            # Animated/multipage and unsupported formats retain their existing path.
            if fmt not in {'JPEG', 'PNG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1:
                return value
            alpha = 'A' in source.getbands() or 'transparency' in source.info
            lossless = fmt == 'PNG' or alpha
            if compact_png and fmt == 'PNG' and len(raw) >= _BATCH_PNG_BYTES:
                # Phone exports often carry a fully opaque alpha channel.
                lossless = alpha and source.convert('RGBA').getchannel('A').getextrema()[0] < 255
            edge, pixels = (4096, 8_000_000) if lossless else (2048, 4_000_000)
            original_size = source.size
            scale = min(1.0, edge / max(source.size), math.sqrt(pixels / (source.width * source.height)))
            orientation = source.getexif().get(274, 1)
            mime = {'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}[fmt]
            if scale == 1 and orientation == 1 and (lossless or len(raw) <= 1_000_000):
                return value if header == 'data:' + mime else 'data:' + mime + ';base64,' + encoded
            image = ImageOps.exif_transpose(source)
            target = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            if target != image.size:
                image = image.resize(target, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if lossless:
                image.save(output, format='PNG', optimize=True)
                output_mime = 'image/png'
            else:
                image.convert('RGB').save(output, format='JPEG', quality=88, optimize=True)
                output_mime = 'image/jpeg'
            prepared = output.getvalue()
            # Recompression alone must not inflate the payload.
            if target == original_size and orientation == 1 and len(prepared) >= len(raw):
                return 'data:' + mime + ';base64,' + encoded
            logger.info('Vision transmission copy: %s %sx%s %s bytes -> %s %sx%s %s bytes',
                        fmt, *original_size, len(raw), output_mime, *image.size, len(prepared))
            return 'data:' + output_mime + ';base64,' + base64.b64encode(prepared).decode('ascii')
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        logger.warning('Vision transmission copy unchanged: %s', type(exc).__name__)
        return value
