"""Read-only Context Builder bridge for Memory V2 recall.

The legacy entrypoint remains a content-free shadow observer.  The explicit
``enabled`` mode may return the same bounded projection to Context Builder;
neither mode writes memory authority data.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

from .active_query_plan import ActiveQueryPlan
from .cognition_recall import CognitionRecallIndex
from .conversation_source import ConversationSource
from .multi_source_recall import ConversationWindowRecallIndex, MultiSourceRecallIndex
from .recall import EventRecallIndex, RecallQuery
from .recall_candidates import SQLiteFTSCandidateSelector
from .recall_context import (
    RecallContextPolicy,
    RecallContextProjection,
    build_recall_context,
    select_summary_hits,
)
from .recall_evidence import RecallEvidenceBundle
from .recall_query import build_recall_query_envelope
from .recall_routing import route_recall_query
from .recall_session import get_recall_session_cache
from .recall_service import ShadowRecallExecution, run_shadow_recall
from .semantic_candidates import (
    HybridRecallCandidateSelector,
    SQLiteSemanticCandidateIndex,
)


_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ContextShadowObservation:
    status: str
    duration_ms: float = 0.0
    candidate_count: int = 0
    evaluated_count: int = 0
    returned_count: int = 0
    semantic_available: bool = False
    semantic_status: str = "disabled"
    evidence_expanded: bool = False
    rendered_hit_count: int = 0
    rendered_chars: int = 0
    truncated: bool = False
    error_type: str = ""
    trace_id: str = ""

    def safe_observation(self) -> dict[str, object]:
        return {
            "status": self.status,
            "injected": False,
            "duration_ms": round(self.duration_ms, 1),
            "candidate_count": self.candidate_count,
            "evaluated_count": self.evaluated_count,
            "returned_count": self.returned_count,
            "semantic_available": self.semantic_available,
            "semantic_status": self.semantic_status,
            "evidence_expanded": self.evidence_expanded,
            "rendered_hit_count": self.rendered_hit_count,
            "rendered_chars": self.rendered_chars,
            "truncated": self.truncated,
            "error_type": self.error_type,
            "trace_id": self.trace_id,
        }


class MemoryV2ContextShadow:
    """Long-lived read-only recall graph plus a rebuildable candidate cache."""

    def __init__(
        self,
        memory_db_paths: Sequence[str | Path],
        authority_db_path: str | Path,
        cache_path: str | Path,
        *,
        cognition_entry_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        semantic_index: SQLiteSemanticCandidateIndex | None = None,
        semantic_setup_status: str = "disabled",
        canonical_sessions_only: bool = False,
    ):
        if not memory_db_paths:
            raise ValueError("Memory V2 shadow needs at least one memory database")
        resolved_memory_paths = tuple(Path(path).resolve() for path in memory_db_paths)
        authority_path = Path(authority_db_path).resolve()
        if any(not path.is_file() for path in resolved_memory_paths):
            raise FileNotFoundError("a configured Memory V2 shadow database is missing")
        if not authority_path.is_file():
            raise FileNotFoundError("the conversation authority database is missing")

        self.source = ConversationSource(
            authority_path,
            canonical_sessions_only=canonical_sessions_only,
        )
        self.lexical_selector = SQLiteFTSCandidateSelector(cache_path)
        self.semantic_index = semantic_index
        self.semantic_setup_status = semantic_setup_status
        self.selector = (
            HybridRecallCandidateSelector(
                self.lexical_selector,
                semantic_index,
            )
            if semantic_index is not None
            else self.lexical_selector
        )
        from .autobiographical import load_recollections_by_message_id

        try:
            recollections_by_message_id = load_recollections_by_message_id(authority_path)
        except Exception:
            # This projection is optional and additive.  A legacy diary schema
            # or one malformed row must never take production recall offline.
            recollections_by_message_id = {}
        indexes: list[EventRecallIndex] = []
        for memory_path in resolved_memory_paths:
            indexes.append(EventRecallIndex(
                memory_path,
                recollections_by_message_id=recollections_by_message_id,
            ))
        indexes.append(ConversationWindowRecallIndex(self.source))
        if cognition_entry_loader is None:
            indexes.append(CognitionRecallIndex(authority_path))
        else:
            indexes.append(
                CognitionRecallIndex(
                    authority_path,
                    entry_loader=cognition_entry_loader,
                )
            )
        self.index = MultiSourceRecallIndex(
            indexes,
            semantic_scorer=semantic_index,
            candidate_selector=self.selector,
        )

    def close(self) -> None:
        self.lexical_selector.close()
        if self.semantic_index is not None:
            self.semantic_index.close()

    def prewarm(self) -> ContextShadowObservation:
        """Materialize read models and optional vectors outside the chat path."""

        started = perf_counter()
        try:
            documents = self.index.load_documents()
            semantic_status = self.semantic_setup_status
            if isinstance(self.selector, HybridRecallCandidateSelector):
                semantic_status = self.selector.semantic_status
                if semantic_status == "ok" and self.semantic_index is not None:
                    self.semantic_index.score("记忆检索预热", ())
            return ContextShadowObservation(
                status="ready",
                duration_ms=(perf_counter() - started) * 1000,
                candidate_count=len(documents),
                semantic_available=semantic_status == "ok",
                semantic_status=semantic_status,
            )
        except Exception as exc:
            return ContextShadowObservation(
                status="error",
                duration_ms=(perf_counter() - started) * 1000,
                semantic_status=self.semantic_setup_status,
                error_type=type(exc).__name__,
            )

    def observe(
        self,
        query_text: str,
        *,
        reference_date: date,
        current_message_id: str = "",
        excluded_message_ids: Sequence[str] = (),
    ) -> ContextShadowObservation:
        observation, _, _ = self.observe_with_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            excluded_message_ids=excluded_message_ids,
        )
        return observation

    def observe_with_trace(
        self,
        query_text: str,
        *,
        reference_date: date,
        current_message_id: str = "",
        excluded_message_ids: Sequence[str] = (),
    ) -> tuple[
        ContextShadowObservation,
        ShadowRecallExecution | None,
        RecallContextProjection | None,
    ]:
        started = perf_counter()
        try:
            execution, projection = self.search_with_projection(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                excluded_message_ids=excluded_message_ids,
            )
            return (
                ContextShadowObservation(
                    status="observed",
                    duration_ms=(perf_counter() - started) * 1000,
                    candidate_count=execution.result.candidate_count,
                    evaluated_count=execution.result.evaluated_count or 0,
                    returned_count=len(execution.result.hits),
                    semantic_available=execution.result.semantic_available,
                    semantic_status=execution.result.semantic_status,
                    evidence_expanded=execution.evidence.expanded,
                    rendered_hit_count=projection.rendered_hit_count,
                    rendered_chars=len(projection.text),
                    truncated=projection.truncated,
                ),
                execution,
                projection,
            )
        except Exception as exc:
            return (
                ContextShadowObservation(
                    status="error",
                    duration_ms=(perf_counter() - started) * 1000,
                    semantic_status=self.semantic_setup_status,
                    error_type=type(exc).__name__,
                ),
                None,
                None,
            )

    def search_with_projection(
        self,
        query_text: str,
        *,
        reference_date: date,
        current_message_id: str = "",
        excluded_message_ids: Sequence[str] = (),
        limit: int = 8,
        force_source_detail: bool | None = None,
        context_policy: RecallContextPolicy | None = None,
    ) -> tuple[ShadowRecallExecution, RecallContextProjection]:
        """Search the shared summary index for automatic or active recall."""

        if not 1 <= int(limit) <= 10:
            raise ValueError("Memory V2 search limit must be within 1..10")
        current_active_dates = tuple(
            dict.fromkeys(
                message.active_date
                for message in self.source.read_message_ids(
                    (current_message_id,) if current_message_id else ()
                )
                if message.active_date
            )
        )
        recent_messages = (
            self.source.read_recent_dialogue_before(current_message_id)
            if current_message_id
            else ()
        )
        query_envelope = build_recall_query_envelope(query_text, recent_messages)
        execution = run_shadow_recall(
            self.index,
            self.source,
            RecallQuery(
                text=query_text,
                variants=query_envelope.variants,
                exclude_message_ids=tuple(
                    dict.fromkeys(
                        str(message_id)
                        for message_id in (
                            *excluded_message_ids,
                            *((current_message_id,) if current_message_id else ()),
                        )
                        if str(message_id)
                    )
                ),
                exclude_active_dates=current_active_dates,
            ),
            reference_date=reference_date,
            limit=int(limit),
            force_source_detail=force_source_detail,
            query_envelope=query_envelope,
        )
        projection = build_recall_context(
            execution.result,
            execution.evidence,
            policy=context_policy,
        )
        if current_message_id:
            get_recall_session_cache().store(
                (current_message_id,),
                query_text=query_text,
                execution=execution,
                limit=int(limit),
                source_detail_requested=(
                    bool(force_source_detail)
                    if force_source_detail is not None
                    else execution.routing.needs_source_detail
                ),
                origin="automatic",
            )
        return execution, projection


def _trace_candidate(
    execution: ShadowRecallExecution,
    projection: RecallContextProjection | None = None,
) -> list[dict[str, object]]:
    def bounded(values: Sequence[Any], limit: int) -> tuple[list[Any], int]:
        items = list(values)
        return items[:limit], max(0, len(items) - limit)

    def bounded_text(value: str, limit: int = 12_000) -> tuple[str, bool]:
        text = str(value or "")
        return text[:limit], len(text) > limit

    summary_identities = (
        set(projection.rendered_documents)
        if projection is not None
        else {
            (hit.document.source_layer, hit.document.document_id)
            for _rank, hit in select_summary_hits(
                execution.result.hits,
                max_hits=RecallContextPolicy().max_hits,
            )
        }
    )
    evidence_identities = {
        (
            execution.result.hits[group.rank - 1].document.source_layer,
            execution.result.hits[group.rank - 1].document.document_id,
        )
        for group in execution.evidence.groups
        if 0 < group.rank <= len(execution.result.hits)
    }
    evidence_message_ids = {
        message.message_id
        for group in execution.evidence.groups
        for message in group.messages
    }
    decisions = (
        tuple(
            (decision.hit, decision.decision, decision.reason)
            for decision in execution.result.decisions
        )
        or tuple(
            (hit, "selected", "selected_top_k")
            for hit in execution.result.hits
        )
    )
    rows: list[dict[str, object]] = []
    for rank, (hit, raw_decision, reason) in enumerate(decisions, start=1):
        document = hit.document
        identity = (document.source_layer, document.document_id)
        summary_selected = identity in summary_identities
        evidence_selected = identity in evidence_identities
        selected = summary_selected or evidence_selected
        if summary_selected and evidence_selected:
            projection_reason = "selected_summary_and_evidence"
        elif summary_selected:
            projection_reason = "selected_summary"
        elif evidence_selected:
            projection_reason = "selected_evidence"
        elif set(document.source_message_ids).intersection(evidence_message_ids):
            projection_reason = "overlapping_source_evidence"
        elif raw_decision == "selected":
            projection_reason = "projection_limit"
        else:
            projection_reason = str(reason or "output_limit")
        event_ids, omitted_event_ids = bounded(document.event_ids, 80)
        source_message_ids, omitted_source_message_ids = bounded(
            document.source_message_ids, 120
        )
        source_anchors, omitted_source_anchors = bounded(document.source_anchors, 40)
        summaries, omitted_summaries = bounded(document.summaries, 24)
        display_text, display_text_truncated = bounded_text(document.display_text)
        rows.append(
            {
                "rank": rank,
                "decision": "selected" if selected else "eliminated",
                "reason": projection_reason,
                "summary_selected": summary_selected,
                "evidence_selected": evidence_selected,
                "retrieval_decision": str(raw_decision),
                "source_layer": document.source_layer,
                "document_id": document.document_id,
                "event_ids": event_ids,
                "omitted_event_id_count": omitted_event_ids,
                "source_message_ids": source_message_ids,
                "omitted_source_message_id_count": omitted_source_message_ids,
                "source_anchors": [
                    {
                        "message_id": anchor.message_id,
                        "span_start": anchor.span_start,
                        "span_end": anchor.span_end,
                        "span_digest": anchor.span_digest,
                    }
                    for anchor in source_anchors
                ],
                "omitted_source_anchor_count": omitted_source_anchors,
                "summaries": summaries,
                "omitted_summary_count": omitted_summaries,
                "display_text": display_text,
                "display_text_truncated": display_text_truncated,
                "subject_ids": list(document.subject_ids),
                "participant_ids": list(document.participant_ids),
                "knower_ids": list(document.knower_ids),
                "event_types": list(document.event_types),
                "facets": list(document.facets),
                "source_kinds": list(document.source_kinds),
                "active_date_from": document.active_date_from,
                "active_date_to": document.active_date_to,
                "date_from": document.date_from,
                "date_to": document.date_to,
                "importance": document.importance,
                "confidence": document.confidence,
                "score": round(hit.score, 6),
                "lexical_score": round(hit.lexical_score, 6),
                "fuzzy_score": round(hit.fuzzy_score, 6),
                "semantic_score": (
                    round(hit.semantic_score, 6)
                    if hit.semantic_score is not None
                    else None
                ),
                "rarity_score": round(hit.rarity_score, 6),
                "direct_match": hit.direct_match,
                "matched_terms": list(hit.matched_terms),
            }
        )
    return rows


def _trace_evidence(bundle: RecallEvidenceBundle) -> dict[str, object]:
    return {
        **bundle.safe_observation(),
        "groups": [
            {
                "rank": group.rank,
                "direct_match": group.direct_match,
                "requested_anchor_count": group.requested_anchor_count,
                "missing_anchor_count": group.missing_anchor_count,
                "omitted_anchor_count": group.omitted_anchor_count,
                "messages": [
                    {
                        "message_id": message.message_id,
                        "timestamp": message.timestamp,
                        "role": message.role,
                        "source_kind": message.source_kind,
                        "event_type": message.event_type,
                        "content": message.content,
                        "is_anchor": message.is_anchor,
                        "content_truncated": message.content_truncated,
                        "span_selected": message.span_selected,
                        "span_fallback": message.span_fallback,
                    }
                    for message in group.messages
                ],
            }
            for group in bundle.groups
        ],
    }


def _record_trace(
    query_text: str,
    *,
    reference_date: date,
    current_message_id: str,
    session_id: str,
    observation: ContextShadowObservation,
    execution: ShadowRecallExecution | None = None,
    projection: RecallContextProjection | None = None,
    index_version: str = "",
    mode: str = "shadow",
) -> dict[str, object]:
    """Attach an opaque trace ID without changing recall success/failure."""

    safe = observation.safe_observation()
    try:
        from .trace_store import (
            TRACE_INDEX_VERSION,
            get_default_trace_store,
            trace_enabled,
        )

        if trace_enabled():
            safe["trace_id"] = get_default_trace_store().create_trace(
                query_text=query_text,
                status=observation.status,
                reference_date=reference_date.isoformat(),
                session_id=session_id,
                user_message_id=current_message_id,
                mode=mode,
                index_version=index_version or TRACE_INDEX_VERSION,
                metrics={
                    **observation.safe_observation(),
                    "candidate_counts_by_layer": (
                        dict(execution.result.candidate_counts_by_layer)
                        if execution is not None
                        else {}
                    ),
                    "evaluated_counts_by_layer": (
                        dict(execution.result.evaluated_counts_by_layer)
                        if execution is not None
                        else {}
                    ),
                },
                routing=(
                    {
                        **execution.routing.safe_observation(),
                        "query_envelope": execution.query_envelope.safe_observation(),
                    }
                    if execution is not None and execution.query_envelope is not None
                    else execution.routing.safe_observation()
                    if execution is not None
                    else {}
                ),
                candidates=(
                    _trace_candidate(execution, projection)
                    if execution is not None
                    else ()
                ),
                evidence=(
                    _trace_evidence(execution.evidence)
                    if execution is not None
                    else {"expanded": False, "reason": observation.status}
                ),
                expected_projection_text=(projection.text if projection is not None else ""),
            )
    except Exception as exc:
        safe["trace_error_type"] = type(exc).__name__
    return safe


_SEMANTIC_ENV_KEYS = (
    "MIRROW_MEMORY_V2_SEMANTIC_MODEL",
    "MIRROW_MEMORY_V2_MODEL_CACHE",
    "MIRROW_MEMORY_V2_SEMANTIC_REVISION",
    "MIRROW_MEMORY_V2_SEMANTIC_QUERY_INSTRUCTION",
    "MIRROW_MEMORY_V2_SEMANTIC_CACHE",
)


@dataclass(frozen=True)
class _ContextShadowConfiguration:
    memory_paths: tuple[Path, ...]
    authority_path: Path
    lexical_cache_path: Path
    backend_root: Path
    semantic_enabled: bool
    semantic_allow_rebuild: bool
    semantic_environment: tuple[tuple[str, str], ...]


@dataclass
class _ContextShadowState:
    status: str
    service: MemoryV2ContextShadow | None = None
    previous_service: MemoryV2ContextShadow | None = None
    warmup: ContextShadowObservation | None = None
    error_type: str = ""
    refresh_excluded_message_ids: tuple[str, ...] = ()


_service_lock = threading.Lock()
_service_states: dict[_ContextShadowConfiguration, _ContextShadowState] = {}
_source_quarantine_message_ids: set[str] = set()


def memory_v2_context_mode() -> str:
    """Resolve the explicit three-state cutover mode with shadow compatibility."""

    explicit = os.environ.get("MIRROW_MEMORY_V2_CONTEXT_MODE", "").strip().casefold()
    if explicit in {"disabled", "shadow", "enabled"}:
        return explicit
    legacy_shadow = (
        os.environ.get("MIRROW_MEMORY_V2_CONTEXT_SHADOW", "")
        .strip()
        .casefold()
        in _TRUE_VALUES
    )
    return "shadow" if legacy_shadow else "disabled"


def _enabled() -> bool:
    return memory_v2_context_mode() != "disabled"


def _configured_paths() -> _ContextShadowConfiguration | None:
    # The neutral key is the production name.  Keep the old shadow-only key as
    # a read-compatible alias so an emergency rollback does not require a data
    # or configuration migration.
    raw_memory_paths = (
        os.environ.get("MIRROW_MEMORY_V2_DBS", "").strip()
        or os.environ.get("MIRROW_MEMORY_V2_SHADOW_DBS", "").strip()
    )
    if not raw_memory_paths:
        return None
    memory_paths = tuple(
        Path(value).resolve()
        for value in raw_memory_paths.split(os.pathsep)
        if value.strip()
    )
    if not memory_paths:
        return None
    backend_root = Path(__file__).resolve().parents[1]
    authority_path = Path(
        os.environ.get(
            "MIRROW_MEMORY_V2_AUTHORITY_DB",
            str(backend_root / "events" / "event_chronicle.db"),
        )
    ).resolve()
    cache_path = Path(
        os.environ.get(
            "MIRROW_MEMORY_V2_RECALL_CACHE",
            str(backend_root / ".tmp" / "memory_v2_context_shadow_fts.db"),
        )
    ).resolve()
    semantic_enabled = (
        (
            os.environ.get("MIRROW_MEMORY_V2_SEMANTIC_ENABLED", "").strip()
            or os.environ.get("MIRROW_MEMORY_V2_SEMANTIC_SHADOW", "").strip()
        )
        .casefold()
        in _TRUE_VALUES
    )
    semantic_allow_rebuild = (
        os.environ.get("MIRROW_MEMORY_V2_SEMANTIC_BACKGROUND_REBUILD", "")
        .strip()
        .casefold()
        in _TRUE_VALUES
    )
    return _ContextShadowConfiguration(
        memory_paths=memory_paths,
        authority_path=authority_path,
        lexical_cache_path=cache_path,
        backend_root=backend_root,
        semantic_enabled=semantic_enabled,
        semantic_allow_rebuild=semantic_allow_rebuild,
        semantic_environment=tuple(
            (key, os.environ.get(key, "")) for key in _SEMANTIC_ENV_KEYS
        ),
    )


def _build_and_prewarm_service(
    configuration: _ContextShadowConfiguration,
) -> tuple[MemoryV2ContextShadow, ContextShadowObservation]:
    semantic_index = None
    semantic_setup_status = "disabled"
    if configuration.semantic_enabled:
        try:
            from .semantic_profile import (
                discover_local_semantic_profile,
                open_local_semantic_index,
            )

            profile = discover_local_semantic_profile(
                configuration.backend_root,
                environ=dict(configuration.semantic_environment),
            )
            semantic_index = open_local_semantic_index(
                profile,
                allow_rebuild=configuration.semantic_allow_rebuild,
            )
            semantic_setup_status = "configured"
        except Exception as exc:
            semantic_setup_status = f"setup_error:{type(exc).__name__}"
    service = MemoryV2ContextShadow(
        configuration.memory_paths,
        configuration.authority_path,
        configuration.lexical_cache_path,
        semantic_index=semantic_index,
        semantic_setup_status=semantic_setup_status,
        canonical_sessions_only=True,
    )
    warmup = service.prewarm()
    if semantic_index is None and semantic_setup_status != "disabled":
        warmup = ContextShadowObservation(
            status=warmup.status,
            duration_ms=warmup.duration_ms,
            candidate_count=warmup.candidate_count,
            semantic_available=False,
            semantic_status=semantic_setup_status,
            error_type=warmup.error_type,
        )
    return service, warmup


def _run_warmup(
    configuration: _ContextShadowConfiguration,
    state: _ContextShadowState,
) -> None:
    service: MemoryV2ContextShadow | None = None
    warmup: ContextShadowObservation | None = None
    error_type = ""
    try:
        service, warmup = _build_and_prewarm_service(configuration)
        if warmup.status != "ready":
            error_type = warmup.error_type or "WarmupError"
    except Exception as exc:
        error_type = type(exc).__name__

    obsolete = False
    previous: MemoryV2ContextShadow | None = None
    reused_previous = False
    with _service_lock:
        current = _service_states.get(configuration)
        if current is not state:
            obsolete = True
        else:
            previous = state.previous_service
            state.previous_service = None
            state.warmup = warmup
            state.error_type = error_type
            if service is not None and not error_type:
                state.service = service
                state.status = "ready"
                _source_quarantine_message_ids.difference_update(
                    state.refresh_excluded_message_ids
                )
                state.refresh_excluded_message_ids = ()
            elif previous is not None:
                # The prior materialized service remains safe because every
                # mutated source is filtered at read time while the durable
                # event status already excludes it.  A failed cache rebuild
                # must not expose legacy memory as a continuity fallback.
                state.service = previous
                state.status = "ready"
                reused_previous = True
            else:
                state.service = None
                state.status = "error"
    if obsolete:
        if service is not None:
            service.close()
        return
    if previous is not None and not reused_previous and previous is not service:
        previous.close()
    if error_type and service is not None:
        service.close()


def _ensure_warmup(
    configuration: _ContextShadowConfiguration,
    *,
    force: bool = False,
    refresh_excluded_message_ids: Sequence[str] = (),
) -> _ContextShadowState:
    with _service_lock:
        existing = _service_states.get(configuration)
        if existing is not None and not force:
            return existing
        carried_exclusions = (
            existing.refresh_excluded_message_ids if existing is not None else ()
        )
        combined_exclusions = tuple(
            dict.fromkeys(
                str(message_id)
                for message_id in (
                    *carried_exclusions,
                    *refresh_excluded_message_ids,
                )
                if str(message_id)
            )
        )
        _source_quarantine_message_ids.update(combined_exclusions)
        previous = (
            existing.service
            if existing is not None and existing.status == "ready"
            else existing.previous_service if existing is not None else None
        )
        state = _ContextShadowState(
            status="warming",
            previous_service=previous,
            refresh_excluded_message_ids=combined_exclusions,
        )
        _service_states[configuration] = state
        thread = threading.Thread(
            target=_run_warmup,
            args=(configuration, state),
            name="memory-v2-shadow-warmup",
            daemon=True,
        )
        thread.start()
        return state


def _state_observation(state: _ContextShadowState) -> dict[str, object]:
    if state.status == "warming":
        return ContextShadowObservation(status="warming").safe_observation()
    if state.status == "error":
        return ContextShadowObservation(
            status="error",
            error_type=state.error_type or "WarmupError",
        ).safe_observation()
    warmup = state.warmup or ContextShadowObservation(status="ready")
    return warmup.safe_observation()


def warm_context_shadow_from_environment(
    *,
    force: bool = False,
) -> dict[str, object]:
    """Start background materialization and return content-free readiness."""

    if not _enabled():
        return ContextShadowObservation(status="disabled").safe_observation()
    configured = _configured_paths()
    if configured is None:
        return ContextShadowObservation(status="not_configured").safe_observation()
    return _state_observation(_ensure_warmup(configured, force=force))


def refresh_context_shadow_from_environment() -> dict[str, object]:
    """Advance the shadow waterline by building a fresh service in background."""

    return warm_context_shadow_from_environment(force=True)


def refresh_context_shadow_for_source_commit(
    changed_paths: Sequence[str | Path],
) -> dict[str, object]:
    """Refresh only when an explicitly committed source belongs to this shadow.

    Callers decide which transaction is a visibility boundary.  This function
    deliberately does not watch mtimes or poll the authority database, because
    ordinary active-day message writes must not repeatedly rebuild the shadow.
    """

    if not _enabled():
        return ContextShadowObservation(status="disabled").safe_observation()
    configured = _configured_paths()
    if configured is None:
        return ContextShadowObservation(status="not_configured").safe_observation()
    committed = {Path(path).resolve() for path in changed_paths}
    visible_sources = {*configured.memory_paths, configured.authority_path}
    if committed.isdisjoint(visible_sources):
        return ContextShadowObservation(status="unrelated_source").safe_observation()
    return _state_observation(_ensure_warmup(configured, force=True))


def quarantine_context_shadow_sources(message_ids: Sequence[str]) -> int:
    """Immediately hide mutated source identities from any materialized index.

    This is a process-local safety overlay for the short cache-rebuild window.
    Durable source invalidation belongs to ``event_status_log``; this overlay
    never changes memory authority and is cleared only after a fresh service is
    successfully materialized from the updated databases.
    """

    normalized = {
        str(message_id).strip()
        for message_id in message_ids
        if str(message_id).strip()
    }
    if not normalized or not _enabled():
        return 0
    with _service_lock:
        _source_quarantine_message_ids.update(normalized)
        return len(normalized)


def refresh_context_shadow_for_source_mutation(
    changed_paths: Sequence[str | Path],
    message_ids: Sequence[str],
) -> dict[str, object]:
    """Quarantine mutated sources, then rebuild their configured read models."""

    if not _enabled():
        return ContextShadowObservation(status="disabled").safe_observation()
    configured = _configured_paths()
    if configured is None:
        return ContextShadowObservation(status="not_configured").safe_observation()
    committed = {Path(path).resolve() for path in changed_paths}
    visible_sources = {*configured.memory_paths, configured.authority_path}
    if committed.isdisjoint(visible_sources):
        return ContextShadowObservation(status="unrelated_source").safe_observation()
    normalized = tuple(
        dict.fromkeys(
            str(message_id).strip()
            for message_id in message_ids
            if str(message_id).strip()
        )
    )
    return _state_observation(
        _ensure_warmup(
            configured,
            force=True,
            refresh_excluded_message_ids=normalized,
        )
    )


def observe_context_shadow_from_environment(
    query_text: str,
    *,
    reference_date: date,
    current_message_id: str = "",
    session_id: str = "",
) -> dict[str, object]:
    """Run the observer and optionally persist its bounded local trace."""

    if not _enabled():
        return _record_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            session_id=session_id,
            observation=ContextShadowObservation(status="disabled"),
        )
    configured = _configured_paths()
    if configured is None:
        return _record_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            session_id=session_id,
            observation=ContextShadowObservation(status="not_configured"),
        )
    try:
        state = _ensure_warmup(configured)
        with _service_lock:
            current = _service_states.get(configured)
            if current is not state:
                return _record_trace(
                    query_text,
                    reference_date=reference_date,
                    current_message_id=current_message_id,
                    session_id=session_id,
                    observation=ContextShadowObservation(status="warming"),
                )
            service = (
                state.service
                if state.status == "ready"
                else state.previous_service if state.status == "warming" else None
            )
            if service is None:
                status = "warming" if state.status == "warming" else "error"
                return _record_trace(
                    query_text,
                    reference_date=reference_date,
                    current_message_id=current_message_id,
                    session_id=session_id,
                    observation=ContextShadowObservation(
                        status=status,
                        error_type=state.error_type if status == "error" else "",
                    ),
                )
            excluded_message_ids = tuple(_source_quarantine_message_ids)
            observation, execution, projection = service.observe_with_trace(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                excluded_message_ids=excluded_message_ids,
            )
        return _record_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            session_id=session_id,
            observation=observation,
            execution=execution,
            projection=projection,
            index_version=(
                "memory-v2-recall-two-lane-v3"
                + (
                    f"+semantic:{service.semantic_index.model_id}"
                    if service.semantic_index is not None
                    else ""
                )
            ),
        )
    except Exception as exc:
        return _record_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            session_id=session_id,
            observation=ContextShadowObservation(
                status="error",
                error_type=type(exc).__name__,
            ),
        )


def recall_context_from_environment(
    query_text: str,
    *,
    reference_date: date,
    current_message_id: str = "",
    session_id: str = "",
) -> tuple[dict[str, object], str]:
    """Return prompt text only under the explicit Memory V2 enabled mode.

    The observation is always content-free.  An empty text with status
    ``recalled`` is an authoritative no-hit result.  Ordinary warming/error
    states let Context Builder retain its existing memory section; a source
    mutation instead reads a quarantined prior V2 service or returns an
    authoritative empty result until a fresh service is ready.
    """

    if memory_v2_context_mode() != "enabled":
        return (
            observe_context_shadow_from_environment(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                session_id=session_id,
            ),
            "",
        )
    configured = _configured_paths()
    if configured is None:
        observation = ContextShadowObservation(status="not_configured")
        return (
            _record_trace(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                session_id=session_id,
                observation=observation,
                mode="enabled",
            ),
            "",
        )
    try:
        state = _ensure_warmup(configured)
        with _service_lock:
            current = _service_states.get(configured)
            if current is not state:
                observation = ContextShadowObservation(status="warming")
                return (
                    _record_trace(
                        query_text,
                        reference_date=reference_date,
                        current_message_id=current_message_id,
                        session_id=session_id,
                        observation=observation,
                        mode="enabled",
                    ),
                    "",
                )
            service = (
                state.service
                if state.status == "ready"
                else state.previous_service if state.status == "warming" else None
            )
            excluded_message_ids = tuple(_source_quarantine_message_ids)
            if service is None:
                status = "warming" if state.status == "warming" else "error"
                observation = ContextShadowObservation(
                    status=status,
                    error_type=state.error_type if status == "error" else "",
                )
                safe = _record_trace(
                    query_text,
                    reference_date=reference_date,
                    current_message_id=current_message_id,
                    session_id=session_id,
                    observation=observation,
                    mode="enabled",
                )
                if excluded_message_ids:
                    # During the first materialization there is no prior safe
                    # V2 service to read.  Treat the empty result as
                    # authoritative instead of falling back to potentially
                    # stale legacy memory containing the mutated source.
                    safe["status"] = "recalled"
                return safe, ""
            observation, execution, projection = service.observe_with_trace(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                excluded_message_ids=excluded_message_ids,
            )
        safe = _record_trace(
            query_text,
            reference_date=reference_date,
            current_message_id=current_message_id,
            session_id=session_id,
            observation=observation,
            execution=execution,
            projection=projection,
            index_version=(
                "memory-v2-recall-two-lane-v3"
                + (
                    f"+semantic:{service.semantic_index.model_id}"
                    if service.semantic_index is not None
                    else ""
                )
            ),
            mode="enabled",
        )
        if observation.status != "observed" or projection is None:
            return safe, ""
        safe["status"] = "recalled"
        safe["injected"] = bool(projection.text)
        return safe, projection.text
    except Exception as exc:
        observation = ContextShadowObservation(
            status="error",
            error_type=type(exc).__name__,
        )
        return (
            _record_trace(
                query_text,
                reference_date=reference_date,
                current_message_id=current_message_id,
                session_id=session_id,
                observation=observation,
                mode="enabled",
            ),
            "",
        )


def search_memory_v2_from_environment(
    query_text: str,
    *,
    reference_date: date,
    limit: int = 8,
    include_source_detail: bool = False,
    session_id: str = "",
    current_message_id: str = "",
    turn_id: str = "",
    active_plan: ActiveQueryPlan | None = None,
    stage_metrics: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], str]:
    """Run Agent's active deep search over the same summary index as auto recall.

    This is a read-only tool boundary, not another memory authority.  It uses a
    larger presentation budget than automatic injection and opens one exact
    source neighborhood only when the caller explicitly requests details.
    """

    bounded_limit = max(1, min(int(limit), 10))
    requested_detail = bool(include_source_detail) or route_recall_query(
        query_text, reference_date=reference_date
    ).needs_source_detail

    def finish(observation: dict[str, object], text: str) -> tuple[dict[str, object], str]:
        if stage_metrics is not None:
            observation = {**observation, "stage_search": {
                key: stage_metrics[key]
                for key in ("source", "branch_count")
                if key in stage_metrics
            }}
        try:
            from .trace_store import get_default_trace_store, trace_enabled

            if trace_enabled() and current_message_id:
                get_default_trace_store().append_search_attempt_by_user_message(
                    user_message_id=current_message_id,
                    session_id=session_id,
                    query_text=query_text,
                    include_source_detail=requested_detail,
                    requested_limit=bounded_limit,
                    reused=bool(observation.get("cache_hit")),
                    status=str(observation.get("status") or "error"),
                    metrics=observation,
                    projection_text=text,
                )
        except Exception:
            pass
        return observation, text

    if not _enabled():
        return finish({"status": "disabled", "injected": False}, "")
    configured = _configured_paths()
    if configured is None:
        return finish({"status": "not_configured", "injected": False}, "")
    try:
        stage_fallback = ""
        if active_plan is not None:
            from .active_multi_recall import search_active_stages

            state = _ensure_warmup(configured)
            with _service_lock:
                current = _service_states.get(configured)
                if current is not state:
                    return finish({"status": "warming", "injected": False}, "")
                service = (
                    state.service
                    if state.status == "ready"
                    else state.previous_service if state.status == "warming" else None
                )
                if service is None:
                    return finish({
                        "status": "warming" if state.status == "warming" else "error",
                        "injected": False,
                        "error_type": state.error_type,
                    }, "")
            try:
                staged = search_active_stages(
                    service.index,
                    service.source,
                    query_text,
                    active_plan,
                    reference_date=reference_date,
                    current_message_id=current_message_id,
                    excluded_message_ids=tuple(_source_quarantine_message_ids),
                    limit=bounded_limit,
                    include_source_detail=requested_detail,
                )
            except Exception as exc:
                stage_fallback = f"error:{type(exc).__name__}"
            else:
                if staged.text:
                    return finish(staged.safe_observation(), staged.text)
                stage_fallback = "no_hits"

        cache = get_recall_session_cache()
        cached = cache.lookup(
            (current_message_id, turn_id),
            query_text=query_text,
            limit=bounded_limit,
            source_detail_requested=requested_detail,
        )
        state = _ensure_warmup(configured)
        with _service_lock:
            current = _service_states.get(configured)
            if current is not state:
                return finish({"status": "warming", "injected": False}, "")
            service = (
                state.service
                if state.status == "ready"
                else state.previous_service if state.status == "warming" else None
            )
            if service is None:
                return finish({
                    "status": "warming" if state.status == "warming" else "error",
                    "injected": False,
                    "error_type": state.error_type,
                }, "")
            policy = RecallContextPolicy(
                max_hits=bounded_limit,
                target_hits=min(8, bounded_limit),
                min_hits_before_cliff=min(3, bounded_limit),
                dense_expansion_min_top_score=0.0,
                max_hit_chars=320,
                max_summary_chars=2_600,
                max_evidence_chars=1_100,
                max_total_chars=3_800,
            )
            if cached is not None:
                execution = cached.execution
                projection = build_recall_context(
                    execution.result,
                    execution.evidence,
                    policy=policy,
                )
            else:
                execution, projection = service.search_with_projection(
                    query_text,
                    reference_date=reference_date,
                    current_message_id=current_message_id,
                    excluded_message_ids=tuple(_source_quarantine_message_ids),
                    limit=bounded_limit,
                    force_source_detail=(True if include_source_detail else None),
                    context_policy=policy,
                )
                cache.store(
                    (current_message_id, turn_id),
                    query_text=query_text,
                    execution=execution,
                    limit=bounded_limit,
                    source_detail_requested=requested_detail,
                    origin="active",
                )
        original_text = projection.text
        text = original_text.replace(
            "[你想起的相关经历｜本轮只呈现与眼前内容最相关的部分]",
            "[你进一步翻到的相关记忆]",
            1,
        )
        if text and text == original_text:
            text = "[你进一步翻到的相关记忆]\n\n" + text
        return finish({
            "status": "recalled" if text else "no_hits",
            "injected": bool(text),
            "multi_stage_fallback": stage_fallback,
            "cache_hit": cached is not None,
            "reused_from": cached.origin if cached is not None else "",
            "candidate_count": execution.result.candidate_count,
            "evaluated_count": execution.result.evaluated_count or 0,
            "returned_count": len(execution.result.hits),
            "rendered_hit_count": projection.rendered_hit_count,
            "rendered_chars": len(text),
            "semantic_available": execution.result.semantic_available,
            "semantic_status": execution.result.semantic_status,
            "evidence_expanded": execution.evidence.expanded,
            "truncated": projection.truncated,
        }, text)
    except Exception as exc:
        return finish({
            "status": "error",
            "injected": False,
            "error_type": type(exc).__name__,
        }, "")


def reset_context_shadow_services_for_tests() -> None:
    """Close singleton sidecars; intended for isolated tests only."""

    with _service_lock:
        states = list(_service_states.values())
        _service_states.clear()
        _source_quarantine_message_ids.clear()
    get_recall_session_cache().clear()
    services: list[MemoryV2ContextShadow] = []
    seen: set[int] = set()
    for state in states:
        for service in (state.service, state.previous_service):
            if service is not None and id(service) not in seen:
                seen.add(id(service))
                services.append(service)
    for service in services:
        service.close()


__all__ = [
    "ContextShadowObservation",
    "MemoryV2ContextShadow",
    "memory_v2_context_mode",
    "observe_context_shadow_from_environment",
    "quarantine_context_shadow_sources",
    "recall_context_from_environment",
    "search_memory_v2_from_environment",
    "refresh_context_shadow_for_source_commit",
    "refresh_context_shadow_for_source_mutation",
    "refresh_context_shadow_from_environment",
    "warm_context_shadow_from_environment",
]
