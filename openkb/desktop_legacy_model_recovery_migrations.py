"""Audit records for explicit recovery from the retired model deadline policy."""

LEGACY_MODEL_RECOVERY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE legacy_model_recovery_audit (
        recovery_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES import_jobs(job_id) ON DELETE CASCADE,
        recovery_choice TEXT NOT NULL
            CHECK(recovery_choice IN ('continue_compatible', 'restart_current_plan')),
        compatible INTEGER NOT NULL CHECK(compatible IN (0, 1)),
        previous_prompt_digest TEXT,
        provider TEXT,
        model TEXT,
        continue_remaining_calls INTEGER NOT NULL,
        continue_input_tokens INTEGER NOT NULL,
        restart_remaining_calls INTEGER NOT NULL,
        restart_input_tokens INTEGER NOT NULL,
        resulting_plan_identity TEXT,
        selected_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX legacy_model_recovery_job_idx
    ON legacy_model_recovery_audit(job_id, selected_at DESC)
    """,
)
