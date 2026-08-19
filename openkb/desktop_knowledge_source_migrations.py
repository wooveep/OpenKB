"""Schema additions for revision-bound Knowledge Source Maps."""

KNOWLEDGE_SOURCE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_page_working_sources (
        page_id TEXT NOT NULL REFERENCES knowledge_page_working_drafts(page_id)
            ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(page_id, source_id)
    )
    """,
    """
    CREATE INDEX knowledge_page_working_sources_evidence_idx
        ON knowledge_page_working_sources(evidence_id, page_id)
    """,
    """
    CREATE TABLE knowledge_page_revision_sources (
        revision_id TEXT NOT NULL REFERENCES knowledge_page_revisions(revision_id)
            ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(revision_id, source_id)
    )
    """,
    """
    CREATE INDEX knowledge_page_revision_sources_evidence_idx
        ON knowledge_page_revision_sources(evidence_id, revision_id)
    """,
)
