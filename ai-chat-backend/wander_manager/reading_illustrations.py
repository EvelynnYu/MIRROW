"""Read-only projection of existing illustrations anchored to actually read text."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


def bind(choice, items):
    if not isinstance(choice, dict) or not all(isinstance(choice.get(key), str) and choice[key].strip() for key in ("quote", "scene")):
        return None
    quote, scene, matches = choice["quote"].strip(), choice["scene"].strip(), []
    for item in items:
        if item.get("source") != "essays":
            continue
        content = str(item.get("content") or "")
        at = content.find(quote)
        if at >= 0 and content.find(quote, at + 1) < 0:
            matches.append({"source": "essays", "work_id": item["id"], "title": item["title"], "quote": quote,
                            "start": at, "end": at + len(quote), "scene": scene})
        elif at >= 0:
            return {"status": "unmatched"}
    return matches[0] if len(matches) == 1 else {"status": "unmatched"}


def entry(node):
    card = (node.get("side_effect_refs") or {}).get("reading_illustration") or {}
    if node.get("execution_status") != "succeeded" or not isinstance(card, dict) or card.get("status") != "ready" or card.get("source") != "essays":
        return None
    url = str(card.get("url") or "")
    if not re.fullmatch(r"/static/essay_illustrations/[0-9a-f]{32}\.(?:png|jpe?g|webp)", url):
        return None
    start, end, quote = card.get("start"), card.get("end"), card.get("quote")
    if not isinstance(quote, str) or not quote or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end - start != len(quote):
        return None
    return {"status": "ready", "node_id": node["node_id"], "source": "essays",
            "work_id": str(card.get("work_id") or ""), "title": str(card.get("title") or "")[:500],
            "quote": quote, "start": start, "end": end, "scene": str(card.get("scene") or "")[:500], "url": url}


def for_work(source, work_id, db_path=None):
    if source != "essays":
        return []
    path = Path(db_path) if db_path else Path(__file__).resolve().parents[1] / "events" / "wander_runtime.db"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""SELECT n.node_id,json_extract(n.side_effect_refs,'$.reading_illustration')
            FROM wander_nodes n JOIN wander_activities a ON n.activity_id=a.activity_id
            WHERE a.activity_type='browse_bookmarks' AND n.execution_status='succeeded'
            AND json_extract(n.side_effect_refs,'$.reading_illustration.status')='ready'
            AND json_extract(n.side_effect_refs,'$.reading_illustration.source')=?
            AND json_extract(n.side_effect_refs,'$.reading_illustration.work_id')=? ORDER BY n.created_at,n.node_id""", (source, work_id))
        return [card for node_id, data in rows if (card := entry({"node_id": node_id, "execution_status": "succeeded", "side_effect_refs": {"reading_illustration": json.loads(data)}}))]


def fact(card):
    chapter = f' · {card["chapter_title"]}' if card.get('chapter_title') else ''
    return f'《{card["title"]}》{chapter}的读后插画，原句「{card["quote"]}」，构思是「{card["scene"]}」。'


def choices(nodes):
    return '\n'.join(f'{c["node_id"]} · {fact(c)}' for n in nodes if (c := entry(n)))


def share_option(nodes):
    return '\n' + SHARE if any(entry(n) for n in nodes) else ''

SHARE = '想随消息送出一张读后插画时，可在收尾填写 reading_picture_node_id 和 share=true。'

OPTION = '可用 illustration 描述想画的场景，quote 必须是这次确实读到的随笔原句，scene 描述画面。'
