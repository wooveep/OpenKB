"""Schema for reviewable Knowledge Analysis claims without an Evidence binding."""

MISSING_SOURCE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_missing_source_candidates (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        entity_subtype TEXT,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        tags_json TEXT NOT NULL DEFAULT '[]',
        claim_text TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN (
            'source_not_provided', 'source_reference_unresolved'
        )),
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        analysis_provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(document_id, kind, normalized_title, claim_text)
    )
    """,
    """
    CREATE INDEX knowledge_missing_source_candidates_created_idx
        ON knowledge_missing_source_candidates(created_at, candidate_id)
    """,
    """
    CREATE TABLE knowledge_missing_source_resolution_records (
        resolution_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        decision TEXT NOT NULL CHECK(decision IN ('bound', 'dismissed')),
        evidence_id TEXT REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        outcome TEXT NOT NULL CHECK(outcome IN (
            'working_draft', 'generated', 'review_required', 'deduplicated', 'dismissed'
        )),
        resolved_at TEXT NOT NULL,
        CHECK(
            (decision = 'bound' AND evidence_id IS NOT NULL AND outcome != 'dismissed')
            OR (decision = 'dismissed' AND evidence_id IS NULL AND outcome = 'dismissed')
        )
    )
    """,
    """
    CREATE INDEX knowledge_missing_source_resolutions_document_idx
        ON knowledge_missing_source_resolution_records(document_id, resolved_at)
    """,
)
