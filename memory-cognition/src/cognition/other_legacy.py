"""Read-only bridge, not a migration. Existing lifecycle remains authoritative."""
import json
from pathlib import Path

from . import books
from .other_book import context, effective, resolve_interlocutors


def feedback_records():
    state_path = books.ROOT / "data" / "persona_evolution_state.json"
    items_path = books.ROOT / "data" / "persona_suggestions.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    items = json.loads(items_path.read_text(encoding="utf-8")) if items_path.exists() else []
    if not isinstance(state, dict) or not isinstance(items, list):
        raise books.BookError("旧互动反馈无法读取，未当作空白")
    active = {str(e["id"]): e for e in state.get("active_evolutions", [])}
    result = []
    for item in items:
        rid = str(item.get("id", ""))
        lifespan = active.get(rid, {})
        current = rid in active and item.get("status") == "applied" and effective({"expires_at": lifespan.get("expires_at", "")})
        result.append({"origin": "legacy_feedback", "id": rid,
                       "name": item.get("target_section", "互动反馈"),
                       "body": item.get("proposed_text") or item.get("suggested_change") or "",
                       "active": current, "status": item.get("status", "unknown"),
                       "expires_at": lifespan.get("expires_at", ""), "subject_id": "human",
                       "scope": "relationship", "owner_id": "agent",
                       "source_revision": books.revision(json.dumps({"item": item, "lifespan": lifespan}, sort_keys=True, ensure_ascii=False))})
    return result


async def profile_records():
    if books.ROOT != Path(__file__).resolve().parents[1]:
        return []
    from ombre_brain_client import get_ob_client
    # get_profile() has a legacy lazy write-back; metadata lookup is read-only.
    _, meta = await get_ob_client()._get_profile_bucket()
    profile = meta.get("profile", {})
    return [{"origin": "legacy_profile", "id": books.revision(str(key)), "name": str(key),
             "body": str(value), "owner_id": "agent", "subject_id": "human", "scope": "待判定",
             "source_revision": books.revision(json.dumps({str(key): value}, ensure_ascii=False, sort_keys=True))}
            for key, value in profile.items()]


def core_context(interlocutor_ids=None, default_interlocutors=None, user_profile="", include_profile=False, inspection=None):
    selected = resolve_interlocutors(interlocutor_ids, default_interlocutors)
    parts = [context(core=True, interlocutor_ids=selected, inspection=inspection)]
    from .retirement import completed
    retired = completed()
    if inspection is not None:
        inspection['legacy_retired'] = retired
        inspection['legacy_profile_enabled'] = bool(include_profile)
    if retired:
        return parts[0]
    if "human" in selected:
        # The old profile is deliberately marked unclassified until a reviewed
        # migration, not silently relabelled as Agent's abstract understanding.
        if include_profile and user_profile:
            parts.append("Agent 关于人类伙伴的旧画像兼容材料（尚未区分长期认识与具体事实，原文）：\n" + user_profile)
        try:
            for record in feedback_records():
                if record["active"] and record["body"]:
                    parts.append("Agent—人类伙伴的旧关系反馈（人类伙伴此前确认；当前有效；截止：" +
                                 (record["expires_at"] or "未设截止日期") + "）：\n" +
                                 "\n".join("> " + line for line in record["body"].splitlines()))
        except Exception:
            parts.append("旧关系反馈本轮读取失败，当前有效内容未取得。")
    return "\n\n".join(p for p in parts if p)
