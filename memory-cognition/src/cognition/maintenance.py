"""Evidence-backed maintenance with durable plans and recoverable Markdown commits.

No reads invoke models or change state. Unknowns remain candidates; receipts make
the gap between the book commit and its audit record recoverable after a crash.
"""
import asyncio
import json
import secrets
from datetime import datetime
from pathlib import Path

from . import books
from .maintenance_sources import (EXTRACTION, REVIEW, model_materials,
                                  parse_response, resolve_ref, sources)

_run_lock = asyncio.Lock()
_instance = secrets.token_hex(12)


def digest(value):
    return books.revision(json.dumps(value, sort_keys=True, ensure_ascii=False))


def folder():
    return books.ROOT / "data" / "cognition_maintenance"


def record_path(rid):
    if not isinstance(rid, str) or len(rid) != 64 or any(c not in "0123456789abcdef" for c in rid):
        raise books.BookError("维护记录编号无效")
    return folder() / "items" / (rid + ".json")


def write(item):
    books.atomic_text(record_path(item["id"]), json.dumps(item, ensure_ascii=False, indent=2))


def get(rid):
    path = record_path(rid)
    if not path.exists():
        raise books.BookError("维护记录不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def records():
    return record_report()["records"]


def record_report():
    with books.LOCK:
        items, damaged = [], 0
        for path in (folder() / "items").glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(item, dict) or any(k not in item for k in ("id", "status", "created_at", "proposal", "plan", "review", "reason", "source_date")):
                    raise ValueError("invalid record")
                items.append(item)
            except (ValueError, OSError):
                damaged += 1
        return {"records": sorted(items, key=lambda i: (i["created_at"], i["id"]), reverse=True),
                "damaged_records": damaged}


def status():
    # Absence is not success: a new install has not yet analysed a conversation.
    paths = sorted([*(folder() / "runs").glob("*.json"), *(folder() / "daily_runs").glob("*.json")],
                   key=lambda p: (p.stat().st_mtime_ns, p.parent.name == 'daily_runs'), reverse=True)
    try:
        state = json.loads(paths[0].read_text(encoding="utf-8")) if paths else {"status": "not_run"}
        if not isinstance(state, dict):
            raise ValueError("invalid run")
    except (ValueError, OSError):
        return {"status": "error", "message": "最近运行状态无法读取，请保留文件排查；未视作完成"}
    if state.get("status") == "running" and state.get("instance") != _instance:
        state = {**state, "status": "interrupted", "message": "上次进程在分析期间退出；可重试，已写入记录保留"}
    from .maintenance_status import current_lanes
    return current_lanes({k: state[k] for k in ("id", "status", "source_date", "started_at", "completed_at", "message", "items", "lanes", "memory_status", "wander_processed", "lane_errors", "lane_rejections", "rejected_proposal_count", "discarded_review_count") if k in state})


def validate(proposal, space):
    """space 是引用空间（当天材料 + 历史观察证据）；模型给的是短序号 n。"""
    if not isinstance(proposal, dict):
        raise books.BookError("候选不是对象")
    p = dict(proposal)
    if p.get("domain") not in books.DOMAINS or p.get("subject_id") not in books.subject_catalog() or p.get("knower_id") not in books.subject_catalog():
        raise books.BookError("领域或主体无效")
    if p.get("kind") not in books.KINDS or p.get("operation") not in {"append", "replace"}:
        raise books.BookError("材料性质或处理方式无效")
    for key in ("name", "body", "reason"):
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise books.BookError("候选缺少名称、正文或依据")
    if not isinstance(p.get("target_entry", ""), str):
        raise books.BookError("目标词条无效")
    if not isinstance(p.get("keywords", []), list) or any(not isinstance(k, str) for k in p.get("keywords", [])):
        raise books.BookError("关键词无效")
    if p["domain"] == "self" and (p["subject_id"] != "agent" or p["knower_id"] != "agent"):
        raise books.BookError("自我认识必须归属 Agent")
    if p["domain"] == "other":
        from .other_book import metadata
        p.update(metadata(p, {}, p["subject_id"], p["knower_id"]))
    if p["domain"] == "world" and p["kind"] == "self_reflection":
        raise books.BookError("自我认识不能写入世界书")
    refs = p.get("evidence")
    if not isinstance(refs, list) or not refs:
        raise books.BookError("缺少原文证据")
    resolved = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise books.BookError("证据结构无效")
        msg = resolve_ref(ref, space)
        quote = ref.get("quote")
        if not msg or not isinstance(quote, str) or len(quote.strip()) < 4 or quote not in msg["text"]:
            raise books.BookError("引文或消息编号无法核实")
        resolved.append({"message_id": msg["id"], "speaker": msg["speaker"], "quote": quote, "timestamp": msg["timestamp"],
                         "identity_basis": msg["identity_basis"],
                         "complete_statement": quote.strip() == msg["text"].strip() or
                         (quote.strip().endswith(tuple("。！？!?")) and
                          (msg["text"].find(quote) == 0 or msg["text"][msg["text"].find(quote) - 1] in "。！？!?\n"))})
    p["evidence"] = resolved
    return p


def make_plan(p, domain=None, name=None, operation=None):
    domain = domain or p["domain"]
    name = name or p.get("target_entry") or p["name"]
    if domain not in books.DOMAINS or not isinstance(name, str):
        raise books.BookError("目标无效")
    entries = books.catalog(domain)
    targets = [e for e in entries if e["name"] == name]
    if not targets:
        targets = [e for e in entries if name in e["aliases"] and name != "镜影"]
    if not targets and domain == 'world':
        targets = [e for e in entries if books.title_key(e['name']) == books.title_key(name)]
    if len(targets) > 1:
        raise books.BookError("目标名称不唯一")
    target = targets[0] if targets else None
    if target:
        name = target["name"]
    operation = operation or p["operation"]
    if operation not in {"append", "replace"}:
        raise books.BookError("处理方式无效")
    before = target["body"] if target else ""
    addition = books.strip_log(p["body"])
    after = addition if operation == "replace" else before if addition in before else (before + "\n\n" + addition).strip()
    base = target or {"subject_id": p["subject_id"], "knower_id": p["knower_id"], "kind": p["kind"], "core": False, "aliases": [], "keywords": []}
    # Existing metadata is not automatically reclassified; explicit domain changes
    # use that domain's valid defaults in the human-reviewed plan.
    if domain == "self":
        base = {**base, "subject_id": "agent", "knower_id": "agent", "kind": "self_reflection"}
    elif domain == "other":
        base = {**base, "knower_id": "agent", "kind": p["kind"] if p["kind"] in {"preference", "interpretation", "agreement"} else "interpretation"}
    elif base["kind"] == "self_reflection":
        base = {**base, "kind": "interpretation"}
    return {"domain": domain, "name": name, "before": before, "after": after,
            "revision": target["revision"] if target else "new", "operation": operation,
            **{k: base[k] for k in ("subject_id", "knower_id", "kind", "core", "aliases")},
            **({k: base.get(k, p.get(k, default)) for k, default in {"owner_id": "agent", "scope": "person", "basis": "inferred", "state": "active", "expires_at": ""}.items()} if domain == "other" else {}),
            "keywords": list(dict.fromkeys([*base["keywords"], *p.get("keywords", [])]))}


def auto_reason(p, review, plan, snapshot):
    if not all(review.get(k) is True for k in ("supported", "attribution_clear", "no_conflict", "entity_match", "novel")):
        return "独立审核未全部确认支持、归属、实体、增量与无冲突"
    if p["operation"] != "append" or plan["core"]:
        return "更正、替换或核心自我变化需要确认"
    if not all(e["complete_statement"] for e in p["evidence"]):
        return "引文不是完整陈述，需保留上下文后确认"
    if plan["revision"] == "new" and books.related_entries(p["name"], p.get("keywords", []), p["body"], p["domain"]):
        return "新建内容涉及已有条目，需核对实体或概念合并目标"
    if p.get("target_entry") and plan["revision"] == "new":
        return "指定目标不存在"
    if plan["domain"] == "world":
        related = books.related_entries(p["name"], p.get("keywords", []), p["body"])
        identities = {plan["name"], *plan["aliases"]} - {"镜影", "宠物", "猫女儿"}
        if plan["revision"] == "new" and related:
            return "新建内容涉及已有实体，需核对合并目标"
        if p["kind"] not in {"fact", "preference"} or p["knower_id"] != "human":
            return "不是人类伙伴明确陈述的外部事实或偏好"
        if any(e["speaker"] != "human" for e in p["evidence"]):
            return "包含非人类伙伴自述的证据"
        if p["subject_id"] != "human" and not any(len(n) >= 2 and n in e["quote"] for n in identities for e in p["evidence"]):
            return "证据没有明确命名外部主体"
    elif plan["domain"] == "self":
        if review.get("stable") is not True or len({e["message_id"] for e in p["evidence"] if e["speaker"] == "agent"}) < 2:
            return "自我认识尚缺少多条一致的 Agent 自述"
        if any(e["speaker"] != "agent" for e in p["evidence"]):
            return "不能把他人评价自动认作 Agent 的自我认识"
    elif plan["domain"] == "other":
        # Old data has not yet been reviewed/migrated. Do not automatically
        # establish a competing authority during this explicit transition.
        return "他者书处于存量归属确认阶段，新增长期认识先保留候选"
    existing = next((e for e in snapshot[p["domain"]] if e["name"] == plan["name"]), None)
    if plan["revision"] != "new" and not existing:
        return "审核之后出现了新目标，需重新核对"
    if existing and any(existing[k] not in {"unknown", p[k]} for k in ("subject_id", "knower_id")):
        return "已有主体或观点归属与候选不同"
    if existing and existing["revision"] != plan["revision"]:
        return "分析期间目标已变化"
    return ""


async def run_for_date(source_date, session_id, llm, progress_cb=None):
    """Read the canonical active-day rows, never a rolling 24-hour transcript."""
    from event_chronicle import get_global_chronicle
    if not session_id:
        raise books.BookError("没有可确定的主会话")
    rows = await asyncio.to_thread(get_global_chronicle().get_messages_by_date, source_date)
    rows = [r for r in rows if r.get("session_id") == session_id]
    from neuron_registry import neuron_trace
    with neuron_trace("cognition_maintenance", model="Flash") as trace:
        trace.set_input(f"active_date={source_date}; canonical messages={len(rows)}")
        try:
            from .daily import run_daily
            result = await run_daily(rows, llm, source_date=source_date, session_id=session_id,
                                     progress_cb=progress_cb)
            trace.set_output(f"status={result['status']}; records={len(result.get('items', []))}")
            if result["status"] in {"completed", "completed_with_rejections"} and result.get("world_items"):
                from world_book.suggestions import clear_flags
                clear_flags(source_date)
            return result
        except BaseException:
            trace.set_error("认知维护未全部完成，查看维护记录")
            raise


def commit(item, plan, automatic):
    with books.LOCK:
        # Write-ahead intent must be durable before a book mutation.
        item.update(status="applying", plan=plan, automatic=automatic)
        write(item)
        try:
            entry = books.save(plan["domain"], plan["name"], {**plan, "body": plan["after"]},
                               receipt="maintenance:" + item["id"],
                               editor=("Agent 自主整理自动更新" if plan['domain'] in {'self', 'other'} else "世界书事实核验自动更新") if automatic else "人类伙伴确认认知维护候选",
                               provenance={"maintenance_item_id": item["id"], "source_date": item["source_date"],
                                           "automatic": automatic, "subject_id": item["proposal"]["subject_id"],
                                           "knower_id": item["proposal"]["knower_id"],
                                           "evidence_ids": list(dict.fromkeys(e["message_id"] for e in item["proposal"]["evidence"]))})
        except books.BookError:
            item.update(status="error" if automatic and plan["domain"] in {"self", "other"} else "pending", reason="目标或版本校验未通过，保留原文等待重新整理")
            write(item)
            raise
        item.update(status="auto_applied" if automatic else "applied", result_revision=entry["revision"],
                    completed_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        write(item)
        return item


def preview(rid, options):
    with books.LOCK:
        item = get(rid)
        if item["status"] not in {"pending", "applying"}:
            raise books.BookError("该记录无需确认")
        if item["status"] == "applying":
            plan = item["plan"]
        else:
            plan = make_plan(item["proposal"], options.get("domain"), options.get("name"), options.get("operation"))
        return {**plan, "item_revision": digest(item), "id": rid,
                "related": books.related_entries(item["proposal"]["name"], item["proposal"].get("keywords", []), item["proposal"]["body"], plan["domain"])}


def music_note_only_world(item, domain):
    refs = item.get("proposal", {}).get("evidence", [])
    return (domain == "world" and item.get("session_id") == "music" and bool(refs)
            and all(str(ref.get("message_id", "")).startswith("music-note:") for ref in refs))


def decide(rid, action, plan=None, expected="", new_entity_confirmed=False):
    with books.LOCK:
        item = get(rid)
        if item["status"] not in {"pending", "applying"}:
            raise books.RevisionConflict("记录已处理，请刷新")
        if digest(item) != expected:
            raise books.RevisionConflict("记录已变化，请重新预览")
        if action == "reject":
            if item["status"] == "applying":
                raise books.BookError("写入结果待核对，请先恢复收尾，不能直接忽略")
            item.update(status="rejected", completed_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            write(item)
            return item
        if action != "apply" or not plan:
            raise books.BookError("处理动作无效")
        if music_note_only_world(item, plan["domain"]):
            raise books.BookError("这条音乐建议只有 Agent 的感想，没有设备或歌单操作回执，不能确认为外部事实")
        committed = books.receipt_present(plan["domain"], plan["name"], "maintenance:" + rid)
        if plan["revision"] == "new" and not committed and any(e["name"] == plan["name"] for e in books.catalog(plan["domain"])):
            raise books.RevisionConflict("预览后目标已创建，请重新预览")
        if plan["revision"] == "new" and not committed and not new_entity_confirmed:
            p = item["proposal"]
            if books.related_entries(p["name"], p.get("keywords", []), p["body"], plan["domain"]):
                raise books.BookError("存在相关条目，请选择已有目标或明确确认这是不同实体／概念")
        item["human_reclassified"] = plan["domain"] != item["proposal"]["domain"] or plan["name"] != (item["proposal"].get("target_entry") or item["proposal"]["name"])
        return commit(item, plan, item.get("automatic", False) if item["status"] == "applying" else False)


async def run(messages, llm, *, source_date, session_id, factual=False, memory=None):
    """Called by topic-end orchestration; retries reuse evidence-derived run IDs."""
    return await run_evidence(sources(messages), llm, source_date=source_date,
                              session_id=session_id, factual=factual, memory=memory)


async def run_evidence(evidence_list, llm, *, source_date, session_id, factual=False, memory=None):
    """Internal attributed observations; never map device facts into user speech."""
    if not evidence_list:
        return {"status": "no_evidence", "items": []}
    run_id = digest({"date": source_date, "session": session_id, "messages": evidence_list, "pipeline": "factual-v1" if factual else "legacy"})
    path = folder() / "runs" / (run_id + ".json")
    async with _run_lock:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if state.get("status") in {"completed", "completed_with_rejections"}:
            return state
        state = {**state, "id": run_id, "source_date": source_date, "status": "running",
                 "instance": _instance,
                 "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        def checkpoint():
            books.atomic_text(path, json.dumps(state, ensure_ascii=False, indent=2))
        if factual and "historical_observations" not in state:
            current_text = "\n".join(e["text"] for e in evidence_list).casefold()
            candidates = []
            for record in records():
                if (record.get("status") != "observing" or record.get("session_id") != session_id
                        or not record.get("source_date") or record["source_date"] >= source_date):
                    continue
                p = record["proposal"]
                terms = [p.get("target_entry", ""), p.get("name", ""), *p.get("keywords", [])]
                if any(isinstance(t, str) and len(t.strip()) >= 2 and t not in {"人类伙伴", "习惯", "事实"}
                       and t.casefold() in current_text for t in terms):
                    candidates.append(record)
            candidates.sort(key=lambda r: (r["source_date"], r["id"]), reverse=True)
            state["historical_observations"] = [{"record_id": r["id"], "source_date": r["source_date"],
                                                 "evidence": r["proposal"]["evidence"]} for r in candidates[:20]]
        checkpoint()
        try:
            state['phase'] = 'catalog'
            snapshot = {domain: [{k: e[k] for k in ("name", "body", "aliases", "keywords", "revision", "subject_id", "knower_id", "kind", "core", "owner_id", "scope", "basis", "state", "expires_at")}
                                for e in books.catalog(domain)] for domain in books.DOMAINS}
            # 引用空间 = 当天材料 + 历史观察证据，统一按 1-based 短序号连续编号；
            # 模型只看到 n，真实 message_id 由程序在收回时映射（2026-09-11）。
            space = list(evidence_list)
            _space_ids = {e["id"] for e in space}
            for observation in state.get("historical_observations", []):
                for ref in observation.get("evidence", []):
                    if ref.get("message_id") not in _space_ids:
                        _space_ids.add(ref["message_id"])
                        space.append({"id": ref["message_id"], "text": ref.get("quote", ""),
                                      "speaker": ref.get("speaker", ""), "timestamp": ref.get("timestamp", ""),
                                      "identity_basis": ref.get("identity_basis", "")})
            _n_of = {e["id"]: i for i, e in enumerate(space, 1)}
            if "proposals" not in state:
                state['phase'] = 'extraction'
                from world_book.suggestions import build_flags_section
                flags = build_flags_section(source_date) if books.ROOT == Path(__file__).resolve().parents[1] else ""
                payload = {"source_date": source_date, "books": snapshot, "messages": model_materials(space),
                           "attention_hints_not_evidence": flags}
                if factual:
                    from .factual import EXTRACTION as extraction_prompt
                    payload["memory"] = memory or []
                    payload["historical_observations"] = [
                        {"record_id": o.get("record_id", ""), "source_date": o.get("source_date", ""),
                         "evidence": [{"n": _n_of[r["message_id"]], "quote": r.get("quote", "")}
                                      for r in o.get("evidence", []) if r.get("message_id") in _n_of]}
                        for o in state.get("historical_observations", [])]
                else:
                    extraction_prompt = EXTRACTION
                extraction_raw = await llm(extraction_prompt + json.dumps(payload, ensure_ascii=False))
                state['phase'] = 'parse_extraction'
                proposals = parse_response(extraction_raw, "proposals")
                state['phase'] = 'validate_proposals'
                # One malformed or unverifiable proposal must not discard other
                # independently anchored proposals from the same model response.
                # JSON/provider failures remain lane-level failures because they
                # cannot be attributed to one proposal safely.
                checked, source_indexes, rejections = [], [], []
                for index, proposal in enumerate(proposals):
                    state['proposal_index'] = index
                    try:
                        candidate = validate(proposal, space)
                        if factual and (candidate.get("domain") != "world" or candidate.get("operation") != "replace"):
                            raise books.BookError("世界书必须返回完整修订正文")
                        if factual and music_note_only_world({"session_id": session_id, "proposal": candidate}, candidate["domain"]):
                            raise books.BookError("Agent 的音乐感想不能单独证明设备播放或歌单变更")
                    except books.BookError as exc:
                        rejections.append({"source_index": index, "stage": "validation",
                                           "error_type": type(exc).__name__, "reason": str(exc)})
                        continue
                    checked.append(candidate)
                    source_indexes.append(index)
                state['proposals'] = checked
                state['proposal_source_indexes'] = source_indexes
                state['proposal_rejections'] = rejections
                state.pop('proposal_index', None)
                checkpoint()
            proposals = state["proposals"]
            source_indexes = state.get("proposal_source_indexes", list(range(len(proposals))))
            if "reviews" not in state:
                state['phase'] = 'review'
                from .factual import REVIEW as factual_review
                raw = parse_response(await llm((factual_review if factual else REVIEW) + json.dumps({"books": snapshot, "messages": evidence_list, "historical_observations": state.get("historical_observations", []), "proposals": proposals}, ensure_ascii=False)), "reviews") if proposals else []
                review_map, duplicate_indexes, unbound_reviews = {}, set(), 0
                for review_item in raw:
                    if (not isinstance(review_item, dict)
                            or type(review_item.get("index")) is not int
                            or review_item["index"] not in range(len(proposals))):
                        unbound_reviews += 1
                        continue
                    review_index = review_item["index"]
                    if review_index in review_map:
                        duplicate_indexes.add(review_index)
                        continue
                    review_map[review_index] = review_item
                for review_index in duplicate_indexes:
                    review_map.pop(review_index, None)
                reviewed_proposals, reviewed_source_indexes, reviews = [], [], []
                for review_index, proposal in enumerate(proposals):
                    review_item = review_map.get(review_index)
                    if review_item is None:
                        state.setdefault("proposal_rejections", []).append(
                            {"source_index": source_indexes[review_index], "stage": "review",
                             "error_type": "ReviewContractError", "reason": "审核结果缺项或编号重复"})
                        continue
                    reviews.append({**review_item, "index": len(reviewed_proposals)})
                    reviewed_proposals.append(proposal)
                    reviewed_source_indexes.append(source_indexes[review_index])
                state["proposals"] = proposals = reviewed_proposals
                state["proposal_source_indexes"] = source_indexes = reviewed_source_indexes
                state["reviews"] = reviews
                state["discarded_review_count"] = unbound_reviews
                # Snapshot versions bind the independent review to what it saw.
                state["snapshot"] = snapshot
                checkpoint()
            reviewed_snapshot = state["snapshot"]
            state['phase'] = 'commit'
            result_ids = []
            for index, p in enumerate(proposals):
                rid = digest({"session": session_id, "proposal": p})
                result_ids.append(rid)
                with books.LOCK:
                    if record_path(rid).exists():
                        old = get(rid)
                        if old["status"] == "applying":
                            try:
                                commit(old, old["plan"], old["automatic"])
                            except books.RevisionConflict:
                                old.update(status="pending", reason="恢复时目标版本已变化，请重新预览")
                                write(old)
                        continue
                    review = next(r for r in state["reviews"] if r["index"] == index)
                    plan = make_plan(p)
                    from .factual import auto_reason as factual_reason
                    reason = (factual_reason if factual else auto_reason)(p, review, plan, reviewed_snapshot)
                    item = {"id": rid, "status": "pending", "source_date": source_date, "session_id": session_id,
                            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "proposal": p, "review": review, "reason": reason or "证据与独立审核通过",
                            "plan": plan}
                    if factual and p.get("observation"):
                        item["status"] = "observing"
                        write(item)
                        continue
                    write(item)
                    if plan["after"] == plan["before"] or (p["operation"] == "append" and all(e["quote"] in plan["before"] for e in p["evidence"])):
                        item.update(status="unchanged", reason="正文已包含该内容，无重复写入")
                        write(item)
                    elif not reason:
                        # Automatic commits preserve the source's actual words, not
                        # the extractor's free-form paraphrase. The latter remains
                        # visible as a candidate summary in the audit.
                        statements = []
                        for e in p["evidence"]:
                            speaker = books.SUBJECTS[e["speaker"]]
                            text = f"{speaker}的明确陈述（{source_date}，原文）：\n" + "\n".join("> " + line for line in e["quote"].splitlines())
                            if text not in statements:
                                statements.append(text)
                        if not factual:
                            plan["after"] = (plan["before"] + "\n\n" + "\n\n".join(statements)).strip()
                        try:
                            commit(item, plan, True)
                        except books.RevisionConflict:
                            item.update(status="pending", reason="写入前目标版本已变化")
                            write(item)
            rejected_count = len(state.get("proposal_rejections", []))
            discarded_review_count = int(state.get("discarded_review_count", 0))
            final_status = "completed_with_rejections" if rejected_count or discarded_review_count else "completed"
            state.update(status=final_status, items=result_ids,
                         rejected_proposal_count=rejected_count,
                         discarded_review_count=discarded_review_count,
                         completed_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            if final_status == "completed_with_rejections":
                state["message"] = "可核实的候选已完成；无法逐条核实的候选已隔离，未写入认知书"
            for key in ('error_type','error_detail','proposal_index','message'):
                if key != 'message' or final_status == 'completed':
                    state.pop(key, None)
            checkpoint()
            return state
        except BaseException as exc:
            state.update(status="error", message="本轮未全部完成；已落地记录保留，下次相同来源重试可恢复")
            state['error_type'] = type(exc).__name__
            state['error_detail'] = str(exc) if isinstance(exc, books.BookError) else 'HTTP ' + str(exc.status_code) if hasattr(exc, 'status_code') else '详见本次调用诊断'
            checkpoint()
            raise
