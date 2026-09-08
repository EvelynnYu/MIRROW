"""
截图/快照本地存储

- 保存到 captures/ 目录
- 自动清理超过 30 天的文件
"""

from __future__ import annotations

import os
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_CAPTURES_DIR: str | None = None
_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 天


def _get_captures_dir() -> str:
    global _CAPTURES_DIR
    if _CAPTURES_DIR is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _CAPTURES_DIR = os.path.join(base, "captures")
    os.makedirs(_CAPTURES_DIR, exist_ok=True)
    return _CAPTURES_DIR


def _cleanup_old_files():
    """删除超过 30 天的文件"""
    now = time.time()
    captures_dir = _get_captures_dir()
    deleted = 0
    try:
        for fname in os.listdir(captures_dir):
            fpath = os.path.join(captures_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if now - os.path.getmtime(fpath) > _MAX_AGE_SECONDS:
                os.remove(fpath)
                deleted += 1
        if deleted:
            logger.info(f"Capture cleanup: removed {deleted} expired files")
    except Exception as e:
        logger.warning(f"Capture cleanup error: {e}")


def save_and_get_path(prefix: str, image_bytes: bytes) -> str:
    """
    保存图片到 captures/ 目录，返回绝对路径。
    每次保存时触发一次过期清理。

    Args:
        prefix: 文件名前缀 (camera / screenshot)
        image_bytes: PNG 图片字节
    """
    _cleanup_old_files()

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    fname = f"{prefix}_{timestamp}.png"
    fpath = os.path.join(_get_captures_dir(), fname)

    with open(fpath, "wb") as f:
        f.write(image_bytes)

    logger.info(f"Capture saved: {fpath}")
    return fpath
