"""Schema additions for explicit Knowledge provenance states."""

KNOWLEDGE_PROVENANCE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_page_revisions
    ADD COLUMN provenance_state TEXT NOT NULL DEFAULT 'legacy_unmapped'
        CHECK(provenance_state IN ('source_backed', 'structural', 'legacy_unmapped'))
    """,
    """
    UPDATE knowledge_page_revisions AS revisions
    SET provenance_state = 'source_backed'
    WHERE EXISTS (
        SELECT 1 FROM knowledge_page_revision_sources AS sources
        WHERE sources.revision_id = revisions.revision_id
    )
    """,
    """
    UPDATE knowledge_page_revisions
    SET provenance_state = 'structural'
    WHERE provenance_state = 'legacy_unmapped'
        AND created_at >= (
            SELECT applied_at FROM schema_migrations WHERE version = 20
        )
    """,
    """
    ALTER TABLE knowledge_generation_items
    ADD COLUMN provenance_state TEXT NOT NULL DEFAULT 'legacy_unmapped'
        CHECK(provenance_state IN ('source_backed', 'structural', 'legacy_unmapped'))
    """,
)
