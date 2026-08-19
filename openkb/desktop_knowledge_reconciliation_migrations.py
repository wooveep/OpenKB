"""Schema upgrade for draft-aware three-way Knowledge Reconciliation."""

THREE_WAY_KNOWLEDGE_RECONCILIATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_reconciliation_resolution_records
        RENAME TO knowledge_reconciliation_resolution_records_v23
    """,
    """
    ALTER TABLE knowledge_reconciliation_candidates
        RENAME TO knowledge_reconciliation_candidates_v23
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
        baseline_kind TEXT CHECK(baseline_kind IN (
            'published_generation', 'user_revision', 'unpublished_page'
        )),
        baseline_id TEXT,
        baseline_title TEXT,
        baseline_content_markdown TEXT,
        observed_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        reconciliation_mode TEXT NOT NULL DEFAULT 'two_way'
            CHECK(reconciliation_mode IN ('two_way', 'three_way')),
        target_page_id TEXT,
        working_draft_title TEXT,
        working_draft_content_markdown TEXT,
        working_draft_content_sha256 TEXT,
        working_draft_updated_at TEXT,
        staged_decision TEXT CHECK(staged_decision IN (
            'publish_incoming', 'keep_current', 'keep_draft', 'apply_incoming',
            'replace_draft', 'manual_merge'
        )),
        staged_content_markdown TEXT,
        resolution_status TEXT CHECK(resolution_status IN (
            'published', 'kept', 'draft_updated'
        )),
        resolved_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO knowledge_reconciliation_candidates (
        candidate_id, document_id, source_block_id, kind, title, normalized_title,
        content_markdown, content_sha256, classification, status,
        baseline_kind, baseline_id, baseline_title, baseline_content_markdown,
        observed_generation_id, reconciliation_mode, target_page_id,
        working_draft_title, working_draft_content_markdown,
        working_draft_content_sha256, working_draft_updated_at,
        staged_decision, staged_content_markdown, resolution_status, resolved_at, created_at
    )
    WITH migrated_candidates AS (
        SELECT candidates.*, COALESCE(
            revisions.page_id,
            (
                SELECT matching_drafts.page_id
                FROM knowledge_page_working_drafts AS matching_drafts
                WHERE matching_drafts.kind = candidates.kind
                    AND matching_drafts.normalized_title = candidates.normalized_title
                ORDER BY matching_drafts.updated_at DESC, matching_drafts.page_id
                LIMIT 1
            ),
            (
                SELECT matching_pages.page_id
                FROM knowledge_pages AS matching_pages
                WHERE matching_pages.kind = candidates.kind
                    AND matching_pages.normalized_title = candidates.normalized_title
                ORDER BY matching_pages.page_id
                LIMIT 1
            )
        ) AS resolved_page_id
        FROM knowledge_reconciliation_candidates_v23 AS candidates
        LEFT JOIN knowledge_page_revisions AS revisions
            ON candidates.baseline_kind = 'user_revision'
            AND revisions.revision_id = candidates.baseline_id
    )
    SELECT
        candidates.candidate_id, candidates.document_id, candidates.source_block_id,
        candidates.kind, candidates.title, candidates.normalized_title,
        candidates.content_markdown, candidates.content_sha256,
        candidates.classification, candidates.status, candidates.baseline_kind,
        candidates.baseline_id, candidates.baseline_title,
        candidates.baseline_content_markdown, candidates.observed_generation_id,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
                AND drafts.page_id IS NOT NULL
            THEN 'three_way'
            ELSE 'two_way'
        END,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
            THEN candidates.resolved_page_id
        END,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
            THEN drafts.title
        END,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
            THEN drafts.content_markdown
        END,
        NULL,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
            THEN drafts.updated_at
        END,
        CASE
            WHEN candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
                AND drafts.page_id IS NULL
            THEN candidates.staged_decision
        END,
        NULL, candidates.resolution_status,
        candidates.resolved_at, candidates.created_at
    FROM migrated_candidates AS candidates
    LEFT JOIN knowledge_page_working_drafts AS drafts
        ON drafts.page_id = candidates.resolved_page_id
    """,
    """
    CREATE TABLE knowledge_reconciliation_resolution_records (
        resolution_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE
            REFERENCES knowledge_reconciliation_candidates(candidate_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        normalized_title TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN (
            'publish_incoming', 'keep_current', 'keep_draft', 'apply_incoming',
            'replace_draft', 'manual_merge'
        )),
        target_page_id TEXT,
        published_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        result_content_sha256 TEXT,
        resolved_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO knowledge_reconciliation_resolution_records (
        resolution_id, candidate_id, document_id, kind, normalized_title,
        decision, target_page_id, published_generation_id,
        result_content_sha256, resolved_at
    )
    SELECT resolution_id, candidate_id, document_id, kind, normalized_title,
        decision, NULL, published_generation_id, NULL, resolved_at
    FROM knowledge_reconciliation_resolution_records_v23
    """,
    """
    DROP TABLE knowledge_reconciliation_resolution_records_v23
    """,
    """
    DROP TABLE knowledge_reconciliation_candidates_v23
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_review_idx
        ON knowledge_reconciliation_candidates(status, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_document_idx
        ON knowledge_reconciliation_candidates(document_id, created_at DESC)
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
