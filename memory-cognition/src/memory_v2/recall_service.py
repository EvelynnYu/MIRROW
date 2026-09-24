"""One-call orchestration for the isolated Memory V2 shadow recall path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .conversation_source import ConversationSource
from .recall import EventRecallIndex, RecallQuery, RecallResult
from .recall_evidence import (
    RecallEvidenceBundle,
    RecallEvidencePolicy,
    expand_recall_evidence,
)
from .recall_routing import (
    RecallRoutingHints,
    apply_recall_routing_hints,
    route_recall_query,
)
from .recall_query import RecallQueryEnvelope


@dataclass(frozen=True)
class ShadowRecallExecution:
    query: RecallQuery
    routing: RecallRoutingHints
    result: RecallResult
    evidence: RecallEvidenceBundle
    query_envelope: RecallQueryEnvelope | None = None

    def safe_observation(self) -> dict[str, object]:
        return {
            "routing": self.routing.safe_observation(),
            "query_envelope": (
                self.query_envelope.safe_observation()
                if self.query_envelope is not None
                else {}
            ),
            "candidate_count": self.result.candidate_count,
            "evaluated_count": self.result.evaluated_count,
            "returned_count": len(self.result.hits),
            "semantic_available": self.result.semantic_available,
            "semantic_status": self.result.semantic_status,
            "evidence": self.evidence.safe_observation(),
        }


def run_shadow_recall(
    index: EventRecallIndex,
    source: ConversationSource,
    query: RecallQuery,
    *,
    reference_date: date,
    limit: int = 5,
    evidence_policy: RecallEvidencePolicy | None = None,
    force_source_detail: bool | None = None,
    query_envelope: RecallQueryEnvelope | None = None,
) -> ShadowRecallExecution:
    """Route, rank, and optionally open evidence without any model call."""

    routing = route_recall_query(query.text, reference_date=reference_date)
    routed_query = apply_recall_routing_hints(query, routing)
    result = index.search(routed_query, limit=limit)
    evidence = expand_recall_evidence(
        source,
        result.hits,
        needs_source_detail=(
            routing.needs_source_detail
            if force_source_detail is None
            else bool(force_source_detail)
        ),
        policy=evidence_policy,
    )
    return ShadowRecallExecution(
        query=routed_query,
        routing=routing,
        result=result,
        evidence=evidence,
        query_envelope=query_envelope,
    )


__all__ = ["ShadowRecallExecution", "run_shadow_recall"]
