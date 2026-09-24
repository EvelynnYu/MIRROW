"""Markdown remains authoritative; reads never migrate or mutate entries."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import tempfile
import threading
import unicodedata
from datetime import datetime

import frontmatter

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()
DOMAINS = {"self": "self_book", "other": "other_book", "world": "world_book", "scp": "scp_encyclopedia"}
KINDS = {"fact", "self_reflection", "interpretation", "theory", "agreement", "preference"}
SUBJECTS = {"agent": "Agent", "human": "人类伙伴", "peer": "Peer", "shared": "Agent 与人类伙伴", "unknown": "未标注"}
MIRROR_FACT = "Agent 与人类伙伴是两个独立主体，互为镜影。Agent 的镜影是人类伙伴，人类伙伴的镜影是 Agent。Peer 是另一独立主体。"


class BookError(ValueError):
    pass


class RevisionConflict(BookError):
    pass


def subject_catalog():
    from .other_book import entities
    return {**SUBJECTS, **{k: v["name"] for k, v in entities().items()}}


def revision(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def strip_log(body: str) -> str:
    return body.split("## 编辑记录", 1)[0].strip()


def folder(domain: str) -> Path:
    if domain not in DOMAINS:
        raise BookError("未知认知领域")
    return ROOT / DOMAINS[domain] / "entries"


def _read(path: Path, domain: str) -> dict:
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    meta = post.get("cognition", {})
    meta = meta if isinstance(meta, dict) else {}
    name = str(post.get("name", path.stem))
    def terms(key):
        value = post.get(key, [])
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return {
        "name": name, "domain": domain, "body": strip_log(post.content), "body_full": post.content,
        "aliases": terms("aliases"), "keywords": terms("keywords"),
        "category": str(post.get("category", "")), "revision": revision(raw),
        "subject_id": meta.get("subject_id", "agent" if domain == "self" else "unknown"),
        "knower_id": meta.get("knower_id", "agent" if domain == "self" else "unknown"),
        "kind": meta.get("kind", "self_reflection" if domain == "self" else "theory" if domain == "scp" else "fact"),
        "core": meta.get("core") is True,
        "source": meta.get("source", "历史条目，来源未标注"),
        "updated_at": meta.get("updated_at", ""),
        "maintenance_links": post.get("cognition_updates", []),
        "owner_id": meta.get("owner_id", "agent"), "scope": meta.get("scope", "person"),
        "basis": meta.get("basis", "inferred"), "state": meta.get("state", "active"),
        "expires_at": meta.get("expires_at", ""),
        "entry_id": post.get("entry_id", revision(domain + ":" + path.relative_to(folder(domain)).as_posix())[:24]),
    }


def catalog(domain: str) -> list[dict]:
    base = folder(domain)
    if not base.exists():
        return []
    result = []
    for path in sorted(base.rglob("*.md")):
        if not path.resolve().is_relative_to(base.resolve()):
            continue
        result.append(_read(path, domain))
    return result


def locate(domain: str, name: str) -> Path:
    base = folder(domain)
    matches = []
    for path in base.rglob("*.md"):
        if path.resolve().is_relative_to(base.resolve()):
            entry = _read(path, domain)
            if entry["name"] == name:
                matches.append(path)
    if len(matches) != 1:
        raise BookError("词条不存在或名称不唯一，请重新选择")
    return matches[0]


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".cognition-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def reload_domain(domain: str):
    # Avoid importing global application state in isolated tests.
    if ROOT != Path(__file__).resolve().parents[1]:
        return
    if domain == "self":
        from self_book import get_global_self_book
        get_global_self_book().reload()
    elif domain == "world":
        from world_book import get_global_world_book
        get_global_world_book().reload()
    elif domain == "other":
        from .other_retrieval import refresh
        refresh()
    elif domain == "scp":
        from scp_encyclopedia import get_global_scp_encyclopedia
        get_global_scp_encyclopedia().reload()


def receipt_present(domain: str, name: str, receipt: str) -> bool:
    try:
        path = locate(domain, name)
    except BookError:
        return False
    return receipt in frontmatter.load(path).get("cognition_receipts", [])


def validate_name(name):
    if isinstance(name, str) and any(ord(char) < 32 for char in name):
        raise BookError("词条名不能包含换行或控制字符")
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 100 or any(c in name for c in '\\/:*?"<>|') or name in {".", ".."} or name.endswith(".") or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise BookError("词条名无效")


def title_key(name: str) -> str:
    """Formatting-only lookup key, never proof of semantic entity equality."""
    return ''.join(unicodedata.normalize('NFKC', name).casefold().split())


def save(domain: str, name: str, data: dict, *, receipt: str = "", editor: str = "", provenance: dict | None = None) -> dict:
    """CAS revision + local recoverable snapshot; preserve all unrelated YAML."""
    with LOCK:
        create = data.get("revision") == "new"
        if receipt and receipt_present(domain, name, receipt):
            return _read(locate(domain, name), domain)
        if create:
            validate_name(name)
            path = folder(domain) / (name + ".md")
            if path.exists() or any(e["name"] == name or (domain == 'world' and title_key(e['name']) == title_key(name)) for e in catalog(domain)):
                raise RevisionConflict("词条已存在，请刷新后编辑")
            raw, post = "", frontmatter.Post("", name=name)
            if domain == "scp":
                post["granularity"] = "secondary"
        else:
            path = locate(domain, name)
            raw = path.read_text(encoding="utf-8")
            post = frontmatter.loads(raw)
            if receipt and receipt in post.get("cognition_receipts", []):
                return _read(path, domain)
            if data.get("revision") != revision(raw):
                raise RevisionConflict("词条已变化，请刷新并重新预览，未覆盖新内容")
        title = data.get("name", name)
        validate_name(title)
        if title != name:
            if create:
                raise BookError("新建标题与请求不一致")
            if any((title_key(e['name']) == title_key(title) if domain == 'world' else e['name'].casefold() == title.casefold()) and e['name'] != name for e in catalog(domain)):
                raise RevisionConflict("同名词条已存在，请使用其他标题")
            # Keep physical identity and receipts stable: one atomic file write,
            # not a create/delete pair. Display title is Markdown frontmatter.
            post["name"] = title
        content = str(data.get("body", "")).strip()
        if not content:
            raise BookError("正文不能为空")
        old_cognition = post.get("cognition", {})
        old_cognition = old_cognition if isinstance(old_cognition, dict) else {}
        subject = data.get("subject_id", old_cognition.get("subject_id", "agent" if domain == "self" else "unknown"))
        kind = data.get("kind", old_cognition.get("kind", "self_reflection" if domain == "self" else "theory" if domain == "scp" else "fact"))
        if subject not in subject_catalog() or kind not in KINDS:
            raise BookError("认知归属无效")
        knower = "agent" if domain == "self" else data.get("knower_id", old_cognition.get("knower_id", "unknown"))
        if knower not in subject_catalog():
            raise BookError("观点归属无效")
        if "core" in data and not isinstance(data["core"], bool):
            raise BookError("核心标记无效")
        if domain == "self" and subject != "agent":
            raise BookError("自我书主体是 Agent；他人的事实请保存到世界书")
        if domain == "world" and kind in {"self_reflection", "theory"}:
            raise BookError("自我认识或理论定义请分别选择自我书或 SCP")
        for key in ("aliases", "keywords"):
            value = data.get(key, post.get(key, []))
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise BookError("别名与关键词需要文本列表")
            post[key] = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        if title != name and name != "镜影":
            post['aliases'] = list(dict.fromkeys([*post['aliases'], name]))
        if "镜影" in post.get("aliases", []) and subject in {"agent", "human"}:
            raise BookError("镜影是双方的相对关系，不能作为某一方固定别名")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        old_id = str(post.get("entry_id") or "")
        post["entry_id"] = old_id if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", old_id) else revision(domain + ":" + path.relative_to(folder(domain)).as_posix())[:24]
        previous = post.get("cognition", {})
        previous = previous if isinstance(previous, dict) else {}
        other_metadata = {}
        if domain == "other":
            from .other_book import metadata
            other_metadata = metadata(data, previous, subject, knower)
        editor = editor or ("人类伙伴手动确认" if not receipt else "人类伙伴审核建议")
        post["cognition"] = {**previous, **other_metadata, "subject_id": subject, "knower_id": knower, "kind": kind,
                             "core": data.get("core", previous.get("core", False)) if domain in {"self", "other"} else False,
                             "source": previous.get("source") or editor,
                             "last_edited_by": editor, "updated_at": stamp}
        if receipt:
            post["cognition_receipts"] = list(dict.fromkeys([*post.get("cognition_receipts", []), receipt]))
        if provenance:
            updates = post.get("cognition_updates", [])
            if not isinstance(updates, list):
                raise BookError("维护来源记录损坏，未覆盖")
            post["cognition_updates"] = [*updates, provenance]
        old_log = post.content.split("## 编辑记录", 1)[1].strip() if "## 编辑记录" in post.content else ""
        post.content = strip_log(content) + f"\n\n## 编辑记录\n- {stamp}: {editor}\n" + old_log
        if raw:
            backup = ROOT / "data" / "cognition_backups" / domain / post["entry_id"] / (revision(raw) + ".md")
            if not backup.exists():
                atomic_text(backup, raw)
        atomic_text(path, frontmatter.dumps(post) + "\n")
        reload_domain(domain)
        return _read(path, domain)


def related_entries(name: str, keywords: list, body: str = "", domain: str = "world") -> list[dict]:
    """Candidates only: category overlap never establishes entity equality."""
    result = []
    proposed = {str(name).strip().casefold(), *(str(k).strip().casefold() for k in keywords)} - {""}
    for entry in catalog(domain):
        identities = {entry["name"].casefold(), *(a.casefold() for a in entry["aliases"])}
        direct = sorted(proposed & identities)
        if domain == 'world' and title_key(name) == title_key(entry['name']):
            direct = direct or [entry['name']]
        mentioned = sorted(t for t in identities if len(t) >= 2 and t in body.casefold())
        overlap = sorted(proposed & {k.casefold() for k in entry["keywords"]})
        if direct or mentioned or overlap:
            result.append({"name": entry["name"], "reason": "名称或别名匹配" if direct else "正文提及已有实体" if mentioned else "关键词相关，需确认主体",
                           "terms": direct or mentioned or overlap})
    return result


def core_context() -> str:
    parts = ["主体与关系事实：" + MIRROR_FACT]
    for entry in catalog("self"):
        if entry["core"]:
            parts.append(f"核心自我记录：{entry['name']}（来源：{entry['source']}）\n{entry['body']}")
    return "\n\n".join(parts)


def person_projection(subject: str) -> list[dict]:
    return [e for e in catalog("world") if e["subject_id"] == subject]
