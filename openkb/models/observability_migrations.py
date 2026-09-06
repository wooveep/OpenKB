"""Schema additions for explicit Model Call lifecycle state."""

MODEL_LIFECYCLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("model_calls", "lifecycle_status", "TEXT"),
    ("model_calls", "elapsed_seconds", "REAL NOT NULL DEFAULT 0"),
    ("model_calls", "retry_after_seconds", "REAL"),
    ("model_attempts", "lifecycle_status", "TEXT"),
    ("model_attempts", "elapsed_seconds", "REAL NOT NULL DEFAULT 0"),
    ("model_attempts", "retry_after_seconds", "REAL"),
)

MODEL_LIFECYCLE_MIGRATION_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
    for table_name, column_name, definition in MODEL_LIFECYCLE_COLUMNS
)
