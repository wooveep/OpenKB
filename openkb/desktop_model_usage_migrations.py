"""Sanitized provider/model usage, timing, and classification records."""

MODEL_USAGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE model_usage_records (
        call_id TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt > 0),
        attempt_id TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        model_role TEXT NOT NULL CHECK(model_role IN ('default', 'analysis', 'answer')),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        job_id TEXT,
        stage_run_id TEXT,
        batch_id TEXT,
        execution_lane TEXT NOT NULL DEFAULT 'background'
            CHECK(execution_lane IN ('background', 'interactive')),
        lifecycle_status TEXT NOT NULL,
        failure_code TEXT,
        attempt_started_elapsed REAL NOT NULL DEFAULT 0,
        connecting_started_elapsed REAL,
        last_event_elapsed REAL NOT NULL DEFAULT 0,
        queue_seconds REAL,
        connect_seconds REAL,
        first_output_seconds REAL,
        total_seconds REAL,
        input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
        total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 0),
        token_usage_source TEXT CHECK(token_usage_source IN ('provider_reported', 'estimated')),
        input_cost REAL,
        output_cost REAL,
        total_cost REAL,
        provider_request_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(call_id, attempt)
    )
    """,
    """
    CREATE INDEX model_usage_role_model_idx
    ON model_usage_records(model_role, model, lifecycle_status, updated_at)
    """,
    """
    CREATE INDEX model_usage_job_idx
    ON model_usage_records(job_id, stage_run_id, updated_at)
    """,
)
