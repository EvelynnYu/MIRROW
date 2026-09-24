"""First-stage front-end cognitive editing and non-mutating inspection."""
from dataclasses import asdict
import hashlib
import hmac
import json
import secrets
from fastapi import APIRouter, HTTPException

from . import books, music

router = APIRouter(prefix="/api/cognition", tags=["cognition"])
_PREVIEW_KEY = secrets.token_bytes(32)
_PLAN_FIELDS = ("domain", "name", "before", "after", "revision", "suggestion_revision", "subject_id", "knower_id", "kind", "core", "keywords", "aliases")


def preview_signature(plan):
    raw = json.dumps({key: plan.get(key) for key in _PLAN_FIELDS}, sort_keys=True, ensure_ascii=False).encode()
    return hmac.new(_PREVIEW_KEY, raw, hashlib.sha256).hexdigest()


def safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except books.RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except books.BookError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(503, "认知材料暂时不可用；未确认保存成功，请刷新核对") from None


@router.get("/books/{domain}")
def get_books(domain: str):
    from . import unread
    return {"entries": safe_call(unread.decorate, safe_call(books.catalog, domain))}


@router.get('/unread')
def unread_counts():
    from . import unread
    return {'counts': safe_call(unread.counts)}


@router.post('/books/{domain}/{name}/read')
def mark_read(domain: str, name: str, data: dict):
    from . import unread
    return safe_call(unread.mark, domain, name, data.get('revision', ''))


@router.put("/books/{domain}/{name}")
def put_book(domain: str, name: str, data: dict):
    from . import manual
    return {"entry": safe_call(manual.save, domain, name, data)}


@router.delete("/books/{domain}/{name}")
def delete_book(domain: str, name: str, data: dict):
    from . import manual
    return safe_call(manual.delete, domain, name, data.get('revision', ''))


@router.post("/deleted/{rid}/restore")
def restore_book(rid: str):
    from . import manual
    return {'entry': safe_call(manual.restore, rid)}


@router.get("/identity")
def identity():
    return {"subjects": books.SUBJECTS, "relationship": books.MIRROR_FACT}


@router.get("/context-preview")
def context_preview(recipe: str = "FULL_CHAT"):
    from context_builder.recipes import get_recipe
    from context_builder.builder import _STABLE_SECTIONS, _DYNAMIC_LAST_RECIPES, dynamic_last_enabled
    spec = get_recipe(recipe)
    if not spec:
        raise HTTPException(404, "配方不存在")
    dynamic = dynamic_last_enabled() and recipe in _DYNAMIC_LAST_RECIPES
    return {"recipe": recipe, "pipeline": spec.pipeline,
            "preview_only": True, "core": safe_call(books.core_context),
            "sections": [{"name": s.name, "ingredient": s.ingredient, "condition": s.condition,
                          "position": "动态候选（列表型原料按历史时间排列）" if dynamic and s.ingredient not in _STABLE_SECTIONS else "配方位置（列表型原料按历史时间排列）"}
                         for s in spec.sections],
            "has_core": any(s.ingredient == "cognitive_core" for s in spec.sections),
            "note": "只读配方检查，不调用模型，不消费状态；真实阅读顺序以监控台当次调用为准。"}


@router.get("/people/{subject}")
def person(subject: str):
    if subject not in books.SUBJECTS:
        raise HTTPException(422, "主体无效")
    return {"subject": books.SUBJECTS[subject], "entries": safe_call(books.person_projection, subject),
            "note": "来自已标注世界书的只读人物概览；旧永久画像尚未迁移。"}


@router.get("/music")
def get_music():
    return {"records": list(reversed(safe_call(music.records))), "dimensions": music.DIMENSIONS}


@router.post("/music")
def add_music(data: dict):
    # This endpoint records a person's note; it cannot assert a player receipt.
    data = {**data, "mode": "manual_note"}
    return {"record": safe_call(music.record, data)}


def suggestion(sid):
    from world_book.suggestions import SuggestionManager
    mgr = SuggestionManager()
    item = next((s for s in mgr.get_pending() if s.id == sid), None)
    if not item:
        raise HTTPException(404, "建议已处理或不存在")
    return mgr, item


@router.post("/suggestions/{sid}/preview")
def suggestion_preview(sid: str, data: dict):
    _, item = suggestion(sid)
    domain = data.get("domain", "world")
    entries = safe_call(books.catalog, domain)
    name = str(data.get("name") or item.target_entry or item.proposed_name).removesuffix(".md")
    target = next((e for e in entries if e["name"] == name), None)
    mode = data.get("mode", "supplement")
    if mode not in {"supplement", "update"}:
        raise HTTPException(422, "合并方式无效")
    before = target["body"] if target else ""
    proposed = item.proposed_body.strip()
    if not proposed:
        raise HTTPException(422, "建议正文为空，不能采纳")
    after = before if proposed and proposed in before else (before + "\n\n" + proposed).strip() if mode == "supplement" else proposed
    result = {"domain": domain, "name": name, "before": before, "after": after,
            "revision": target["revision"] if target else "new",
            "suggestion_revision": books.revision(str(asdict(item))),
            "candidates": safe_call(books.related_entries, item.proposed_name, item.proposed_keywords, item.proposed_body),
            "subject_id": target["subject_id"] if target else "agent" if domain == "self" else "unknown",
            "knower_id": target["knower_id"] if target else "agent" if domain == "self" else "unknown",
            "kind": target["kind"] if target else "self_reflection" if domain == "self" else "fact",
            "aliases": target["aliases"] if target else [],
            "core": target["core"] if target else False,
            "keywords": list(dict.fromkeys([*(target["keywords"] if target else []), *item.proposed_keywords]))}
    result["preview_token"] = preview_signature(result)
    return result


@router.post("/suggestions/{sid}/apply")
def suggestion_apply(sid: str, data: dict):
    with books.LOCK:
        if not hmac.compare_digest(str(data.get("preview_token", "")), preview_signature(data)):
            raise HTTPException(409, "预览内容已变化或后端已重启，请重新预览")
        mgr, item = suggestion(sid)
        if data.get("suggestion_revision") != books.revision(str(asdict(item))):
            raise HTTPException(409, "建议已编辑，请重新预览")
        committed = safe_call(books.receipt_present, data.get("domain", "world"), data.get("name", ""), sid)
        if data.get("domain") == "world" and data.get("revision") == "new" and not committed:
            candidates = safe_call(books.related_entries, item.proposed_name, item.proposed_keywords, item.proposed_body)
            if candidates and data.get("new_entity_confirmed") is not True:
                raise HTTPException(409, "存在相关实体，请选择已有目标或明确确认这是不同实体")
        payload = {**data, "body": data.get("after", "")}
        entry = safe_call(books.save, data.get("domain", "world"), data.get("name", ""), payload, receipt=sid)
        item.status = "applied"
        safe_call(mgr._archive, item)
        mgr._pending = [s for s in mgr._pending if s.id != sid]
        safe_call(mgr._save)
        return {"entry": entry, "status": "applied"}
