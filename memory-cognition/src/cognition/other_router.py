"""Agent's other-book editor and non-mutating migration/context previews."""
from fastapi import APIRouter, HTTPException

from . import books, other_book, other_legacy
from .router import safe_call

router = APIRouter(prefix="/api/cognition/other", tags=["cognition"])


@router.get("")
def listing():
    from .entity_names import revision
    entities = safe_call(other_book.entities)
    return {"owner_id": "agent", "entities": {sid:{**e, 'revision':revision(e)} for sid,e in entities.items()},
            "entries": safe_call(books.catalog, "other"), "scopes": other_book.SCOPES, "bases": other_book.BASES,
            "migration_state": "旧数据仍在原存储；这里只读兼容与预览，尚未迁移或删除"}


@router.post("/entities")
def register(data: dict):
    return safe_call(other_book.register, data.get("name"), data.get("type"), data.get('aliases'))


@router.put('/entities/{identifier}/names')
def update_names(identifier: str, data: dict):
    from .entity_names import update
    return safe_call(update, identifier, data)


@router.get("/legacy-preview")
async def legacy():
    try:
        profile = await other_legacy.profile_records()
        feedback = other_legacy.feedback_records()
    except Exception:
        raise HTTPException(503, "旧数据预览读取失败，未迁移或删除任何内容") from None
    return {"profile": profile, "feedback": feedback, "read_only": True,
            "note": "具体生活字段拟归世界书；跨场景长期认识拟归他者书；逐项归属尚待确认。当前没有迁移执行操作。"}


@router.post("/context-preview")
async def preview(data: dict):
    selected = safe_call(other_book.resolve_interlocutors, data.get("interlocutor_ids", []))
    # Same participant selector as the real ingredient; this never changes the
    # actual conversation or registers a visitor.
    legacy_profile = ""
    if "human" in selected:
        try:
            legacy_profile = "\n".join(f"{r['name']}: {r['body']}" for r in await other_legacy.profile_records())
        except Exception:
            legacy_profile = "旧画像本轮读取失败，资料未取得。"
    return {"owner_id": "agent", "interlocutor_ids": selected, "preview_only": True,
            "core": safe_call(other_legacy.core_context, interlocutor_ids=selected, user_profile=legacy_profile, include_profile=True),
            "dynamic": safe_call(other_book.context, interlocutor_ids=selected, user_message=str(data.get("query", ""))),
            "note": "仅模拟选材，不改变当前聊天对象；会客室尚未接入。未知实体不会回退成人类伙伴。"}
