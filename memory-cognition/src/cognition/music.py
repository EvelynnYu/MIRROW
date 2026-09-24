"""Music experiences and attributed taste notes, separate from self-book text."""
import json
from datetime import datetime
from uuid import uuid4

from . import books

DIMENSIONS = ["人声与唱法", "旋律", "节奏与速度", "音色与编配", "和声与结构", "歌词与主题", "情绪", "情境", "反例与不喜欢", "共同意义"]


def path():
    return books.ROOT / "data" / "music_experiences.json"


def records():
    data = json.loads(path().read_text(encoding="utf-8")) if path().exists() else []
    required = {"id", "source_id", "source", "subject_id", "title", "artist", "mode", "reaction", "dimensions", "created_at"}
    if not isinstance(data, list) or any(not isinstance(item, dict) or not required.issubset(item) or not isinstance(item["dimensions"], dict) for item in data):
        raise books.BookError("音乐记录格式异常，未覆盖原文件")
    # Backfilled notes carry their original dates, not the migration date.
    # Ordering by occurrence keeps old imports out of the recent-experience tail.
    def occurrence(item):
        try:
            return datetime.fromisoformat(item['created_at']).timestamp()
        except (TypeError, ValueError):
            return float('-inf')
    return sorted(data, key=occurrence)


def record(data: dict, *, source_id: str = "", source: str = "人类伙伴手动记录"):
    subject = data.get("subject_id", "agent")
    if subject not in {"agent", "human", "shared", "unknown"}:
        raise books.BookError("音乐记录主体无效")
    title = str(data.get("title", "")).strip()
    if not title or title in {"未知", "?"}:
        raise books.BookError("需要可靠的歌曲名称")
    mode = data.get("mode", "manual_note")
    if mode not in {"manual_note", "analysis", "observed_playback"}:
        raise books.BookError("音乐材料类型无效")
    notes = data.get("dimensions", {})
    if not isinstance(notes, dict) or any(k not in DIMENSIONS or not isinstance(v, str) for k, v in notes.items()):
        raise books.BookError("音乐维度无效")
    with books.LOCK:
        items = records()
        key = source_id or "manual:" + uuid4().hex
        old = next((item for item in items if item["source_id"] == key), None)
        if old:
            return old
        item = {"id": uuid4().hex, "source_id": key, "source": source,
                "subject_id": subject, "title": title, "artist": str(data.get("artist", "")),
                "mode": mode, "reaction": str(data.get("reaction", "")),
                "dimensions": {k: v.strip() for k, v in notes.items() if v.strip()},
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        items.append(item)
        books.atomic_text(path(), json.dumps(items, ensure_ascii=False, indent=2))
        return item


def context(subject: str = "agent") -> str:
    if subject == "all":
        return "\n\n".join(part for key in ("agent", "human", "shared", "unknown") if (part := context(key)))
    items = [r for r in records() if r["subject_id"] == subject][-8:]
    if not items:
        return ""
    lines = [f"{books.SUBJECTS[subject]}的近期音乐体验记录（各次体验，不代表稳定偏好）："]
    for item in items:
        notes = "；".join(f"{k}：{v}" for k, v in item["dimensions"].items())
        lines.append(f"{item['created_at']} · {item['title']} · 来源：{item['source']} · 材料：{item['mode']}\n{item['reaction']}\n{notes}")
    return "\n".join(lines)
