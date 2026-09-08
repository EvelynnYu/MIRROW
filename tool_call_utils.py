"""工具调用清洗工具 — 供持久化 / 重建路径复用。"""
import re
from typing import Any, List, Optional
from urllib.parse import urlsplit


_XHS_COMMENT_URL_PREFIX = "/static/chat_images/"
_SAFE_ATTACHMENT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$")


def _safe_xhs_comment_images(value: Any) -> list[dict]:
    """Keep only owned, non-data screenshot metadata for the comment handoff.

    Other tool image lists may contain data URLs and must continue to be
    stripped.  The XHS handoff is the one exception because it intentionally
    uses the existing local chat attachment contract; this local validation
    avoids importing the heavy wander package from the persistence utility.
    """

    if not isinstance(value, list):
        return []
    safe: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        path = urlsplit(url).path
        if not path.startswith(_XHS_COMMENT_URL_PREFIX):
            continue
        filename = path[len(_XHS_COMMENT_URL_PREFIX):]
        if "/" in filename or "\\" in filename or not _SAFE_ATTACHMENT_FILENAME.fullmatch(filename):
            continue
        safe_item = {
            key: item.get(key)
            for key in ("id", "url", "filename", "size", "type")
            if item.get(key) not in (None, "")
        }
        safe_item["url"] = path
        safe_item["filename"] = str(safe_item.get("filename") or filename)[:200]
        safe_item["id"] = str(safe_item.get("id") or "")[:120]
        safe_item["type"] = str(safe_item.get("type") or "image/png")[:80]
        try:
            safe_item["size"] = int(safe_item.get("size") or 0)
        except (TypeError, ValueError):
            safe_item["size"] = 0
        safe.append(safe_item)
    return safe[:4]


def strip_voice_base64(tool_calls: Optional[List[Any]]) -> Optional[List[dict]]:
    """剥离 tool_calls 中每项 extra_data 的大字段（voice_audio_base64 / image_base64），不入存储。

    接受 dict 或带 model_dump 的 Pydantic 对象混合列表，统一返回 dict 列表。
    输入为空 / None 返回 None。
    2026-08-22：增加剥离 image_base64（工具产图后避免 SQL tool_calls 列膨胀，见 phase3 自查 2）。
    """
    if not tool_calls:
        return None
    cleaned = []
    for tc in tool_calls:
        d = tc if isinstance(tc, dict) else tc.model_dump() if hasattr(tc, "model_dump") else {}
        if isinstance(d, dict) and d.get("extra_data"):
            ed = dict(d["extra_data"])
            ed.pop("voice_audio_base64", None)
            ed.pop("image_base64", None)
            # Eyes and other visual tools may carry large data URLs.  Preserve
            # only the XHS manual-comment handoff's already-owned local image
            # metadata so the screenshot survives reload and can be deleted
            # through the normal message ownership path.
            if d.get("tool") == "third_state_capability" and ed.get("xhs_comment") is True:
                images = _safe_xhs_comment_images(ed.get("images"))
                if images:
                    ed["images"] = images
                else:
                    ed.pop("images", None)
            else:
                ed.pop("images", None)  # eyes 三源图列表（含大 base64 data URL）
            d = {**d, "extra_data": ed}
        cleaned.append(d)
    return cleaned


def build_tool_summary(tool_calls: Optional[List[Any]]) -> Optional[str]:
    """生成人类可读工具摘要，如 'web_search(搜索), camera(拍照)'。空返回 None。"""
    if not tool_calls:
        return None
    parts = []
    for tc in tool_calls:
        d = tc if isinstance(tc, dict) else tc.model_dump() if hasattr(tc, "model_dump") else {}
        name = d.get("tool") or d.get("name", "?")
        desc = str(d.get("description") or d.get("desc", "") or "")[:30]
        parts.append(f"{name}({desc})" if desc else name)
    return ", ".join(parts) if parts else None


def build_voice_marker(tool_calls: Optional[List[Any]]) -> str:
    """从 tool_calls 提取 send_voice 的 voice_text，构建存储描述占位 '[语音消息]（内容）'。

    对齐图片存储描述模式（'[图片消息]（描述）'）：AI 语音的 assistant 消息 content 为空，
    注入此占位后 SQL 非空，上下文注入/日记/记忆管道自然可见 AI 说过什么。
    无 send_voice 或无 voice_text 返回空串（调用方保持原 content 不变）。
    """
    if not tool_calls:
        return ""
    for tc in tool_calls:
        d = tc if isinstance(tc, dict) else tc.model_dump() if hasattr(tc, "model_dump") else {}
        if d.get("tool") == "send_voice":
            ed = d.get("extra_data") or {}
            vt = ed.get("voice_text") or (d.get("parameters") or {}).get("text", "")
            return f"[语音消息]（{vt}）" if vt else ""
    return ""


def apply_voice_marker(content: str, marker: str) -> str:
    """给 assistant 消息 content 前缀注入语音描述占位（有文字回复则换行拼接）。"""
    if not marker:
        return content
    content = (content or "").strip()
    return marker if not content else f"{marker}\n{content}"
