"""Schema additions for user-controlled Knowledge Page lifecycle."""

KNOWLEDGE_LIFECYCLE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_pages
    ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'stable'
        CHECK(lifecycle_state IN ('stable', 'deprecated'))
    """,
    """
    ALTER TABLE knowledge_pages
    ADD COLUMN stale_after TEXT
    """,
    # Deliberately no page FK: confirmed deletion removes page content but retains
    # the non-content operation record required by the lifecycle log.
    """
    CREATE TABLE knowledge_page_lifecycle_events (
        event_id TEXT PRIMARY KEY,
        page_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'stale_after_changed', 'deprecated', 'restored', 'permanently_deleted'
        )),
        previous_lifecycle_state TEXT CHECK(previous_lifecycle_state IN (
            'draft', 'stable', 'deprecated'
        )),
        new_lifecycle_state TEXT CHECK(new_lifecycle_state IN (
            'stable', 'deprecated'
        )),
        previous_stale_after TEXT,
        new_stale_after TEXT,
        actor TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX knowledge_page_lifecycle_events_page_idx
        ON knowledge_page_lifecycle_events(page_id, occurred_at)
    """,
)
