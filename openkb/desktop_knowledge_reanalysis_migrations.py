"""SQLite schema for explicit Knowledge Reanalysis work."""

KNOWLEDGE_REANALYSIS_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_reanalysis_runs (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL CHECK(mode IN ('single', 'bulk')),
        status TEXT NOT NULL CHECK(status IN (
            'pending', 'running', 'completed', 'partial_failure', 'failed'
        )),
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE knowledge_reanalysis_jobs (
        job_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES knowledge_reanalysis_runs(run_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        phase TEXT NOT NULL CHECK(phase IN (
            'pending', 'batches', 'merge', 'reconciliation', 'completed', 'failed'
        )),
        progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        expected_prompt_digest TEXT NOT NULL,
        checkpoint_json TEXT,
        error_code TEXT,
        reason TEXT,
        current_operation TEXT,
        attempt_count INTEGER,
        timeout_seconds REAL,
        remaining_seconds REAL,
        next_timeout_seconds REAL,
        execution_token TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        UNIQUE(run_id, document_id)
    )
    """,
    """
    CREATE INDEX knowledge_reanalysis_jobs_status_idx
        ON knowledge_reanalysis_jobs(status, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_reanalysis_jobs_document_idx
        ON knowledge_reanalysis_jobs(document_id, completed_at DESC, created_at DESC)
    """,
    """
    CREATE TABLE knowledge_reanalysis_batches (
        batch_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES knowledge_reanalysis_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL,
        batch_ordinal INTEGER NOT NULL CHECK(batch_ordinal >= 0),
        section_paths_json TEXT NOT NULL,
        evidence_ids_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        checkpoint_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_id, batch_ordinal)
    )
    """,
    """
    CREATE INDEX knowledge_reanalysis_batches_job_status_idx
        ON knowledge_reanalysis_batches(job_id, status, batch_ordinal)
    """,
    """
    CREATE TABLE knowledge_reanalysis_merges (
        job_id TEXT PRIMARY KEY REFERENCES knowledge_reanalysis_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        checkpoint_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
