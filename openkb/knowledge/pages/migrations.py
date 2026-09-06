"""Schema additions for user Knowledge Page drafts and editor state."""

KNOWLEDGE_PAGE_DRAFT_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_page_working_drafts (
        page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(kind, normalized_title)
    )
    """,
    """
    CREATE TABLE knowledge_page_ui_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        last_page_id TEXT
    )
    """,
    """
    INSERT INTO knowledge_page_ui_state (singleton, last_page_id)
    VALUES (1, NULL)
    """,
)
