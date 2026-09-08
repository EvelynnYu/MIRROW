"""Ownership checks for files referenced by MIRROW chat image metadata."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


_CHAT_URL_PREFIX = "/static/chat_images/"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$")
_ALLOWED_KEYS = ("id", "url", "filename", "size", "type")


def sanitize_chat_image_metadata(value: Any) -> dict[str, Any] | None:
    """Return only the existing frontend image contract, without data URLs."""
    if not isinstance(value, Mapping):
        return None
    result = {key: value.get(key) for key in _ALLOWED_KEYS if value.get(key) not in (None, "")}
    url = str(result.get("url") or "")
    if not url.startswith(_CHAT_URL_PREFIX):
        return None
    path = urlsplit(url).path
    filename = path[len(_CHAT_URL_PREFIX):]
    if "/" in filename or "\\" in filename or not _SAFE_FILENAME.fullmatch(filename):
        return None
    result["url"] = path
    result["filename"] = str(result.get("filename") or filename)[:200]
    result["id"] = str(result.get("id") or "")[:120]
    result["type"] = str(result.get("type") or "image/png")[:80]
    try:
        result["size"] = int(result.get("size") or 0)
    except (TypeError, ValueError):
        result["size"] = 0
    return result


def delete_owned_chat_attachments(images: Iterable[Any] | None, root: str | Path) -> int:
    """Delete only files proven to belong to ``static/chat_images``.

    Message deletion is the ownership boundary.  URLs from another static
    directory, absolute paths, traversal paths and malformed metadata are
    ignored, so a stale or forged message cannot make this helper delete an
    unrelated file.
    """
    root_path = Path(root)
    try:
        root_resolved = root_path.resolve()
    except OSError:
        return 0
    deleted = 0
    for item in images or ():
        metadata = sanitize_chat_image_metadata(item)
        if not metadata:
            continue
        filename = Path(urlsplit(metadata["url"]).path).name
        target = root_resolved / filename
        try:
            if target.resolve().parent != root_resolved or not target.is_file():
                continue
            target.unlink()
            deleted += 1
        except (OSError, ValueError):
            continue
    return deleted


__all__ = ["delete_owned_chat_attachments", "sanitize_chat_image_metadata"]
