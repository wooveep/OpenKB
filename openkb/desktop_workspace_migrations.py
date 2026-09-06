"""Additive SQLite statements owned by Desktop Runtime schema migrations."""

from __future__ import annotations

MODEL_CALL_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE model_calls (
        call_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        operation TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'running', 'retry_wait', 'completed', 'failed'
        )),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
        timeout_seconds REAL NOT NULL CHECK(timeout_seconds >= 0),
        next_timeout_seconds REAL,
        remaining_seconds REAL NOT NULL CHECK(remaining_seconds >= 0),
        error_code TEXT,
        reason TEXT,
        suggested_action TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE model_attempts (
        call_id TEXT NOT NULL REFERENCES model_calls(call_id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL CHECK(attempt >= 1),
        status TEXT NOT NULL CHECK(status IN (
            'running', 'retry_wait', 'completed', 'failed'
        )),
        timeout_seconds REAL NOT NULL CHECK(timeout_seconds >= 0),
        remaining_seconds REAL NOT NULL CHECK(remaining_seconds >= 0),
        error_code TEXT,
        reason TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY(call_id, attempt)
    )
    """,
    """
    CREATE TABLE quarantined_documents (
        job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id),
        stage TEXT NOT NULL,
        error_code TEXT NOT NULL,
        reason TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
        created_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO stage_runs (
        stage_run_id, job_id, stage, status, progress, error_code, started_at, completed_at
    )
    SELECT
        lower(hex(randomblob(16))), import_jobs.job_id, 'model_analysis',
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled') THEN 'skipped'
            ELSE 'pending'
        END,
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled') THEN 100
            ELSE 0
        END,
        NULL,
        NULL,
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled')
            THEN COALESCE(import_jobs.completed_at, import_jobs.created_at)
            ELSE NULL
        END
    FROM import_jobs
    LEFT JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_runs existing
        WHERE existing.job_id = import_jobs.job_id AND existing.stage = 'model_analysis'
    )
    """,
    """
    INSERT INTO stage_run_runtime (
        stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
    )
    SELECT
        stage_runs.stage_run_id, stage_runs.job_id, stage_runs.status,
        NULL, stage_runs.error_code,
        COALESCE(stage_runs.completed_at, import_job_runtime.updated_at, import_jobs.created_at)
    FROM stage_runs
    JOIN import_jobs ON import_jobs.job_id = stage_runs.job_id
    LEFT JOIN import_job_runtime ON import_job_runtime.job_id = stage_runs.job_id
    WHERE stage_runs.stage = 'model_analysis'
        AND NOT EXISTS (
            SELECT 1 FROM stage_run_runtime existing
            WHERE existing.stage_run_id = stage_runs.stage_run_id
        )
    """,
    """
    CREATE INDEX model_calls_job_idx ON model_calls(job_id, created_at)
    """,
    """
    CREATE INDEX model_attempts_call_idx ON model_attempts(call_id, attempt)
    """,
)


RECOVERY_RUN_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE recovery_runs (
        recovery_run_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        model_override TEXT,
        initial_timeout_seconds REAL,
        status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
        started_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX recovery_runs_job_idx ON recovery_runs(job_id, started_at DESC)
    """,
)


RAW_ASSET_INTEGRITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE raw_asset_integrity (
        asset_sha256 TEXT PRIMARY KEY REFERENCES raw_assets(asset_sha256) ON DELETE CASCADE,
        lifecycle_status TEXT NOT NULL DEFAULT 'available'
        CHECK(lifecycle_status IN ('available', 'quarantined')),
        integrity_error_code TEXT,
        verified_at TEXT
    )
    """,
    """
    INSERT INTO raw_asset_integrity (
        asset_sha256, lifecycle_status, integrity_error_code, verified_at
    )
    SELECT asset_sha256, 'available', NULL, NULL FROM raw_assets
    """,
    """
    CREATE TRIGGER raw_assets_create_integrity
    AFTER INSERT ON raw_assets
    BEGIN
        INSERT INTO raw_asset_integrity (
            asset_sha256, lifecycle_status, integrity_error_code, verified_at
        )
        VALUES (NEW.asset_sha256, 'available', NULL, NULL);
    END
    """,
    """
    CREATE INDEX raw_asset_integrity_lifecycle_idx
        ON raw_asset_integrity(lifecycle_status, verified_at DESC)
    """,
)


SOURCE_IMAGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE source_images (
        source_image_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id)
            ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        image_sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
        media_type TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        display_name TEXT NOT NULL,
        alt_text TEXT,
        locator_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(document_id, ordinal)
    )
    """,
    """
    CREATE INDEX source_images_document_idx
        ON source_images(document_id, ordinal)
    """,
)


GROUNDED_ANSWER_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE grounded_answers (
        answer_id TEXT PRIMARY KEY,
        question TEXT NOT NULL,
        answer_text TEXT NOT NULL,
        retrieval_plan_json TEXT NOT NULL,
        degradations_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        completed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE grounded_answer_citations (
        answer_id TEXT NOT NULL REFERENCES grounded_answers(answer_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        channels_json TEXT NOT NULL,
        PRIMARY KEY(answer_id, evidence_id)
    )
    """,
    """
    CREATE INDEX grounded_answer_citations_answer_idx
        ON grounded_answer_citations(answer_id, ordinal)
    """,
)


GROUNDED_ANSWER_SOURCE_IMAGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE grounded_answer_source_images (
        answer_id TEXT NOT NULL REFERENCES grounded_answers(answer_id) ON DELETE CASCADE,
        source_image_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        PRIMARY KEY(answer_id, source_image_id),
        FOREIGN KEY(answer_id, evidence_id)
            REFERENCES grounded_answer_citations(answer_id, evidence_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX grounded_answer_source_images_answer_idx
        ON grounded_answer_source_images(answer_id, ordinal)
    """,
)


INTERRUPTED_ANSWER_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE grounded_answers
        ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'
        CHECK(status IN ('completed', 'interrupted'))
    """,
    """
    ALTER TABLE grounded_answers ADD COLUMN interruption_code TEXT
    """,
    """
    ALTER TABLE grounded_answers ADD COLUMN interruption_reason TEXT
    """,
    """
    ALTER TABLE grounded_answers ADD COLUMN updated_at TEXT
    """,
    """
    UPDATE grounded_answers SET updated_at = completed_at WHERE updated_at IS NULL
    """,
    """
    CREATE INDEX grounded_answers_status_created_idx
        ON grounded_answers(status, created_at DESC)
    """,
)


# Conversations deliberately do not backfill the earlier flat grounded_answers
# history: there is no reliable way to infer message ordering or ownership.
CONVERSATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE conversations (
        conversation_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        draft_text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE conversation_messages (
        message_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
            ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN (
            'completed', 'generating', 'interrupted'
        )),
        reply_to_message_id TEXT REFERENCES conversation_messages(message_id)
            ON DELETE CASCADE,
        selected_answer_version_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(conversation_id, ordinal)
    )
    """,
    """
    CREATE TABLE conversation_answer_versions (
        answer_version_id TEXT PRIMARY KEY,
        assistant_message_id TEXT NOT NULL
            REFERENCES conversation_messages(message_id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL CHECK(version_number >= 1),
        answer_text TEXT NOT NULL,
        retrieval_plan_json TEXT NOT NULL,
        degradations_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('completed', 'interrupted')),
        interruption_code TEXT,
        interruption_reason TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(assistant_message_id, version_number)
    )
    """,
    """
    CREATE TABLE conversation_answer_citations (
        answer_version_id TEXT NOT NULL
            REFERENCES conversation_answer_versions(answer_version_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        channels_json TEXT NOT NULL,
        PRIMARY KEY(answer_version_id, evidence_id)
    )
    """,
    """
    CREATE TABLE conversation_answer_source_images (
        answer_version_id TEXT NOT NULL,
        source_image_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        media_type TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        alt_text TEXT,
        locator_json TEXT NOT NULL,
        PRIMARY KEY(answer_version_id, source_image_id),
        FOREIGN KEY(answer_version_id, evidence_id)
            REFERENCES conversation_answer_citations(answer_version_id, evidence_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE conversation_ui_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        last_conversation_id TEXT REFERENCES conversations(conversation_id)
            ON DELETE SET NULL
    )
    """,
    """
    INSERT INTO conversation_ui_state (singleton, last_conversation_id)
    VALUES (1, NULL)
    """,
    """
    CREATE INDEX conversations_updated_idx ON conversations(updated_at DESC)
    """,
    """
    CREATE INDEX conversation_messages_order_idx
        ON conversation_messages(conversation_id, ordinal)
    """,
    """
    CREATE INDEX conversation_answer_versions_message_idx
        ON conversation_answer_versions(assistant_message_id, version_number)
    """,
)


KNOWLEDGE_PAGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_pages (
        page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        materialized_path TEXT NOT NULL UNIQUE,
        current_revision_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(kind, normalized_title)
    )
    """,
    """
    CREATE TABLE knowledge_page_revisions (
        revision_id TEXT PRIMARY KEY,
        page_id TEXT NOT NULL REFERENCES knowledge_pages(page_id) ON DELETE CASCADE,
        revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
        title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(page_id, revision_number)
    )
    """,
    """
    CREATE INDEX knowledge_pages_kind_updated_idx
        ON knowledge_pages(kind, updated_at DESC)
    """,
    """
    CREATE INDEX knowledge_page_revisions_page_revision_idx
        ON knowledge_page_revisions(page_id, revision_number DESC)
    """,
)


DEDUPLICATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE document_content_fingerprints (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        normalized_body_sha256 TEXT NOT NULL,
        canonical_document_id TEXT REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX document_content_fingerprints_lookup_idx
        ON document_content_fingerprints(normalized_body_sha256, canonical_document_id)
    """,
    """
    CREATE TABLE evidence_fingerprints (
        evidence_sha256 TEXT PRIMARY KEY,
        evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence_refs(evidence_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE evidence_occurrences (
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        block_id TEXT NOT NULL REFERENCES document_ir_blocks(block_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        PRIMARY KEY(document_id, block_id),
        UNIQUE(document_id, ordinal)
    )
    """,
    """
    CREATE INDEX evidence_occurrences_evidence_idx
        ON evidence_occurrences(evidence_id, document_id, ordinal)
    """,
    """
    CREATE TABLE import_deduplications (
        job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        level TEXT NOT NULL CHECK(level IN ('D0', 'D1', 'D2')),
        reason TEXT NOT NULL,
        reused_document_id TEXT REFERENCES source_documents(document_id) ON DELETE SET NULL,
        reused_evidence_count INTEGER NOT NULL DEFAULT 0 CHECK(reused_evidence_count >= 0),
        normalized_body_sha256 TEXT,
        reusable_stages_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


DOCUMENT_VERSION_CANDIDATE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE document_version_sources (
        source_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE document_version_members (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL REFERENCES document_version_sources(source_id) ON DELETE RESTRICT,
        linked_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO document_version_sources (source_id, created_at)
    SELECT document_id, created_at
    FROM source_documents
    """,
    """
    INSERT INTO document_version_members (document_id, source_id, linked_at)
    SELECT document_id, document_id, created_at
    FROM source_documents
    """,
    """
    CREATE TRIGGER source_documents_create_version_source
    AFTER INSERT ON source_documents
    BEGIN
        INSERT INTO document_version_sources (source_id, created_at)
        VALUES (NEW.document_id, NEW.created_at);
        INSERT INTO document_version_members (document_id, source_id, linked_at)
        VALUES (NEW.document_id, NEW.document_id, NEW.created_at);
    END
    """,
    """
    CREATE TABLE document_version_candidates (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        candidate_document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        lexical_score REAL NOT NULL CHECK(lexical_score >= 0.0 AND lexical_score <= 1.0),
        character_score REAL NOT NULL CHECK(character_score >= 0.0 AND character_score <= 1.0),
        reason TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected', 'dismissed')),
        resolution TEXT CHECK(
            resolution IS NULL
            OR resolution IN (
                'linked_existing_source', 'kept_independent', 'other_candidate_selected'
            )
        ),
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        CHECK(document_id <> candidate_document_id),
        UNIQUE(document_id, candidate_document_id)
    )
    """,
    """
    CREATE INDEX document_version_candidates_pending_idx
        ON document_version_candidates(status, document_id, created_at DESC)
    """,
    """
    CREATE INDEX document_version_members_source_idx
        ON document_version_members(source_id, linked_at)
    """,
)


# Knowledge reconciliation deliberately has its own lineage.  A document-version
# relationship is about source identity; a reconciliation candidate is about a
# proposed Concept or Entity change and must never rewrite that relationship.
KNOWLEDGE_RECONCILIATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_generations (
        generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_generation_id INTEGER REFERENCES knowledge_generations(generation_id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE knowledge_generation_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        current_generation_id INTEGER NOT NULL
            REFERENCES knowledge_generations(generation_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE knowledge_generation_items (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
        item_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        source_document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, item_key),
        UNIQUE(generation_id, kind, normalized_title)
    )
    """,
    """
    CREATE INDEX knowledge_generation_items_current_lookup_idx
        ON knowledge_generation_items(generation_id, kind, normalized_title)
    """,
    """
    CREATE TABLE knowledge_reconciliation_candidates (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        source_block_id TEXT REFERENCES document_ir_blocks(block_id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        classification TEXT NOT NULL CHECK(classification IN (
            'duplicate', 'compatible_addition', 'conflict'
        )),
        status TEXT NOT NULL CHECK(status IN ('auto_reconciled', 'pending_conflict')),
        baseline_kind TEXT CHECK(baseline_kind IN ('published_generation', 'user_revision')),
        baseline_id TEXT,
        baseline_title TEXT,
        baseline_content_markdown TEXT,
        observed_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_review_idx
        ON knowledge_reconciliation_candidates(status, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_document_idx
        ON knowledge_reconciliation_candidates(document_id, created_at DESC)
    """,
)


# Review choices are durable only inside the reconciliation queue.  A resolution
# record deliberately retains identifiers, outcome and time, but never a copy of
# the discarded derived Markdown.
KNOWLEDGE_RECONCILIATION_RESOLUTION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_reconciliation_candidates
        ADD COLUMN staged_decision TEXT CHECK(staged_decision IN (
            'publish_incoming', 'keep_current'
        ))
    """,
    """
    ALTER TABLE knowledge_reconciliation_candidates
        ADD COLUMN resolution_status TEXT CHECK(resolution_status IN ('published', 'kept'))
    """,
    """
    ALTER TABLE knowledge_reconciliation_candidates
        ADD COLUMN resolved_at TEXT
    """,
    """
    CREATE TABLE knowledge_reconciliation_resolution_records (
        resolution_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE
            REFERENCES knowledge_reconciliation_candidates(candidate_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        normalized_title TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('publish_incoming', 'keep_current')),
        published_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        resolved_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_staged_idx
        ON knowledge_reconciliation_candidates(status, resolution_status, staged_decision)
    """,
    """
    CREATE INDEX knowledge_reconciliation_resolution_records_document_idx
        ON knowledge_reconciliation_resolution_records(document_id, resolved_at DESC)
    """,
)


# Graph diagnostics are code-owned operational metadata. Semantic identities and
# relation labels live only in the current-epoch semantic authority tables.
KNOWLEDGE_GRAPH_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_graph_diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL CHECK(phase IN ('extraction', 'query')),
        error_code TEXT NOT NULL,
        document_id TEXT REFERENCES source_documents(document_id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX knowledge_graph_diagnostics_recent_idx
        ON knowledge_graph_diagnostics(created_at DESC)
    """,
)


# Every retrieval-affecting write advances one cheap revision.  The graph
# approval check can therefore stay O(1) on the normal answer path, while the
# evaluation/promotion path retains its full corpus fingerprint.
_RETRIEVAL_CORPUS_TABLES = (
    "source_documents",
    "document_ir_blocks",
    "evidence_refs",
    "evidence_occurrences",
    "knowledge_generation_state",
    "knowledge_generation_items",
)


def _retrieval_corpus_revision_triggers() -> tuple[str, ...]:
    return tuple(
        f"""
        CREATE TRIGGER desktop_retrieval_corpus_{table_name}_{event}
        AFTER {event.upper()} ON {table_name}
        BEGIN
            UPDATE desktop_retrieval_corpus_state
            SET revision = revision + 1
            WHERE singleton = 1;
        END
        """
        for table_name in _RETRIEVAL_CORPUS_TABLES
        for event in ("insert", "update", "delete")
    )


# Only the evidence-anchored local graph has a switch in this release.  The
# constrained key set makes Community, Global GraphRAG, and DRIFT impossible to
# turn on until they have their own approved schema and evaluation gate.
GRAPH_FEATURE_FLAG_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE desktop_retrieval_corpus_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        revision INTEGER NOT NULL CHECK(revision >= 0)
    )
    """,
    """
    INSERT INTO desktop_retrieval_corpus_state (singleton, revision) VALUES (1, 1)
    """,
    """
    CREATE TABLE desktop_graph_feature_flags (
        feature_key TEXT PRIMARY KEY CHECK(feature_key IN ('local_graph')),
        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
        approved_suite_digest TEXT,
        approved_snapshot_digest TEXT,
        approved_snapshot_revision INTEGER CHECK(approved_snapshot_revision >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO desktop_graph_feature_flags (
        feature_key, enabled, approved_suite_digest, approved_snapshot_digest,
        approved_snapshot_revision, updated_at
    ) VALUES ('local_graph', 0, NULL, NULL, NULL, '1970-01-01T00:00:00+00:00')
    """,
    *_retrieval_corpus_revision_triggers(),
)
