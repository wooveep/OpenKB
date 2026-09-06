"""Immutable Knowledge Analysis Plan and hierarchical merge checkpoints."""

KNOWLEDGE_ANALYSIS_PLAN_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_analysis_plans (
        job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        document_ir_digest TEXT NOT NULL,
        analysis_model TEXT NOT NULL,
        prompt_contract_digest TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE knowledge_reanalysis_plans (
        job_id TEXT PRIMARY KEY REFERENCES knowledge_reanalysis_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL,
        document_ir_digest TEXT NOT NULL,
        analysis_model TEXT NOT NULL,
        prompt_contract_digest TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE knowledge_analysis_merge_nodes (
        node_id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        level INTEGER NOT NULL CHECK(level >= 0),
        node_ordinal INTEGER NOT NULL CHECK(node_ordinal >= 0),
        child_ids_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        checkpoint_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(job_id, node_id),
        UNIQUE(job_id, level, node_ordinal)
    )
    """,
    """
    CREATE TABLE knowledge_reanalysis_merge_nodes (
        node_id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES knowledge_reanalysis_jobs(job_id) ON DELETE CASCADE,
        level INTEGER NOT NULL CHECK(level >= 0),
        node_ordinal INTEGER NOT NULL CHECK(node_ordinal >= 0),
        child_ids_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        checkpoint_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(job_id, node_id),
        UNIQUE(job_id, level, node_ordinal)
    )
    """,
)
