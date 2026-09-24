"""Host-injected HTTP surface for the complete cognition-book lifecycle.

Nothing here opens a socket or reads a private conversation store. The host
supplies an authorization dependency, canonical day loader, and model call.
"""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException

from . import books, daily, maintenance_router, other_router, router


def create_router(*, authorize, load_day, llm, session_id,
                  memory_lookup=None, load_other_evidence=None):
    """Create a protected router; all four callbacks are required.

    ``load_day(date, session_id)`` and ``llm(prompt)`` are async. ``session_id``
    is a zero-argument callable returning the fixed private conversation ID.
    Optional ``memory_lookup(evidence)`` and
    ``load_other_evidence(date, session_id)`` are async host callbacks.
    The host mounts the router only behind its own local authentication.
    """
    if not all(callable(value) for value in (authorize, load_day, llm, session_id)):
        raise TypeError("authorize, load_day, llm and session_id must be callables")
    if any(value is not None and not callable(value) for value in (memory_lookup, load_other_evidence)):
        raise TypeError("optional source hooks must be callables")
    if books.ROOT.resolve() == Path(__file__).resolve().parents[1]:
        raise ValueError("configure cognition.books.ROOT to a private data directory before mounting")
    api = APIRouter(prefix="/api/cognition", tags=["cognition"], dependencies=[Depends(authorize)])

    api.add_api_route("/books/{domain}", router.get_books, methods=["GET"])
    api.add_api_route("/books/{domain}/{name}", router.put_book, methods=["PUT"])
    api.add_api_route("/books/{domain}/{name}", router.delete_book, methods=["DELETE"])
    api.add_api_route("/books/{domain}/{name}/read", router.mark_read, methods=["POST"])
    api.add_api_route("/deleted/{rid}/restore", router.restore_book, methods=["POST"])
    api.add_api_route("/identity", router.identity, methods=["GET"])
    api.add_api_route("/people/{subject}", router.person, methods=["GET"])

    api.add_api_route("/other", other_router.listing, methods=["GET"])
    api.add_api_route("/other/entities", other_router.register, methods=["POST"])
    api.add_api_route("/other/entities/{identifier}/names", other_router.update_names, methods=["PUT"])

    api.add_api_route("/maintenance", maintenance_router.listing, methods=["GET"])
    api.add_api_route("/maintenance/scp-status", maintenance_router.scp_status, methods=["GET"])
    api.add_api_route("/maintenance/{rid}/preview", maintenance_router.preview, methods=["POST"])
    api.add_api_route("/maintenance/{rid}/decide", maintenance_router.decide, methods=["POST"])

    @api.post("/maintenance/run")
    async def run_day(data: dict = Body(...)):
        source_date = data.get("source_date") if isinstance(data, dict) else None
        try:
            if not isinstance(source_date, str) or datetime.strptime(source_date, "%Y-%m-%d").strftime("%Y-%m-%d") != source_date:
                raise ValueError("bad date")
        except ValueError:
            raise HTTPException(422, "source_date must be YYYY-MM-DD") from None
        sid = session_id()
        if not isinstance(sid, str) or not sid:
            raise HTTPException(409, "no canonical session")
        rows = await load_day(source_date, sid)
        if not isinstance(rows, list):
            raise HTTPException(503, "canonical day loader failed")
        if any(not isinstance(row, dict) or row.get("session_id", sid) != sid for row in rows):
            raise HTTPException(422, "day loader returned foreign-session rows")
        other_evidence = await load_other_evidence(source_date, sid) if load_other_evidence else None
        if other_evidence is not None and not isinstance(other_evidence, list):
            raise HTTPException(503, "other evidence loader failed")
        try:
            result = await daily.run_daily(rows, llm, source_date=source_date, session_id=sid,
                                           memory_lookup=memory_lookup, other_evidence=other_evidence)
        except books.BookError:
            raise HTTPException(503, "daily cognition did not fully complete; inspect maintenance status") from None
        return {"status": result["status"], "source_date": source_date,
                "lanes": result["lanes"], "items_count": len(result.get("items", []))}

    return api


__all__ = ["create_router"]
