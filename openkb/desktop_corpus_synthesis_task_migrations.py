"""Durable claims and diagnostics for generation-owned corpus synthesis work."""

CORPUS_SYNTHESIS_TASK_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_corpus_synthesis_tasks (
        generation_id INTEGER PRIMARY KEY
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN (
            'running', 'completed', 'failed', 'cancelled', 'superseded'
        )),
        phase TEXT NOT NULL CHECK(phase IN (
            'dossier_planning', 'qualification', 'completed', 'failed'
        )),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        retry_scope TEXT,
        execution_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        error_code TEXT,
        error_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        CHECK(
            (status = 'running' AND execution_token IS NOT NULL)
            OR (status != 'running' AND execution_token IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_corpus_synthesis_tasks_status_idx
        ON knowledge_corpus_synthesis_tasks(status, updated_at, generation_id)
    """,
)
