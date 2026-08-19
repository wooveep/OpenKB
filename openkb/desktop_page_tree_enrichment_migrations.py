"""SQLite schema for optional PageTree summary enrichment generations."""

PAGE_TREE_ENRICHMENT_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE document_page_tree_enrichment_generations (
        enrichment_generation_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        base_generation_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('current', 'superseded')),
        created_at TEXT NOT NULL,
        UNIQUE(document_id, enrichment_generation_id),
        UNIQUE(enrichment_generation_id, base_generation_id),
        FOREIGN KEY(document_id, base_generation_id)
            REFERENCES document_page_tree_generations(document_id, generation_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE document_page_tree_enrichment_summaries (
        enrichment_generation_id TEXT NOT NULL,
        base_generation_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 600),
        PRIMARY KEY(enrichment_generation_id, node_id),
        FOREIGN KEY(enrichment_generation_id, base_generation_id)
            REFERENCES document_page_tree_enrichment_generations(
                enrichment_generation_id, base_generation_id
            ) ON DELETE CASCADE,
        FOREIGN KEY(base_generation_id, node_id)
            REFERENCES document_page_tree_nodes(generation_id, node_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE document_page_tree_enrichment_current (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        enrichment_generation_id TEXT NOT NULL UNIQUE,
        base_generation_id TEXT NOT NULL,
        activated_at TEXT NOT NULL,
        FOREIGN KEY(document_id, enrichment_generation_id)
            REFERENCES document_page_tree_enrichment_generations(
                document_id, enrichment_generation_id
            ) ON DELETE CASCADE,
        FOREIGN KEY(enrichment_generation_id, base_generation_id)
            REFERENCES document_page_tree_enrichment_generations(
                enrichment_generation_id, base_generation_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE document_page_tree_enrichment_tasks (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        base_generation_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'failed', 'completed')),
        reason TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_digest TEXT NOT NULL,
        execution_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        model_attempt INTEGER NOT NULL DEFAULT 0 CHECK(model_attempt >= 0),
        call_id TEXT,
        timeout_seconds REAL,
        remaining_seconds REAL,
        error_code TEXT,
        error_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY(document_id, base_generation_id)
            REFERENCES document_page_tree_generations(document_id, generation_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX document_page_tree_enrichment_generations_document_idx
        ON document_page_tree_enrichment_generations(document_id, created_at DESC)
    """,
    """
    CREATE INDEX document_page_tree_enrichment_tasks_status_idx
        ON document_page_tree_enrichment_tasks(status, updated_at)
    """,
)
