"""SQLite schema for durable optional Knowledge Graph extraction tasks."""

KNOWLEDGE_GRAPH_TASK_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_graph_extraction_tasks (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'failed', 'completed')),
        reason TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_digest TEXT NOT NULL,
        execution_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        model_attempt INTEGER NOT NULL DEFAULT 0 CHECK(model_attempt >= 0),
        call_id TEXT,
        error_code TEXT,
        error_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX knowledge_graph_extraction_tasks_status_idx
        ON knowledge_graph_extraction_tasks(status, updated_at)
    """,
)


KNOWLEDGE_GRAPH_RETRY_SCOPE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_graph_extraction_tasks
    ADD COLUMN retry_scope TEXT
    """,
)
