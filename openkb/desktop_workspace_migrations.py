"""Additive SQLite statements owned by Desktop Runtime schema migrations."""

from __future__ import annotations

MODEL_CALL_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE model_calls (
        call_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        operation TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'running', 'retry_wait', 'completed', 'failed'
        )),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
        timeout_seconds REAL NOT NULL CHECK(timeout_seconds >= 0),
        next_timeout_seconds REAL,
        remaining_seconds REAL NOT NULL CHECK(remaining_seconds >= 0),
        error_code TEXT,
        reason TEXT,
        suggested_action TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE model_attempts (
        call_id TEXT NOT NULL REFERENCES model_calls(call_id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL CHECK(attempt >= 1),
        status TEXT NOT NULL CHECK(status IN (
            'running', 'retry_wait', 'completed', 'failed'
        )),
        timeout_seconds REAL NOT NULL CHECK(timeout_seconds >= 0),
        remaining_seconds REAL NOT NULL CHECK(remaining_seconds >= 0),
        error_code TEXT,
        reason TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY(call_id, attempt)
    )
    """,
    """
    CREATE TABLE quarantined_documents (
        job_id TEXT PRIMARY KEY REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id),
        stage TEXT NOT NULL,
        error_code TEXT NOT NULL,
        reason TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
        created_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO stage_runs (
        stage_run_id, job_id, stage, status, progress, error_code, started_at, completed_at
    )
    SELECT
        lower(hex(randomblob(16))), import_jobs.job_id, 'model_analysis',
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled') THEN 'skipped'
            ELSE 'pending'
        END,
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled') THEN 100
            ELSE 0
        END,
        NULL,
        NULL,
        CASE
            WHEN COALESCE(import_job_runtime.status, import_jobs.status)
                IN ('completed', 'failed', 'cancelled')
            THEN COALESCE(import_jobs.completed_at, import_jobs.created_at)
            ELSE NULL
        END
    FROM import_jobs
    LEFT JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_runs existing
        WHERE existing.job_id = import_jobs.job_id AND existing.stage = 'model_analysis'
    )
    """,
    """
    INSERT INTO stage_run_runtime (
        stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
    )
    SELECT
        stage_runs.stage_run_id, stage_runs.job_id, stage_runs.status,
        NULL, stage_runs.error_code,
        COALESCE(stage_runs.completed_at, import_job_runtime.updated_at, import_jobs.created_at)
    FROM stage_runs
    JOIN import_jobs ON import_jobs.job_id = stage_runs.job_id
    LEFT JOIN import_job_runtime ON import_job_runtime.job_id = stage_runs.job_id
    WHERE stage_runs.stage = 'model_analysis'
        AND NOT EXISTS (
            SELECT 1 FROM stage_run_runtime existing
            WHERE existing.stage_run_id = stage_runs.stage_run_id
        )
    """,
    """
    CREATE INDEX model_calls_job_idx ON model_calls(job_id, created_at)
    """,
    """
    CREATE INDEX model_attempts_call_idx ON model_attempts(call_id, attempt)
    """,
)


RECOVERY_RUN_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE recovery_runs (
        recovery_run_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        stage_run_id TEXT NOT NULL REFERENCES stage_runs(stage_run_id) ON DELETE CASCADE,
        model_override TEXT,
        initial_timeout_seconds REAL,
        status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
        started_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX recovery_runs_job_idx ON recovery_runs(job_id, started_at DESC)
    """,
)


RAW_ASSET_INTEGRITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE raw_asset_integrity (
        asset_sha256 TEXT PRIMARY KEY REFERENCES raw_assets(asset_sha256) ON DELETE CASCADE,
        lifecycle_status TEXT NOT NULL DEFAULT 'available'
        CHECK(lifecycle_status IN ('available', 'quarantined')),
        integrity_error_code TEXT,
        verified_at TEXT
    )
    """,
    """
    INSERT INTO raw_asset_integrity (
        asset_sha256, lifecycle_status, integrity_error_code, verified_at
    )
    SELECT asset_sha256, 'available', NULL, NULL FROM raw_assets
    """,
    """
    CREATE TRIGGER raw_assets_create_integrity
    AFTER INSERT ON raw_assets
    BEGIN
        INSERT INTO raw_asset_integrity (
            asset_sha256, lifecycle_status, integrity_error_code, verified_at
        )
        VALUES (NEW.asset_sha256, 'available', NULL, NULL);
    END
    """,
    """
    CREATE INDEX raw_asset_integrity_lifecycle_idx
        ON raw_asset_integrity(lifecycle_status, verified_at DESC)
    """,
)


SOURCE_IMAGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE source_images (
        source_image_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id)
            ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        image_sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
        media_type TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        display_name TEXT NOT NULL,
        alt_text TEXT,
        locator_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(document_id, ordinal)
    )
    """,
    """
    CREATE INDEX source_images_document_idx
        ON source_images(document_id, ordinal)
    """,
)
