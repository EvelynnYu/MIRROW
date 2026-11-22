"""Shared V2 retrieval contracts for bodies other than the private chat builder.

Consumers receive attributed records, not chat prompts or private trace data.
All lanes use the same warmed ranker and bounded source projection. No LLM.
"""

from datetime import date
import asyncio


from .context_shadow import recall_service_snapshot


def _record(document, score: float, *, hit=None, max_chars=1_200) -> dict:
    from .recall_context import project_reading_record
    prefix = ("[旧资料中的记忆印象，未按 Memory V2 重新核验]\n"
              if document.source_layer == "legacy" else "")
    content, cut, points = (project_reading_record(
        hit, max_chars=max(1, max_chars - len(prefix))) if hit is not None else
                           (document.display_text or "\n".join(document.summaries), False, ()))
    content = prefix + content
    if hit is not None and len(content) > max_chars:
        content, cut = content[:max_chars], True
    subject = "AI" if document.subject_ids == ("k",) else (
        "人类伙伴" if document.subject_ids == ("human",) else "shared")
    return {"id": document.document_id, "content": content, "score": score,
            "source": "Memory V2", "metadata": {
                "source_layer": document.source_layer, "subject": subject,
                "source_date": document.date_from, "latest_source_date": document.date_to,
                "source_date_basis": "record_calendar_date",
                "domain": list(document.facets), "importance": document.importance,
                "confidence": document.confidence,
                "source_message_ids": list(document.source_message_ids),
                "participant_ids": list(document.participant_ids),
                "source_kinds": list(document.source_kinds),
                "source_scope_ids": list(document.source_scope_ids),
                "related_record_ids": [item.document_id for item in document.reading_companions],
                "related_records_are_confirmed_continuations": False,
                "rendered_event_ids": [point.node.event_id for point in points],
                "excerpt": cut,
                "reading_document_ids": ([f'event:{eid}' for eid in document.event_ids]
                                         if document.source_layer == 'event' else [document.document_id]),
                "excerpt": cut,
                "derived_legacy_summary": document.source_layer == "legacy"}}


def search_records_sync(query: str, *, limit=8, reference_date=None,
                        date_from="", date_to="", max_record_chars=1_200,
                        exclude_ids=()) -> list[dict]:
    from .recall import RecallQuery
    from .recall_routing import apply_recall_routing_hints, route_recall_query
    from .process_reading import search_reading
    if not str(query or "").strip():
        return []
    with recall_service_snapshot() as (service, excluded):
        routed = apply_recall_routing_hints(
            RecallQuery(query, date_from=date_from, date_to=date_to, exclude_message_ids=excluded,
                        exclude_document_ids=tuple(exclude_ids)),
            route_recall_query(query, reference_date=reference_date or date.today()))
        result = search_reading(service.index, routed, limit=max(1, min(int(limit), 20)), source=service.source)
        records = []
        seen_events = set()
        for hit in result.hits:
            if hit.document.event_ids and set(hit.document.event_ids).issubset(seen_events):
                continue
            record = _record(hit.document, hit.score, hit=hit,
                             max_chars=max(1, int(max_record_chars)))
            if not record["content"]:
                continue
            records.append(record)
            seen_events.update(record["metadata"]["rendered_event_ids"])
        return records


async def search_records(query: str, **kwargs) -> list[dict]:
    return await asyncio.to_thread(search_records_sync, query, **kwargs)


async def search_memory_items(query: str, *, limit=8, **kwargs):
    from context_scheduler_mcp.context_scheduler import MemoryItem
    records = await search_records(query, limit=limit, **kwargs)
    return [MemoryItem(id=item["id"], content=item["content"], similarity=item["score"],
                       importance=item["metadata"]["importance"], bucket="long_term",
                       source="memory_v2", metadata=item["metadata"]) for item in records]


def browse_records_sync(*, limit=5, min_importance=0.3, exclude_ids=()) -> list[dict]:
    import random
    with recall_service_snapshot() as (service, excluded):
        documents = [item for item in service.index.load_documents()
                     if item.source_layer in {"event", "period", "legacy", "group"}
                     and item.importance >= min_importance
                     and item.document_id not in exclude_ids
                     and not set(item.source_message_ids).intersection(excluded)]
        current = [item for item in documents if item.source_layer != "legacy"]
        documents = current or documents
        return [_record(item, 0.0) for item in random.sample(documents, min(len(documents), max(1, limit)))]


async def get_wander_memories(min_importance=0.3, query=None, top_k=5, exclude_ids=()):
    records = (await search_records(query, limit=top_k, exclude_ids=exclude_ids) if query
               else await asyncio.to_thread(browse_records_sync, limit=top_k,
                                            min_importance=min_importance, exclude_ids=exclude_ids))
    return [{"id": item["id"], "topic": item["content"][:40],
             "reading_document_ids": item['metadata']['reading_document_ids'],
             "event": item["content"], "timestamp": item["metadata"]["source_date"],
             "importance_score": item["metadata"]["importance"],
             "domain": item["metadata"]["domain"], "tags": [],
             "source": item["source"], "source_layer": item["metadata"]["source_layer"]}
            for item in records]
