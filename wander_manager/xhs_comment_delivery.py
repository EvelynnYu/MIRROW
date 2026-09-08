"""Safe, local-only handoff for an XHS comment draft.

The handoff contains a cropped post-home image attachment metadata object and
the draft text.  It never calls a device primitive.  A host (the existing
MIRROW message attachment saver) must be injected explicitly; without one,
the operation reports ``attachment_saver_not_configured`` instead of placing
base64 in SQLite, logs or model context.
"""

from __future__ import annotations

import base64
import binascii
import inspect
from io import BytesIO
from typing import Any, Awaitable, Callable, Mapping

from .chat_attachment_boundary import sanitize_chat_image_metadata


MAX_DRAFT_CHARS = 2000
_ALLOWED_ATTACHMENT_KEYS = ("id", "url", "filename", "size", "type")


def _draft(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_DRAFT_CHARS]


def _decode_image(value: str) -> bytes:
    text = str(value or "")
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=True) if text else b""
    except (ValueError, TypeError, binascii.Error):
        return b""


def _crop_png(raw: bytes, bounds: tuple[int, int, int, int] | None) -> tuple[bytes, bool]:
    """Crop to the selected feed-card bounds when Pillow is available."""

    if not raw or not bounds:
        return raw, False
    try:
        from PIL import Image  # optional dependency; only this helper needs it
        image = Image.open(BytesIO(raw))
        x1, y1, x2, y2 = (max(0, int(value)) for value in bounds)
        x1, x2 = min(x1, image.width), min(x2, image.width)
        y1, y2 = min(y1, image.height), min(y2, image.height)
        if x2 <= x1 or y2 <= y1:
            return raw, False
        output = BytesIO()
        image.crop((x1, y1, x2, y2)).save(output, format="PNG")
        return output.getvalue(), True
    except Exception:
        return raw, False


def _attachment_metadata(value: Any, *, size: int) -> dict[str, Any] | None:
    result = sanitize_chat_image_metadata(value)
    if result is None:
        return None
    result["size"] = int(result.get("size") or size)
    return result


async def prepare_comment_delivery(
    *,
    post_id: str,
    comment_draft: str,
    home_screenshot_base64: str,
    crop_bounds: tuple[int, int, int, int] | None = None,
    attachment_saver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Save one cropped screenshot through the host attachment boundary."""

    draft = _draft(comment_draft)
    if not draft:
        return {"status": "not_requested"}
    if not attachment_saver:
        return {"status": "attachment_saver_not_configured", "comment_draft": draft, "post_id": str(post_id or "")[:160]}
    raw = _decode_image(home_screenshot_base64)
    if not raw:
        return {"status": "home_screenshot_unavailable", "comment_draft": draft, "post_id": str(post_id or "")[:160]}
    cropped, crop_applied = _crop_png(raw, crop_bounds)
    try:
        try:
            saved = attachment_saver(cropped, "xhs-post-home.png", "image/png")
        except TypeError:
            saved = attachment_saver({"data": base64.b64encode(cropped).decode("ascii"), "filename": "xhs-post-home.png", "type": "image/png"})
        if inspect.isawaitable(saved):
            saved = await saved
    except Exception as exc:
        return {"status": "attachment_save_failed", "error": type(exc).__name__, "comment_draft": draft, "post_id": str(post_id or "")[:160]}
    metadata = _attachment_metadata(saved, size=len(cropped))
    if metadata is None:
        return {"status": "attachment_save_invalid", "comment_draft": draft, "post_id": str(post_id or "")[:160]}
    return {
        "status": "ready_for_user",
        "post_id": str(post_id or "")[:160],
        "comment_draft": draft,
        "image": metadata,
        "crop_applied": crop_applied,
        "action": "user_may_review_and_comment_manually",
        "device_interaction": "none",
    }


__all__ = ["MAX_DRAFT_CHARS", "prepare_comment_delivery"]
