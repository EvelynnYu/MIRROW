"""Agent-owned understanding of other entities. Names never establish identity."""
import json
import uuid
import re
from datetime import datetime

from . import books

SCOPES = {"person": "长期人物认识", "relationship": "Agent 与对方的关系"}
BASES = {"self_report": "对方自述", "observed": "Agent 的观察", "inferred": "Agent 的理解／推断", "mutual_agreement": "双方明确共识"}


def entities():
    result = {"human": {"id": "human", "name": "人类伙伴", "type": "human"},
              "peer": {"id": "peer", "name": "Peer", "type": "silicon"}}
    path = books.ROOT / "data" / "other_book_entities.json"
    if path.exists():
        extra = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(extra, dict) or any(k in books.SUBJECTS for k in extra):
            raise books.BookError("实体登记损坏，未使用不确定身份")
        result.update(extra)
    return result


def register(name, entity_type, aliases=None):
    if not isinstance(name, str) or not name.strip() or name.strip().casefold() in {"agent", "镜影", "agent和人类伙伴", "agent与人类伙伴", "双方", "我们的关系", "agent 与人类伙伴"} or (isinstance(name, str) and re.search(r"(?:Agent|人类伙伴)\s*(?:和|与|及|&).*(?:Agent|人类伙伴)", name, re.I)):
        raise books.BookError("请使用实体名称；Agent 是认知主体，镜影不是固定实体名")
    if entity_type not in {"human", "silicon", "other"}:
        raise books.BookError("实体类型无效")
    aliases = aliases or []
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise books.BookError("实体别名无效")
    with books.LOCK:
        from .entity_names import resolve_name, validate_aliases
        known = entities()
        if resolve_name(name, known):
            raise books.BookError("名称已登记，请选择已有实体；不同实体请使用可区分的名称")
        aliases = validate_aliases(name, aliases, known)
        path = books.ROOT / "data" / "other_book_entities.json"
        extra = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        rid = "entity_" + uuid.uuid4().hex[:16]
        extra[rid] = {"id": rid, "name": name.strip(), "type": entity_type, "aliases": list(dict.fromkeys(a.strip() for a in aliases if a.strip() and a.strip() != '镜影'))}
        books.atomic_text(path, json.dumps(extra, ensure_ascii=False, indent=2))
        return extra[rid]


def metadata(data, previous, subject, knower):
    if subject not in entities() or knower != "agent" or data.get("owner_id", "agent") != "agent":
        raise books.BookError("他者书归属于 Agent，描述对象必须是已登记的其他实体")
    scope = data.get("scope", previous.get("scope", "person"))
    basis = data.get("basis", previous.get("basis", "inferred"))
    state = data.get("state", previous.get("state", "active"))
    expires = data.get("expires_at", previous.get("expires_at", "")) or ""
    if scope not in SCOPES or basis not in BASES or state not in {"active", "inactive"}:
        raise books.BookError("人物／关系范围、认识依据或生效状态无效")
    if expires:
        try:
            datetime.fromisoformat(expires)
        except (TypeError, ValueError):
            raise books.BookError("有效期格式无效") from None
    if data.get("kind", previous.get("kind", "interpretation")) not in {"preference", "interpretation", "agreement"}:
        raise books.BookError("具体事实属于世界书；他者书保存长期认识、偏好与关系约定")
    return {"owner_id": "agent", "scope": scope, "basis": basis, "state": state, "expires_at": expires}


def effective(entry):
    if entry.get("state", "active") != "active":
        return False
    expires = entry.get("expires_at")
    if not expires:
        return True
    try:
        limit = datetime.fromisoformat(expires)
        now = datetime.now(limit.tzinfo) if limit.tzinfo else datetime.now()
        return now < limit
    except (TypeError, ValueError):
        return False


def resolve_interlocutors(interlocutor_ids=None, default_interlocutors=None):
    selected = default_interlocutors if interlocutor_ids is None else interlocutor_ids
    selected = ["human"] if selected is None else selected
    if not isinstance(selected, (list, tuple)) or any(not isinstance(x, str) for x in selected):
        raise books.BookError("交互对象必须使用实体编号列表")
    known = entities()
    return list(dict.fromkeys(x for x in selected if x in known))


def context(*, core=False, interlocutor_ids=None, default_interlocutors=None, user_message="", context_query="", memories=None, inspection=None):
    selected = resolve_interlocutors(interlocutor_ids, default_interlocutors)
    names = entities()
    from .other_retrieval import selected_entries
    # Entity admission is based on the current utterance and the actual
    # interlocutor only.  Recalled event text may mention many people; letting
    # it pick cognition subjects made unrelated profiles leak into private chat.
    query = str(context_query or user_message or "")
    retrieved = selected_entries(query, selected, expand_entities=False) if not core else None
    if retrieved is not None:
        selected = list(dict.fromkeys([*selected, *(e["subject_id"] for e in retrieved)]))
    parts = []
    if inspection is not None:
        inspection.update({'selected_entities': selected, 'core_only': bool(core), 'entities': []})
    for sid in selected:
        entity = names[sid]
        if entity.get('aliases') or entity.get('preferred_name'):
            aliases = [str(alias).strip() for alias in entity.get('aliases', []) if str(alias).strip()]
            preferred = str(entity.get('preferred_name', '') or '').strip()
            naming = [f"已登记称呼：{entity['name']}"]
            if preferred and preferred != entity['name']:
                naming.append(f"常用称呼：{preferred}")
            if aliases:
                naming.append(f"其他称呼：{'、'.join(aliases)}")
            parts.append('；'.join(naming))
        items = [e for e in (retrieved if retrieved is not None else books.catalog("other")) if e["subject_id"] == sid and e["core"] == core and effective(e)]
        items.sort(key=lambda e: (-sum(bool(t) and t.casefold() in query for t in [e["name"], *e["keywords"]]), e["name"]))
        canonical = [e for e in items if e["name"] == names[sid]["name"]]
        chosen = canonical or (items if core else items[:3])
        if inspection is not None:
            inspection['entities'].append({'entity_id': sid, 'eligible_entries': len(items), 'selected_entries': len(chosen), 'has_naming_metadata': bool(entity.get('aliases') or entity.get('preferred_name'))})
        for e in chosen:
            from .other_facets import project
            from . import other_retrieval
            cached = other_retrieval._facet_vectors.get(e['name'])
            vectors = cached[1] if cached and cached[0] == e['revision'] else None
            body = project(e, '' if core else query, other_retrieval._embedder, vectors)
            parts.append(f"Agent 对 {names[sid]['name']} 的主观认识：{body}")
    return "\n\n".join(parts)
