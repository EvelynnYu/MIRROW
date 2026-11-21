"""Persistent, read-only public essay catalogue backed by Wander nodes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SEED_PROMPT = '让一个此刻突然想到的词自然浮现，与当前状态有关或无关都可以。只输出 JSON：{"keyword":"词"}。'
WRITING_PROMPT = '''随手写一篇此刻想写的随笔，心得、感想、短篇小说或幻想都可以，题材与文体自由，500 字以内为宜。浮现的词只是一点灵感，也可以写别的。
只输出 JSON，title 写题目，expansion 写完整正文。写完若有想画的一幕，可在 illustration 里用一句话描述画面。'''


async def generate(call, exclude_keywords=()):
    from .flash_structured import parse_json_object
    excluded = {str(value).strip() for value in exclude_keywords or () if str(value).strip()}
    seed = parse_json_object(await call(SEED_PROMPT + ("\n本次已出现的词：" + "、".join(sorted(excluded)) if excluded else ""))) or {}
    keyword = seed.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip() or keyword.strip() in excluded:
        return None
    keyword = keyword.strip()
    data = parse_json_object(await call(WRITING_PROMPT + "\n这一次脑子里突然冒出来一个词：" + keyword)) or {}
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("title", "expansion")):
        return None
    work = {"essay_version": 1, "keyword": keyword, "title": data["title"].strip(),
            "expansion": data["expansion"].strip(), "source_kind": "creative_work"}
    if isinstance(data.get("illustration"), str) and data["illustration"].strip():
        work["illustration_request"] = data["illustration"].strip()
    return work


def entry(node, activity_id=""):
    payload = node.get("source_payload") or {}
    if payload.get("essay_version") != 1 or node.get("execution_status") != "succeeded":
        return None
    if not payload.get("title") or not payload.get("expansion"):
        return None
    illustration = (node.get("side_effect_refs") or {}).get("essay_illustration")
    return {"id": node["node_id"], "activity_id": activity_id or node.get("activity_id", ""),
            "title": payload["title"], "keyword": payload.get("keyword", ""),
            "created_at": node.get("completed_at") or node.get("started_at") or node.get("created_at", ""),
            "content": payload["expansion"], **({"illustration": _public_illustration(illustration)} if illustration else {})}


def _public_illustration(card):
    import re
    if not isinstance(card, dict):
        return {"status": "failed"}
    status = card.get("status")
    if status != "ready":
        return {"status": status if status in ("requested", "interrupted") else "failed"}
    url = str(card.get("url") or "")
    if not re.fullmatch(r"/static/essay_illustrations/[0-9a-f]{32}\.(?:png|jpe?g|webp)", url):
        return {"status": "failed"}
    return {"status": "ready", "scene": str(card.get("scene") or "")[:500], "url": url}


def read(*, event_id="", query="", date="", limit=30, offset=0, db_path=None):
    """Read only; this endpoint never initializes, migrates, or writes a database."""
    path = Path(db_path) if db_path else Path(__file__).resolve().parents[1] / "events" / "wander_runtime.db"
    if not path.is_file():
        return None if event_id else {"items": [], "total": 0}
    clauses = ["a.activity_type='keyword_expansion'", "n.execution_status='succeeded'",
               "json_extract(n.source_payload,'$.essay_version')=1"]
    params = []
    if event_id:
        clauses.append("n.node_id=?"); params.append(event_id)
    if date:
        clauses.append("substr(COALESCE(n.completed_at,n.started_at,n.created_at),1,10)=?"); params.append(date)
    if query:
        clauses.append("(instr(json_extract(n.source_payload,'$.title'),?)>0 OR instr(json_extract(n.source_payload,'$.expansion'),?)>0 OR instr(json_extract(n.source_payload,'$.keyword'),?)>0)")
        params.extend([query] * 3)
    base = " FROM wander_nodes n JOIN wander_activities a ON a.activity_id=n.activity_id WHERE " + " AND ".join(clauses)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT count(*)" + base, params).fetchone()[0]
        rows = db.execute("SELECT n.*" + base + " ORDER BY COALESCE(n.completed_at,n.started_at,n.created_at) DESC,n.node_id LIMIT ? OFFSET ?", [*params, 1 if event_id else max(1, min(100, int(limit))), max(0, int(offset))]).fetchall()
    items = []
    for row in rows:
        node = dict(row); node["source_payload"] = json.loads(node["source_payload"]); node["side_effect_refs"] = json.loads(node["side_effect_refs"] or "{}")
        item = entry(node)
        if item:
            if not event_id:
                item.pop("content"); item.pop("illustration", None)
            items.append(item)
    return (items[0] if items else None) if event_id else {"items": items, "total": total}


def card_text(card):
    return f'[AI 分享的随笔作品｜《{card.get("title", "")}》]\n{card.get("content", "")}'


def selected_card(store, run_id, *, field="essay_share_node_id", make_card=entry):
    from .runtime_models import DecisionPhase
    activities = {row["activity_id"]: row for row in store.list_activities(run_id)}
    settled = set()
    for decision in reversed(store.list_decisions(run_id, DecisionPhase.SETTLEMENT)):
        activity_id = decision.get("activity_id")
        if activity_id in settled:
            continue
        settled.add(activity_id)
        activity = activities.get(activity_id)
        if not activity or not activity.get("share_decision"):
            continue
        node_id = ((decision.get("parsed_output") or {}).get("normalized") or {}).get(field)
        for node in store.list_nodes(activity_id) if node_id else ():
            if node.get("node_id") == node_id and (card := make_card(node)):
                return card
    return None
