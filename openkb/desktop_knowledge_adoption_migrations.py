"""Schema for immutable Generated-to-User Knowledge Adoption lineage."""

KNOWLEDGE_ADOPTION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_origin_references (
        generation_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        page_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, item_key),
        FOREIGN KEY(generation_id, item_key)
            REFERENCES knowledge_generation_items(generation_id, item_key)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_origin_references_page_idx
        ON knowledge_origin_references(page_id, generation_id, item_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_adoption_requests (
        request_id TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'adopted', 'already_adopted', 'reconciliation_required', 'choice_required'
        )),
        page_id TEXT,
        candidates_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        FOREIGN KEY(generation_id, item_key)
            REFERENCES knowledge_generation_items(generation_id, item_key)
            ON DELETE RESTRICT
    )
    """,
)

KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "knowledge_adoption_requests",
        "decision",
        "TEXT CHECK(decision IN ('create_new', 'use_existing'))",
    ),
    ("knowledge_adoption_requests", "candidate_page_id", "TEXT"),
)

KNOWLEDGE_ADOPTION_REQUEST_INPUT_MIGRATION_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
    for table_name, column_name, definition in KNOWLEDGE_ADOPTION_REQUEST_INPUT_COLUMNS
)
