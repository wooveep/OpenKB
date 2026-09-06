"""SQLite schema for resumable Knowledge Analysis batches."""

KNOWLEDGE_ANALYSIS_BATCH_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_analysis_batches (
        batch_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
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
    CREATE INDEX knowledge_analysis_batches_job_status_idx
        ON knowledge_analysis_batches(job_id, status, batch_ordinal)
    """,
    """
    CREATE TABLE knowledge_analysis_merges (
        job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        checkpoint_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
