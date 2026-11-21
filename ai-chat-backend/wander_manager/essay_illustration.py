"""Optional local illustration adapter; disabled until explicitly configured."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import os
from pathlib import Path
from uuid import uuid4

_DIRECTORY = Path(__file__).resolve().parents[1] / "static" / "essay_illustrations"
_FORMATS = {"PNG": ("png", "PNG"), "JPEG": ("jpg", "JPEG"), "WEBP": ("webp", "WEBP")}


def _adapter():
    target = os.environ.get("MIRROW_ESSAY_IMAGE_ADAPTER", "").strip()
    if not target or target.count(":") != 1:
        return None
    module, name = target.split(":", 1)
    if not module.replace(".", "").isidentifier() or not name.isidentifier():
        return None
    value = getattr(importlib.import_module(module), name, None)
    return value if callable(value) else None


def _normalise(value):
    raw = value.get("data") if isinstance(value, dict) else value
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 20 * 1024 * 1024:
        raise ValueError("adapter_invalid_image")
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in _FORMATS or getattr(image, "is_animated", False):
            raise ValueError("adapter_unsupported_image")
        if image.width < 1 or image.height < 1 or image.width * image.height > 40_000_000:
            raise ValueError("adapter_invalid_dimensions")
        image.load()
        extension, output_format = _FORMATS[image.format]
        canvas = image.convert("RGBA" if output_format == "PNG" else "RGB")
        canvas.info.clear()
        output = io.BytesIO()
        canvas.save(output, format=output_format)
    return extension, output.getvalue()


async def render(request):
    adapter = _adapter()
    if adapter is None:
        raise RuntimeError("essay_image_adapter_unavailable")
    result = adapter(request) if inspect.iscoroutinefunction(adapter) else await asyncio.to_thread(adapter, request)
    if inspect.isawaitable(result):
        result = await result
    extension, data = await asyncio.to_thread(_normalise, result)
    _DIRECTORY.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}.{extension}"
    await asyncio.to_thread((_DIRECTORY / filename).write_bytes, data)
    return {"status": "ready", "scene": request, "url": f"/static/essay_illustrations/{filename}"}


async def attach(store, node_id, request, key="essay_illustration", source=None):
    source = source or {}
    try:
        receipt = await render(request)
    except asyncio.CancelledError:
        store.merge_node_side_effect_refs(node_id, {key: {**source, "status": "interrupted"}})
        raise
    except Exception:
        receipt = {"status": "failed"}
    return store.merge_node_side_effect_refs(node_id, {key: {**source, **receipt}})
