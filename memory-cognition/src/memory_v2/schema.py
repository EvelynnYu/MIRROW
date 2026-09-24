"""SQLite schema for the isolated Memory V2 store."""

from __future__ import annotations


SCHEMA_VERSION = 10


DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_schema (
    schema_key TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    upgraded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS encoding_batches (
    id TEXT PRIMARY KEY,
    source_namespace TEXT NOT NULL,
    session_id TEXT NOT NULL,
    active_date TEXT NOT NULL,
    from_message_row_id INTEGER NOT NULL,
    to_message_row_id INTEGER NOT NULL,
    from_message_id TEXT NOT NULL,
    to_message_id TEXT NOT NULL,
    source_count INTEGER NOT NULL CHECK (source_count > 0),
    source_digest TEXT NOT NULL,
    encoder_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'error')),
    error_code TEXT NOT NULL DEFAULT '',
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    UNIQUE (
        source_namespace,
        session_id,
        from_message_row_id,
        to_message_row_id,
        encoder_version,
        prompt_version
    )
);

CREATE TABLE IF NOT EXISTS encoding_batch_sources (
    batch_id TEXT NOT NULL REFERENCES encoding_batches(id),
    message_row_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    active_date TEXT NOT NULL,
    calendar_date TEXT NOT NULL DEFAULT '',
    source_ts TEXT NOT NULL,
    source_role TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('chat', 'wander', 'sentinel', 'reminder')
    ),
    source_event_type TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    PRIMARY KEY (batch_id, message_id),
    UNIQUE (batch_id, message_row_id),
    UNIQUE (batch_id, source_order)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES encoding_batches(id),
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal >= 0),
    subject_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL,
    active_date TEXT NOT NULL,
    calendar_date TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    emotional_weight REAL NOT NULL CHECK (emotional_weight >= -1 AND emotional_weight <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    epistemic_status TEXT NOT NULL CHECK (
        epistemic_status IN (
            'explicit_report', 'direct_observation', 'model_inference', 'legacy'
        )
    ),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    content_digest TEXT NOT NULL,
    encoder_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_ordinal)
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id TEXT NOT NULL REFERENCES events(id),
    message_row_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_ts TEXT NOT NULL,
    source_role TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('chat', 'wander', 'sentinel', 'reminder')
    ),
    source_event_type TEXT NOT NULL DEFAULT '',
    scene_id TEXT NOT NULL DEFAULT '',
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    span_start INTEGER,
    span_end INTEGER,
    span_digest TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, message_id),
    UNIQUE (event_id, source_order),
    CHECK (
        (span_start IS NULL AND span_end IS NULL)
        OR (span_start IS NOT NULL AND span_end IS NOT NULL AND span_start < span_end)
    )
);

CREATE TABLE IF NOT EXISTS event_participants (
    event_id TEXT NOT NULL REFERENCES events(id),
    participant_id TEXT NOT NULL,
    participant_order INTEGER NOT NULL CHECK (participant_order >= 0),
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (event_id, participant_id),
    UNIQUE (event_id, participant_order)
);

CREATE TABLE IF NOT EXISTS event_status_log (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'superseded', 'invalid_source', 'retracted')
    ),
    reason_code TEXT NOT NULL DEFAULT '',
    replacement_event_id TEXT REFERENCES events(id),
    source_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_threads (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    thread_type TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_thread_status_log (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES event_threads(id),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'retracted', 'superseded')
    ),
    reason_code TEXT NOT NULL DEFAULT '',
    replacement_thread_id TEXT REFERENCES event_threads(id),
    source_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (thread_id, source_key)
);

CREATE TABLE IF NOT EXISTS event_links (
    id TEXT PRIMARY KEY,
    from_event_id TEXT NOT NULL REFERENCES events(id),
    to_event_id TEXT NOT NULL REFERENCES events(id),
    link_type TEXT NOT NULL CHECK (
        link_type IN (
            'same_thread', 'continues', 'causes', 'results_in',
            'contradicts', 'updates_state', 'similar_context'
        )
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (from_event_id, to_event_id, link_type)
);

CREATE TABLE IF NOT EXISTS thread_events (
    thread_id TEXT NOT NULL REFERENCES event_threads(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, event_id),
    UNIQUE (thread_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS boundary_link_judgments (
    id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL UNIQUE,
    previous_batch_id TEXT NOT NULL REFERENCES encoding_batches(id),
    next_batch_id TEXT NOT NULL REFERENCES encoding_batches(id),
    source_namespace TEXT NOT NULL,
    linker_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'explicit_no_link', 'below_threshold',
            'accepted', 'skipped_empty_side'
        )
    ),
    minimum_confidence REAL NOT NULL CHECK (
        minimum_confidence >= 0 AND minimum_confidence <= 1
    ),
    candidate_from_event_id TEXT REFERENCES events(id),
    candidate_to_event_id TEXT REFERENCES events(id),
    confidence REAL CHECK (confidence >= 0 AND confidence <= 1),
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    accepted_link_id TEXT REFERENCES event_links(id),
    created_at TEXT NOT NULL,
    CHECK (previous_batch_id <> next_batch_id),
    CHECK (
        (outcome IN ('explicit_no_link', 'skipped_empty_side')
            AND candidate_from_event_id IS NULL
            AND candidate_to_event_id IS NULL
            AND confidence IS NULL
            AND accepted_link_id IS NULL)
        OR (outcome = 'below_threshold'
            AND candidate_from_event_id IS NOT NULL
            AND candidate_to_event_id IS NOT NULL
            AND confidence IS NOT NULL
            AND confidence < minimum_confidence
            AND accepted_link_id IS NULL)
        OR (outcome = 'accepted'
            AND candidate_from_event_id IS NOT NULL
            AND candidate_to_event_id IS NOT NULL
            AND confidence IS NOT NULL
            AND confidence >= minimum_confidence
            AND accepted_link_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS day_settlements (
    id TEXT PRIMARY KEY,
    source_namespace TEXT NOT NULL,
    active_date TEXT NOT NULL,
    settlement_version TEXT NOT NULL,
    assembler_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    execution_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    minimum_continuation_confidence REAL NOT NULL CHECK (
        minimum_continuation_confidence >= 0
        AND minimum_continuation_confidence <= 1
    ),
    day_event_count INTEGER NOT NULL CHECK (day_event_count > 0),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    compact_item_count INTEGER NOT NULL CHECK (compact_item_count > 0),
    judgment_count INTEGER NOT NULL CHECK (judgment_count >= 0),
    accepted_continuation_count INTEGER NOT NULL CHECK (
        accepted_continuation_count >= 0
    ),
    created_at TEXT NOT NULL,
    UNIQUE (
        source_namespace, active_date, settlement_version, prompt_version
    )
);

CREATE TABLE IF NOT EXISTS day_compact_items (
    id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES day_settlements(id),
    item_order INTEGER NOT NULL CHECK (item_order >= 0),
    summary TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (settlement_id, item_order)
);

CREATE TABLE IF NOT EXISTS day_compact_item_events (
    settlement_id TEXT NOT NULL REFERENCES day_settlements(id),
    item_order INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    event_order INTEGER NOT NULL CHECK (event_order >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (settlement_id, event_id),
    UNIQUE (settlement_id, item_order, event_order),
    FOREIGN KEY (settlement_id, item_order)
        REFERENCES day_compact_items(settlement_id, item_order)
);

CREATE TABLE IF NOT EXISTS day_thread_judgments (
    id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES day_settlements(id),
    candidate_ref TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('continue', 'separate', 'unknown')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reason_codes_json TEXT NOT NULL,
    from_event_id TEXT NOT NULL REFERENCES events(id),
    match_event_id TEXT NOT NULL REFERENCES events(id),
    to_event_id TEXT NOT NULL REFERENCES events(id),
    prior_thread_id TEXT REFERENCES event_threads(id),
    accepted_link_id TEXT REFERENCES event_links(id),
    created_at TEXT NOT NULL,
    UNIQUE (settlement_id, candidate_ref),
    CHECK (from_event_id <> to_event_id),
    CHECK (accepted_link_id IS NULL OR outcome = 'continue')
);

CREATE TABLE IF NOT EXISTS period_summary_versions (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('day', 'week', 'month')),
    date_basis TEXT NOT NULL CHECK (date_basis IN ('active_date', 'calendar_date')),
    period_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (namespace, period_kind, date_basis, period_key, revision),
    CHECK (date_from <= date_to)
);

CREATE TABLE IF NOT EXISTS period_summary_event_inputs (
    period_summary_id TEXT NOT NULL REFERENCES period_summary_versions(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    input_order INTEGER NOT NULL CHECK (input_order >= 0),
    PRIMARY KEY (period_summary_id, event_id),
    UNIQUE (period_summary_id, input_order)
);

CREATE TABLE IF NOT EXISTS period_summary_parent_inputs (
    period_summary_id TEXT NOT NULL REFERENCES period_summary_versions(id),
    parent_summary_id TEXT NOT NULL REFERENCES period_summary_versions(id),
    input_order INTEGER NOT NULL CHECK (input_order >= 0),
    PRIMARY KEY (period_summary_id, parent_summary_id),
    UNIQUE (period_summary_id, input_order),
    CHECK (period_summary_id <> parent_summary_id)
);

CREATE TABLE IF NOT EXISTS period_summary_items (
    id TEXT PRIMARY KEY,
    period_summary_id TEXT NOT NULL REFERENCES period_summary_versions(id),
    item_order INTEGER NOT NULL CHECK (item_order >= 0),
    item_kind TEXT NOT NULL CHECK (
        item_kind IN ('timeline', 'continuity', 'state_change')
    ),
    summary TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (period_summary_id, item_order)
);

CREATE TABLE IF NOT EXISTS period_item_event_sources (
    period_item_id TEXT NOT NULL REFERENCES period_summary_items(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    PRIMARY KEY (period_item_id, event_id),
    UNIQUE (period_item_id, source_order)
);

CREATE TABLE IF NOT EXISTS period_item_parent_sources (
    period_item_id TEXT NOT NULL REFERENCES period_summary_items(id),
    parent_item_id TEXT NOT NULL REFERENCES period_summary_items(id),
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    PRIMARY KEY (period_item_id, parent_item_id),
    UNIQUE (period_item_id, source_order),
    CHECK (period_item_id <> parent_item_id)
);

CREATE TABLE IF NOT EXISTS period_generation_jobs (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('day', 'week', 'month')),
    date_basis TEXT NOT NULL CHECK (date_basis IN ('active_date', 'calendar_date')),
    reference_date TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    estimated_model_calls INTEGER NOT NULL CHECK (estimated_model_calls >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (
        namespace, period_kind, date_basis, reference_date,
        generator_version, prompt_version, plan_digest
    )
);

CREATE TABLE IF NOT EXISTS period_generation_job_candidates (
    job_id TEXT NOT NULL REFERENCES period_generation_jobs(id),
    candidate_order INTEGER NOT NULL CHECK (candidate_order >= 0),
    period_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    input_digest TEXT NOT NULL,
    input_count INTEGER NOT NULL CHECK (input_count > 0),
    model_call_required INTEGER NOT NULL CHECK (model_call_required IN (0, 1)),
    PRIMARY KEY (job_id, candidate_order),
    UNIQUE (job_id, period_key)
);

CREATE TABLE IF NOT EXISTS period_generation_job_transitions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES period_generation_jobs(id),
    transition_order INTEGER NOT NULL CHECK (transition_order >= 0),
    state TEXT NOT NULL CHECK (state IN ('planned', 'running', 'completed', 'error')),
    completed_candidate_count INTEGER NOT NULL CHECK (completed_candidate_count >= 0),
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (job_id, transition_order)
);

CREATE TABLE IF NOT EXISTS period_generation_job_outputs (
    job_id TEXT NOT NULL REFERENCES period_generation_jobs(id),
    candidate_order INTEGER NOT NULL,
    period_summary_id TEXT NOT NULL REFERENCES period_summary_versions(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, candidate_order),
    FOREIGN KEY (job_id, candidate_order)
        REFERENCES period_generation_job_candidates(job_id, candidate_order)
);

CREATE TABLE IF NOT EXISTS migration_receipts (
    id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('pending', 'migrated', 'skipped', 'error')),
    reason_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (source_system, source_type, source_id, source_digest)
);

CREATE TABLE IF NOT EXISTS day_repair_receipts (
    id TEXT PRIMARY KEY,
    source_namespace TEXT NOT NULL,
    active_date TEXT NOT NULL,
    repair_contract_version TEXT NOT NULL,
    source_revision TEXT NOT NULL UNIQUE,
    authority_digest TEXT NOT NULL,
    baseline_digest TEXT NOT NULL,
    stage_result_digest TEXT NOT NULL,
    repair_encoder_version TEXT NOT NULL,
    repair_settlement_version TEXT NOT NULL,
    batch_count INTEGER NOT NULL CHECK (batch_count >= 0),
    new_event_count INTEGER NOT NULL CHECK (new_event_count >= 0),
    superseded_event_count INTEGER NOT NULL CHECK (superseded_event_count >= 0),
    retracted_event_count INTEGER NOT NULL CHECK (retracted_event_count >= 0),
    replacement_thread_count INTEGER NOT NULL CHECK (replacement_thread_count >= 0),
    retracted_thread_count INTEGER NOT NULL CHECK (retracted_thread_count >= 0),
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (source_namespace, active_date, authority_digest, baseline_digest)
);

CREATE INDEX IF NOT EXISTS idx_batches_active_date
    ON encoding_batches(active_date, from_message_row_id);
CREATE INDEX IF NOT EXISTS idx_batch_sources_message
    ON encoding_batch_sources(message_id);
CREATE INDEX IF NOT EXISTS idx_events_active_date
    ON events(active_date, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_subject_type
    ON events(subject_id, event_type);
CREATE INDEX IF NOT EXISTS idx_event_sources_message
    ON event_sources(message_id);
CREATE INDEX IF NOT EXISTS idx_event_sources_scene
    ON event_sources(scene_id);
CREATE INDEX IF NOT EXISTS idx_event_participants_subject
    ON event_participants(participant_id, event_id);
CREATE INDEX IF NOT EXISTS idx_event_status_event
    ON event_status_log(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_links_to
    ON event_links(to_event_id, link_type);
CREATE INDEX IF NOT EXISTS idx_event_thread_status_thread
    ON event_thread_status_log(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_thread_events_event
    ON thread_events(event_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_boundary_judgments_batches
    ON boundary_link_judgments(previous_batch_id, next_batch_id);
CREATE INDEX IF NOT EXISTS idx_day_settlements_date
    ON day_settlements(source_namespace, active_date);
CREATE INDEX IF NOT EXISTS idx_day_compact_events_event
    ON day_compact_item_events(event_id, settlement_id);
CREATE INDEX IF NOT EXISTS idx_day_thread_judgments_events
    ON day_thread_judgments(to_event_id, from_event_id);
CREATE INDEX IF NOT EXISTS idx_period_summaries_key
    ON period_summary_versions(namespace, period_kind, date_basis, period_key, revision DESC);
CREATE INDEX IF NOT EXISTS idx_period_event_inputs_event
    ON period_summary_event_inputs(event_id, period_summary_id);
CREATE INDEX IF NOT EXISTS idx_period_parent_inputs_parent
    ON period_summary_parent_inputs(parent_summary_id, period_summary_id);
CREATE INDEX IF NOT EXISTS idx_period_items_summary
    ON period_summary_items(period_summary_id, item_order);
CREATE INDEX IF NOT EXISTS idx_period_item_event_sources_event
    ON period_item_event_sources(event_id, period_item_id);
CREATE INDEX IF NOT EXISTS idx_period_item_parent_sources_parent
    ON period_item_parent_sources(parent_item_id, period_item_id);
CREATE INDEX IF NOT EXISTS idx_period_generation_jobs_created
    ON period_generation_jobs(created_at, id);
CREATE INDEX IF NOT EXISTS idx_period_generation_transitions_job
    ON period_generation_job_transitions(job_id, transition_order);
CREATE INDEX IF NOT EXISTS idx_period_generation_outputs_summary
    ON period_generation_job_outputs(period_summary_id);
CREATE INDEX IF NOT EXISTS idx_day_repair_receipts_date
    ON day_repair_receipts(source_namespace, active_date, created_at);
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS encoding_batches_identity_no_update
BEFORE UPDATE OF
    source_namespace, session_id, active_date,
    from_message_row_id, to_message_row_id, from_message_id, to_message_id,
    source_count, source_digest, encoder_version, prompt_version
ON encoding_batches BEGIN
    SELECT RAISE(ABORT, 'encoding batch identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS encoding_batch_sources_no_update
BEFORE UPDATE ON encoding_batch_sources BEGIN
    SELECT RAISE(ABORT, 'encoding batch sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS encoding_batch_sources_no_delete
BEFORE DELETE ON encoding_batch_sources BEGIN
    SELECT RAISE(ABORT, 'encoding batch sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_sources_no_update
BEFORE UPDATE ON event_sources BEGIN
    SELECT RAISE(ABORT, 'event sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_sources_no_delete
BEFORE DELETE ON event_sources BEGIN
    SELECT RAISE(ABORT, 'event sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_participants_no_update
BEFORE UPDATE ON event_participants BEGIN
    SELECT RAISE(ABORT, 'event participants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_participants_no_delete
BEFORE DELETE ON event_participants BEGIN
    SELECT RAISE(ABORT, 'event participants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_status_log_no_update
BEFORE UPDATE ON event_status_log BEGIN
    SELECT RAISE(ABORT, 'event status records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_status_log_no_delete
BEFORE DELETE ON event_status_log BEGIN
    SELECT RAISE(ABORT, 'event status records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_links_no_update
BEFORE UPDATE ON event_links BEGIN
    SELECT RAISE(ABORT, 'event links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_links_no_delete
BEFORE DELETE ON event_links BEGIN
    SELECT RAISE(ABORT, 'event links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_threads_no_update
BEFORE UPDATE ON event_threads BEGIN
    SELECT RAISE(ABORT, 'event threads are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_threads_no_delete
BEFORE DELETE ON event_threads BEGIN
    SELECT RAISE(ABORT, 'event threads are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_thread_status_log_no_update
BEFORE UPDATE ON event_thread_status_log BEGIN
    SELECT RAISE(ABORT, 'event thread status records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_thread_status_log_no_delete
BEFORE DELETE ON event_thread_status_log BEGIN
    SELECT RAISE(ABORT, 'event thread status records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thread_events_no_update
BEFORE UPDATE ON thread_events BEGIN
    SELECT RAISE(ABORT, 'thread membership is append-only');
END;
CREATE TRIGGER IF NOT EXISTS thread_events_no_delete
BEFORE DELETE ON thread_events BEGIN
    SELECT RAISE(ABORT, 'thread membership is append-only');
END;
CREATE TRIGGER IF NOT EXISTS boundary_link_judgments_no_update
BEFORE UPDATE ON boundary_link_judgments BEGIN
    SELECT RAISE(ABORT, 'boundary judgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS boundary_link_judgments_no_delete
BEFORE DELETE ON boundary_link_judgments BEGIN
    SELECT RAISE(ABORT, 'boundary judgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_settlements_no_update
BEFORE UPDATE ON day_settlements BEGIN
    SELECT RAISE(ABORT, 'day settlements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_settlements_no_delete
BEFORE DELETE ON day_settlements BEGIN
    SELECT RAISE(ABORT, 'day settlements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_compact_items_no_update
BEFORE UPDATE ON day_compact_items BEGIN
    SELECT RAISE(ABORT, 'day compact items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_compact_items_no_delete
BEFORE DELETE ON day_compact_items BEGIN
    SELECT RAISE(ABORT, 'day compact items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_compact_item_events_no_update
BEFORE UPDATE ON day_compact_item_events BEGIN
    SELECT RAISE(ABORT, 'day compact sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_compact_item_events_no_delete
BEFORE DELETE ON day_compact_item_events BEGIN
    SELECT RAISE(ABORT, 'day compact sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_thread_judgments_no_update
BEFORE UPDATE ON day_thread_judgments BEGIN
    SELECT RAISE(ABORT, 'day thread judgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_thread_judgments_no_delete
BEFORE DELETE ON day_thread_judgments BEGIN
    SELECT RAISE(ABORT, 'day thread judgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summaries_no_update
BEFORE UPDATE ON period_summary_versions BEGIN
    SELECT RAISE(ABORT, 'period summary versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summaries_no_delete
BEFORE DELETE ON period_summary_versions BEGIN
    SELECT RAISE(ABORT, 'period summary versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_event_inputs_no_update
BEFORE UPDATE ON period_summary_event_inputs BEGIN
    SELECT RAISE(ABORT, 'period summary event inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_event_inputs_no_delete
BEFORE DELETE ON period_summary_event_inputs BEGIN
    SELECT RAISE(ABORT, 'period summary event inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_parent_inputs_no_update
BEFORE UPDATE ON period_summary_parent_inputs BEGIN
    SELECT RAISE(ABORT, 'period summary parent inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_parent_inputs_no_delete
BEFORE DELETE ON period_summary_parent_inputs BEGIN
    SELECT RAISE(ABORT, 'period summary parent inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_items_no_update
BEFORE UPDATE ON period_summary_items BEGIN
    SELECT RAISE(ABORT, 'period summary items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_summary_items_no_delete
BEFORE DELETE ON period_summary_items BEGIN
    SELECT RAISE(ABORT, 'period summary items are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_item_event_sources_no_update
BEFORE UPDATE ON period_item_event_sources BEGIN
    SELECT RAISE(ABORT, 'period item event sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_item_event_sources_no_delete
BEFORE DELETE ON period_item_event_sources BEGIN
    SELECT RAISE(ABORT, 'period item event sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_item_parent_sources_no_update
BEFORE UPDATE ON period_item_parent_sources BEGIN
    SELECT RAISE(ABORT, 'period item parent sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_item_parent_sources_no_delete
BEFORE DELETE ON period_item_parent_sources BEGIN
    SELECT RAISE(ABORT, 'period item parent sources are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_jobs_no_update
BEFORE UPDATE ON period_generation_jobs BEGIN
    SELECT RAISE(ABORT, 'period generation jobs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_jobs_no_delete
BEFORE DELETE ON period_generation_jobs BEGIN
    SELECT RAISE(ABORT, 'period generation jobs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_candidates_no_update
BEFORE UPDATE ON period_generation_job_candidates BEGIN
    SELECT RAISE(ABORT, 'period generation candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_candidates_no_delete
BEFORE DELETE ON period_generation_job_candidates BEGIN
    SELECT RAISE(ABORT, 'period generation candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_transitions_no_update
BEFORE UPDATE ON period_generation_job_transitions BEGIN
    SELECT RAISE(ABORT, 'period generation transitions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_transitions_no_delete
BEFORE DELETE ON period_generation_job_transitions BEGIN
    SELECT RAISE(ABORT, 'period generation transitions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_outputs_no_update
BEFORE UPDATE ON period_generation_job_outputs BEGIN
    SELECT RAISE(ABORT, 'period generation outputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS period_generation_job_outputs_no_delete
BEFORE DELETE ON period_generation_job_outputs BEGIN
    SELECT RAISE(ABORT, 'period generation outputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_repair_receipts_no_update
BEFORE UPDATE ON day_repair_receipts BEGIN
    SELECT RAISE(ABORT, 'day repair receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS day_repair_receipts_no_delete
BEFORE DELETE ON day_repair_receipts BEGIN
    SELECT RAISE(ABORT, 'day repair receipts are immutable');
END;
"""
