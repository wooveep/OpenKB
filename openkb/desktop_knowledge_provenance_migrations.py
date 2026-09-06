"""Schema additions for explicit Knowledge provenance states."""

KNOWLEDGE_PROVENANCE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_page_revisions
    ADD COLUMN provenance_state TEXT NOT NULL DEFAULT 'structural'
        CHECK(provenance_state IN ('source_backed', 'structural'))
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
    ALTER TABLE knowledge_generation_items
    ADD COLUMN provenance_state TEXT NOT NULL DEFAULT 'structural'
        CHECK(provenance_state IN ('source_backed', 'structural'))
    """,
)
