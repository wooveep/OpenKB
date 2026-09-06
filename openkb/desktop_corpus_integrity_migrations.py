"""Persist the code-owned integrity report for each generated corpus snapshot."""

CORPUS_INTEGRITY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("knowledge_generations", "integrity_report_json", "TEXT"),
)

CORPUS_INTEGRITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    *(
        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        for table, column, definition in CORPUS_INTEGRITY_COLUMNS
    ),
)
