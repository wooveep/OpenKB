"""Persist the benchmark report that qualifies each generated corpus snapshot."""

CORPUS_BENCHMARK_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("knowledge_generations", "qualification_report_json", "TEXT"),
)

CORPUS_BENCHMARK_MIGRATION_STATEMENTS: tuple[str, ...] = (
    *(
        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        for table, column, definition in CORPUS_BENCHMARK_COLUMNS
    ),
)
