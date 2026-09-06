"""Durable, content-free exact-profile capability-check outcomes."""

MODEL_CAPABILITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE model_capability_checks (
        profile_identity TEXT PRIMARY KEY,
        profile_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'unchecked', 'checking', 'verified', 'failed', 'cancelled'
        )),
        failure_code TEXT,
        reason TEXT,
        checked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX model_capability_checks_status_idx
    ON model_capability_checks(status, updated_at)
    """,
)
