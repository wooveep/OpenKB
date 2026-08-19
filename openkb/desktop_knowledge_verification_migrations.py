"""Schema additions for revision-bound human Knowledge Verification."""

KNOWLEDGE_VERIFICATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_page_verifications (
        verification_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES knowledge_page_revisions(revision_id)
            ON DELETE CASCADE,
        verification_kind TEXT NOT NULL CHECK(verification_kind = 'human_reviewed'),
        actor TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        invalidated_at TEXT,
        invalidation_reason TEXT,
        CHECK(
            (invalidated_at IS NULL AND invalidation_reason IS NULL)
            OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX knowledge_page_verifications_active_revision_idx
        ON knowledge_page_verifications(revision_id)
        WHERE invalidated_at IS NULL
    """,
    """
    CREATE INDEX knowledge_page_verifications_revision_history_idx
        ON knowledge_page_verifications(revision_id, verified_at)
    """,
)
