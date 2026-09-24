"""Memory V2 isolated foundation.

The package is deliberately not wired into the production context or OB_Rev
paths yet.  Its first responsibility is to make source-backed episodic writes
transactional, immutable, and replay-safe.
"""

from .models import (
    BatchSourceRef,
    BoundaryLinkJudgmentDraft,
    DayCompactItemDraft,
    DaySettlementCandidateDraft,
    DaySettlementDraft,
    DayThreadJudgmentDraft,
    EncodingBatch,
    EventDraft,
    EventLinkDraft,
    SourceRef,
)
from .day_settlement_persistence import (
    DaySettlementPersistenceError,
    prepare_day_settlement_draft,
)
from .conversation_source import (
    ConversationMessage,
    ConversationSource,
    ConversationSourceError,
)
from .store import (
    ImmutableConflictError,
    MemoryV2Store,
    SourceBoundaryError,
    ThreadConflictError,
)
from .day_settlement_orchestrator import (
    DaySettlementOrchestrationResult,
    settle_completed_day,
)
from .day_backfill_runner import run_backfill_day
from .periods import (
    PeriodGenerationCandidateReceipt,
    PeriodGenerationJobDraft,
    PeriodDateBasis,
    PeriodSummaryDraft,
    PeriodSummaryItemDraft,
    PeriodWindow,
    calendar_period_window,
    period_input_digest,
)
from .period_runner import (
    BlockedPeriod,
    PeriodContractError,
    PeriodGenerationCandidate,
    PeriodGenerationPlan,
    PeriodRunResult,
    build_period_json_schema,
    build_period_messages,
    parse_period_output,
    plan_period_generation,
    run_period_generation,
)
from .recall import (
    CachedEmbeddingSemanticScorer,
    EventRecallIndex,
    RecallDateBasis,
    RecallDecision,
    RecallQuery,
    RecallResult,
    RecallSourceAnchor,
)
from .recall_routing import (
    RecallRoutingHints,
    apply_recall_routing_hints,
    route_recall_query,
)
from .recall_evidence import (
    RecallEvidenceBundle,
    RecallEvidencePolicy,
    expand_recall_evidence,
    render_recall_evidence,
    render_recall_evidence_for_context,
)
from .recall_service import ShadowRecallExecution, run_shadow_recall
from .recall_context import (
    RecallContextPolicy,
    RecallContextProjection,
    build_recall_context,
)
from .multi_source_recall import ConversationWindowRecallIndex, MultiSourceRecallIndex
from .recall_candidates import SQLiteFTSCandidateSelector
from .semantic_candidates import (
    HybridRecallCandidateSelector,
    QueryInstructionEmbedder,
    SemanticIndexNotReadyError,
    SQLiteSemanticCandidateIndex,
    TextEmbedder,
)
from .semantic_profile import (
    LocalSemanticProfile,
    discover_local_semantic_profile,
    open_local_semantic_index,
)
from .context_shadow import (
    ContextShadowObservation,
    MemoryV2ContextShadow,
    memory_v2_context_mode,
    observe_context_shadow_from_environment,
    quarantine_context_shadow_sources,
    recall_context_from_environment,
    search_memory_v2_from_environment,
    refresh_context_shadow_for_source_commit,
    refresh_context_shadow_for_source_mutation,
    refresh_context_shadow_from_environment,
    warm_context_shadow_from_environment,
)
from .source_lifecycle import (
    configured_memory_v2_paths,
    reconcile_memory_v2_source_mutation,
    source_mutation_revision,
)
from .day_repair import (
    DAY_REPAIR_CONTRACT_VERSION,
    DayRepairPlan,
    DayRepairPlanner,
    DayRepairPlanningError,
)
from .day_repair_staging import (
    BatchEncoder,
    DayRepairEventReplacement,
    DayRepairStagingResult,
    DayRepairThreadProjection,
    execute_day_repair_staging,
)
from .day_repair_promotion import (
    DayRepairPromotionError,
    DayRepairPromotionResult,
    promote_day_repair,
)
from .day_repair_runner import DayRepairRunResult, run_day_repair
from .backfill import (
    BackfillDayPlan,
    BackfillDayProgress,
    BackfillPlan,
    BackfillWavePlan,
    execute_backfill_wave,
    inspect_backfill_progress,
    plan_backfill,
)
from .period_recall import PeriodRecallIndex
from .cognition_recall import CognitionRecallIndex, load_current_cognition_entries
from .period_experiment import (
    FlashPeriodModelAdapter,
    PeriodProviderError,
    run_flash_period_experiment,
)
from .trace_store import (
    MemoryRecallTraceStore,
    TRACE_INDEX_VERSION,
    collect_memory_sections,
    default_trace_db_path,
    get_default_trace_store,
    trace_enabled,
)

__all__ = [
    "BatchSourceRef",
    "BackfillDayPlan",
    "BackfillDayProgress",
    "BackfillPlan",
    "BackfillWavePlan",
    "BoundaryLinkJudgmentDraft",
    "DayCompactItemDraft",
    "DAY_REPAIR_CONTRACT_VERSION",
    "DayRepairPlan",
    "DayRepairPlanner",
    "DayRepairPlanningError",
    "DayRepairPromotionError",
    "DayRepairPromotionResult",
    "DayRepairRunResult",
    "BatchEncoder",
    "DayRepairEventReplacement",
    "DayRepairStagingResult",
    "DayRepairThreadProjection",
    "DaySettlementCandidateDraft",
    "DaySettlementDraft",
    "DaySettlementPersistenceError",
    "DaySettlementOrchestrationResult",
    "DayThreadJudgmentDraft",
    "EncodingBatch",
    "EventDraft",
    "EventLinkDraft",
    "ConversationMessage",
    "ConversationWindowRecallIndex",
    "CognitionRecallIndex",
    "ConversationSource",
    "ConversationSourceError",
    "ContextShadowObservation",
    "ImmutableConflictError",
    "EventRecallIndex",
    "FlashPeriodModelAdapter",
    "CachedEmbeddingSemanticScorer",
    "MemoryV2Store",
    "MemoryV2ContextShadow",
    "MemoryRecallTraceStore",
    "TRACE_INDEX_VERSION",
    "MultiSourceRecallIndex",
    "BlockedPeriod",
    "PeriodContractError",
    "PeriodGenerationCandidate",
    "PeriodGenerationCandidateReceipt",
    "PeriodGenerationJobDraft",
    "PeriodGenerationPlan",
    "PeriodProviderError",
    "PeriodRecallIndex",
    "PeriodRunResult",
    "PeriodSummaryDraft",
    "PeriodSummaryItemDraft",
    "PeriodWindow",
    "PeriodDateBasis",
    "RecallQuery",
    "RecallDateBasis",
    "RecallDecision",
    "RecallContextPolicy",
    "RecallContextProjection",
    "RecallResult",
    "RecallRoutingHints",
    "RecallSourceAnchor",
    "RecallEvidenceBundle",
    "RecallEvidencePolicy",
    "ShadowRecallExecution",
    "SourceBoundaryError",
    "SourceRef",
    "SQLiteFTSCandidateSelector",
    "HybridRecallCandidateSelector",
    "QueryInstructionEmbedder",
    "SemanticIndexNotReadyError",
    "SQLiteSemanticCandidateIndex",
    "TextEmbedder",
    "LocalSemanticProfile",
    "discover_local_semantic_profile",
    "open_local_semantic_index",
    "ThreadConflictError",
    "apply_recall_routing_hints",
    "calendar_period_window",
    "collect_memory_sections",
    "default_trace_db_path",
    "get_default_trace_store",
    "build_period_json_schema",
    "build_period_messages",
    "build_recall_context",
    "expand_recall_evidence",
    "execute_backfill_wave",
    "execute_day_repair_staging",
    "inspect_backfill_progress",
    "render_recall_evidence",
    "render_recall_evidence_for_context",
    "parse_period_output",
    "period_input_digest",
    "plan_backfill",
    "plan_period_generation",
    "promote_day_repair",
    "prepare_day_settlement_draft",
    "load_current_cognition_entries",
    "memory_v2_context_mode",
    "observe_context_shadow_from_environment",
    "quarantine_context_shadow_sources",
    "recall_context_from_environment",
    "search_memory_v2_from_environment",
    "reconcile_memory_v2_source_mutation",
    "refresh_context_shadow_for_source_commit",
    "refresh_context_shadow_for_source_mutation",
    "refresh_context_shadow_from_environment",
    "route_recall_query",
    "run_shadow_recall",
    "run_backfill_day",
    "run_day_repair",
    "settle_completed_day",
    "source_mutation_revision",
    "configured_memory_v2_paths",
    "run_period_generation",
    "run_flash_period_experiment",
    "trace_enabled",
    "warm_context_shadow_from_environment",
]
