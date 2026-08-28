"""SQLite schema for operation-local model contract readiness."""

MODEL_OPERATION_STATE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS model_operation_contract_states (
        operation TEXT NOT NULL,
        capability_identity TEXT NOT NULL,
        prompt_contract_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('unverified', 'ready', 'suspended')),
        failure_code TEXT,
        reason TEXT,
        failure_stage TEXT,
        failure_signature TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(operation, capability_identity, prompt_contract_digest)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS model_operation_contract_states_status_idx
        ON model_operation_contract_states(status, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS model_capability_compatibility_audit (
        legacy_profile_identity TEXT PRIMARY KEY,
        shared_profile_identity TEXT,
        decision TEXT NOT NULL CHECK(decision IN (
            'carried_verified', 'restored_graph_local', 'left_unverified',
            'invalid_legacy_profile'
        )),
        evidence_json TEXT NOT NULL,
        migrated_at TEXT NOT NULL
    )
    """,
)

MODEL_OPERATION_RETRY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS model_operation_retry_permits (
        operation TEXT NOT NULL,
        capability_identity TEXT NOT NULL,
        prompt_contract_digest TEXT NOT NULL,
        retry_scope TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(
            operation, capability_identity, prompt_contract_digest, retry_scope
        ),
        FOREIGN KEY(operation, capability_identity, prompt_contract_digest)
            REFERENCES model_operation_contract_states(
                operation, capability_identity, prompt_contract_digest
            ) ON DELETE CASCADE
    )
    """,
)

MODEL_OPERATION_AUDIT_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS model_operation_contract_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL,
        capability_identity TEXT NOT NULL,
        prompt_contract_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('unverified', 'ready', 'suspended')),
        failure_code TEXT,
        failure_stage TEXT,
        failure_signature TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS model_operation_contract_events_contract_idx
        ON model_operation_contract_events(
            operation, capability_identity, prompt_contract_digest, event_id
        )
    """,
)
