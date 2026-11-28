"""Affect v2 投影、人工操作与只读诊断 API。

这些端点只提供本地单用户监控所需的有限信息；dry-run gate/context preview
不会写生产状态。Flash 原始载荷不从这里直接返回。
"""

from __future__ import annotations

import logging
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from affect import get_affect_service
from affect.sidecar import parse_affect_sidecar
from affect.open_loop import select_open_loops
from affect.quiet_timer import timer_status
from open_loops import get_open_loop_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/affect", tags=["affect"])


def _service():
    return get_affect_service()


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Expose status only; never return v1 prompt material or raw output."""
    item = dict(job or {})
    for key in ("input_summary_json", "raw_output", "materials", "content", "prompt"):
        item.pop(key, None)
    summary = item.get("output_summary_json")
    if isinstance(summary, dict):
        item["output_summary_json"] = {
            key: summary[key] for key in ("operation", "confidence", "event_id") if key in summary
        }
    return item


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep event status/provenance while withholding all v1 body fields."""
    allowed = {
        "event_id", "trace_id", "source_type", "source_id", "occurred_at",
        "accepted_at", "status", "operation", "explicitness", "confidence",
        "time_scope", "prompt_version", "idempotency_key",
        "previous_snapshot_version", "result_snapshot_version", "validation_error",
    }
    return {key: value for key, value in dict(event or {}).items() if key in allowed}


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose snapshot version/status without v1 state or provider material."""
    item = dict(snapshot or {})
    result = {
        key: item[key] for key in ("id", "version", "updated_at", "last_event_id")
        if key in item
    }
    # Labels/flags are enough for a monitor status badge.  Descriptions,
    # evidence, and any legacy observed-state fields stay in affect.db only.
    for field in ("self_state", "resonance_state"):
        state = item.get(field)
        if not isinstance(state, dict):
            result[field] = {}
            continue
        result[field] = {
            key: state[key] for key in ("labels", "confidence", "explicitness", "time_scope", "updated_at")
            if key in state
        }
    return result


def _public_transition(transition: dict[str, Any]) -> dict[str, Any]:
    """Expose trace handles/status without before/after material or excerpts."""
    allowed = {
        "transition_id", "trace_id", "owner", "operation", "component_id",
        "parent_transition_id", "reason", "status", "branch_state",
        "source_invalidated", "created_at",
    }
    return {key: value for key, value in dict(transition or {}).items() if key in allowed}


def _public_component(component: dict[str, Any]) -> dict[str, Any]:
    """Keep component facts while removing source excerpts from diagnostics."""
    item = dict(component or {})
    refs = item.pop("source_refs_json", item.pop("source_refs", []))
    # Appraisal JSON is provider output, not a panel-safe trace.  The panel
    # has the normalized label/reason and transition handles instead.
    item.pop("appraisal_json", None)
    item.pop("appraisal", None)
    if isinstance(refs, list):
        item["source_refs"] = [
            {key: ref.get(key) for key in ("source_type", "source_id", "source_revision", "relation")}
            for ref in refs if isinstance(ref, dict)
        ]
    return item


def _public_projection(projection: dict[str, Any]) -> dict[str, Any]:
    """Remove source excerpts/provider appraisal from projection diagnostics."""
    item = dict(projection or {})
    for owner_key in ("self", "resonance"):
        state = item.get(owner_key)
        if not isinstance(state, dict):
            continue
        state = dict(state)
        # The full candidate list is an internal context allocation, not a
        # second public component channel with unsanitized source excerpts.
        state.pop("foreground_feelings", None)
        state["components"] = [_public_component(component) for component in state.get("components", [])]
        if isinstance(state.get("dominant"), dict):
            state["dominant"] = _public_component(state["dominant"])
        item[owner_key] = state
    return item


async def _current_projection(service):
    # The fixed primary chat owns this board's active day; UI selection and
    # leftover test topics cannot choose a different emotional foreground.
    from session_manager import get_global_session_manager
    session_id = await get_global_session_manager().get_primary_session_id()
    projection = await asyncio.to_thread(service.get_v2_projection, session_id=session_id)
    return _public_projection(projection)


@router.get("/overview")
async def affect_overview():
    service = _service()
    store = service.store
    settings = service.settings()
    return {
        "mode": settings.get("engine_mode", settings.get("context_mode", "shadow")),
        "settings": settings,
        "snapshot": _public_snapshot(store.get_snapshot()),
        "projection": await _current_projection(service),
        "components": [_public_component(item) for item in store.list_components(include_inactive=True)[:100]],
        "presets": store.list_presets(include_disabled=True),
        "checkins": store.list_checkins(),
        "timer": timer_status(),
        "jobs": [_public_job(job) for job in store.list_jobs(limit=20)],
        "events": [_public_event(event) for event in store.list_events(limit=20)],
        "checkpoints": store.list_checkpoints(limit=20),
        "gates": store.list_gates(limit=20),
        "context_audits": store.list_context_audits(limit=20),
    }


@router.get("/board")
async def affect_board():
    """Lightweight read model for the user-facing mood board."""
    service = _service()
    store = service.store
    settings = service.settings()
    return {
        "mode": settings.get("engine_mode", settings.get("context_mode", "shadow")),
        "settings": settings,
        "projection": await _current_projection(service),
        "recognition": service.resonance_recognition_status(),
        "presets": store.list_presets(include_disabled=True),
        "checkins": store.list_checkins(),
    }


@router.get("/open-loops/lifecycle")
async def open_loop_lifecycle():
    """A separate, read-only Open Loop authority projected into the mood board."""
    return {"items": get_open_loop_store().list_lifecycle(limit=50)}


@router.get("/snapshot")
async def affect_snapshot():
    service = _service()
    snapshot = service.store.get_snapshot()
    public = _public_snapshot(snapshot)
    return {**public, "snapshot": public, "projection": await _current_projection(service)}


@router.get("/projection")
async def affect_projection(widget: bool = False):
    projection = await _current_projection(_service())
    if widget:
        dominant = (projection.get("self") or {}).get("dominant") or {}
        return {"family": dominant.get("family", "")}
    return projection


@router.get("/components")
async def affect_components(owner: str = "", include_inactive: bool = False):
    items = _service().store.list_components(owner=owner, include_inactive=include_inactive)
    # The ordinary panel may inspect provenance handles, but never receives
    # prompt materials or provider raw output.
    return {"components": [_public_component(item) for item in items]}


@router.patch("/components/{component_id}/correct")
async def correct_affect_component(component_id: str, payload: dict[str, Any]):
    """Append an 人类伙伴-owned correction for one active component.

    The service generates the correction source identity and revision.  The
    endpoint deliberately accepts no direct component/database mutation
    primitive; failed validation leaves the projection untouched.
    """
    try:
        service = _service()
        result = service.correct_component(component_id, **(payload or {}))
        return {
            "accepted": bool(result.get("accepted")),
            "idempotent": bool(result.get("idempotent", False)),
            "component": _public_component(result.get("component") or {}),
            "transition": _public_transition(result.get("transition") or {}),
            "projection": await _current_projection(service),
        }
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "component not found" else 409 if "active" in message or "mismatch" in message or "idempotency" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.post("/components/{component_id}/resolve")
async def resolve_affect_component(component_id: str, payload: dict[str, Any] | None = None):
    """Append an 人类伙伴-owned resolve transition for one active component."""
    try:
        service = _service()
        result = service.resolve_component(component_id, **(payload or {}))
        return {
            "accepted": bool(result.get("accepted")),
            "idempotent": bool(result.get("idempotent", False)),
            "component": _public_component(result.get("component") or {}),
            "transition": _public_transition(result.get("transition") or {}),
            "projection": await _current_projection(service),
        }
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "component not found" else 409 if "active" in message or "mismatch" in message or "idempotency" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/transitions")
async def affect_transitions(limit: int = 50):
    """返回可读的演变履历；不返回 Flash 原文或 episode prompt material。"""
    return {"transitions": [_public_transition(item) for item in _service().store.list_transitions(limit=max(1, min(int(limit), 200)))]}


@router.get("/presets")
async def affect_presets(include_disabled: bool = True):
    return {"presets": _service().store.list_presets(include_disabled=include_disabled)}


@router.post("/manual/checkins")
async def create_manual_checkin(payload: dict[str, Any]):
    try:
        return _service().record_manual_checkin(
            preset_id=str(payload.get("preset_id") or ""), label=str(payload.get("label") or ""),
            emoji=str(payload.get("emoji") or "🙂"), family=str(payload.get("family") or "复杂/未归类"),
            intensity=payload.get("intensity"), persistence=str(payload.get("persistence") or ""),
            dimensions=payload.get("dimensions") if isinstance(payload.get("dimensions"), dict) else None,
            note=str(payload.get("note") or ""), checkin_id=str(payload.get("checkin_id") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/manual/checkins/{checkin_id}")
async def edit_manual_checkin(checkin_id: str, payload: dict[str, Any]):
    try:
        return _service().edit_manual_checkin(checkin_id, **(payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/manual/checkins/{checkin_id}/resolve")
async def resolve_manual_checkin(checkin_id: str):
    try:
        return _service().resolve_manual_checkin(checkin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/manual/checkins/{checkin_id}/revoke")
async def revoke_manual_checkin(checkin_id: str):
    try:
        return _service().revoke_manual_checkin(checkin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/manual/checkins/{checkin_id}")
async def delete_manual_checkin(checkin_id: str):
    try:
        return _service().delete_manual_checkin(checkin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/presets/{preset_id}")
async def update_affect_preset(preset_id: str, payload: dict[str, Any]):
    try:
        existing = _service().store.get_preset(preset_id) or {}
        item = {**existing, **dict(payload or {})}
        item["preset_id"] = preset_id
        return {"preset": _service().store.upsert_preset(item)}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/presets/{preset_id}")
async def delete_affect_preset(preset_id: str):
    try:
        preset = _service().store.delete_preset(preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail="preset not found")
        return {"preset": preset}
    except HTTPException:
        raise
    except ValueError as exc:
        status = 409 if "builtin" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/sidecar/preview")
async def preview_affect_sidecar(payload: dict[str, Any]):
    result = parse_affect_sidecar(str((payload or {}).get("content") or ""))
    return result.to_dict()


@router.get("/events")
async def affect_events(limit: int = 100):
    return {"events": [_public_event(event) for event in _service().store.list_events(limit=limit)]}


@router.get("/jobs")
async def affect_jobs(limit: int = 100):
    return {"jobs": [_public_job(job) for job in _service().store.list_jobs(limit=limit)]}


@router.get("/checkpoints")
async def affect_checkpoints(limit: int = 100):
    return {"checkpoints": _service().store.list_checkpoints(limit=limit)}


@router.get("/gates")
async def affect_gates(limit: int = 100):
    return {"gates": _service().store.list_gates(limit=limit)}


@router.get("/context-audits")
async def affect_context_audits(limit: int = 100):
    return {"audits": _service().store.list_context_audits(limit=limit)}


@router.get("/settings")
async def affect_settings():
    return {"settings": _service().settings()}


@router.put("/settings")
async def update_affect_settings(payload: dict[str, Any]):
    try:
        return {"settings": _service().update_settings(payload or {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/gate/preview")
async def preview_affect_gate(payload: dict[str, Any]):
    """只读 gate dry-run；不创建 gate 记录。"""
    service = _service()
    return service.gate.preview(
        str((payload or {}).get("content", "")),
        source_id=str((payload or {}).get("source_id", "preview")) or "preview",
        semantic_score=(payload or {}).get("semantic_score"),
        semantic_hint=bool((payload or {}).get("semantic_hint", False)),
        semantic_label=str((payload or {}).get("semantic_label", "")),
    )


@router.post("/context-preview")
async def preview_affect_context(payload: dict[str, Any]):
    """只读 open-loop/emotion preview；不写 audit、不唤醒 snooze、不改变状态。"""
    body = payload or {}
    service = _service()
    settings = service.settings()
    mode = str(body.get("mode") or settings.get("engine_mode", settings.get("context_mode", "shadow")))
    if mode not in {"shadow", "enabled", "disabled"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    selection = select_open_loops(
        recipe=str(body.get("recipe") or "FULL_CHAT"),
        user_message=str(body.get("user_message") or ""),
        mode=mode,
        char_budget=int(body.get("open_loop_char_budget", settings.get("open_loop_char_budget", 3200))),
        foreground_limit=int(body.get("foreground_limit", settings.get("open_loop_foreground_limit", 6))),
        related_limit=int(body.get("related_limit", settings.get("open_loop_related_limit", 3))),
        due_limit=int(body.get("due_limit", settings.get("open_loop_due_limit", 2))),
        trace_id="dry-run",
        record_audit=False,
    )
    return {
        "mode": mode,
        "emotion_self": service.render_self(),
        "emotion_resonance": service.render_resonance(),
        "open_loops": selection,
        "injected": False,
        "dry_run": True,
    }


@router.post("/quiet/check")
async def create_quiet_checkpoints():
    # v2 exposes the persisted quiet watermark without creating a v1
    # checkpoint/job.  The background timer consumes the same episode rows.
    return {"episodes": _service().store.list_due_episodes()}


@router.put("/open-loops/{loop_id}/attention")
async def set_open_loop_attention(loop_id: str, payload: dict[str, Any]):
    body = payload or {}
    state = str(body.get("state", ""))
    authoritative = get_open_loop_store().get(loop_id)
    if authoritative is None:
        raise HTTPException(status_code=404, detail="open loop not found")
    if str(authoritative.get("status") or "") == "resolved":
        raise HTTPException(status_code=409, detail="resolved open loop cannot receive attention projection")
    try:
        item = _service().store.set_loop_attention(
            loop_id, state, wake_at=str(body.get("wake_at", "")),
            source_id=str(body.get("source_id", "manual")), reason=str(body.get("reason", "")),
        )
        return {"ok": True, "attention": item}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
