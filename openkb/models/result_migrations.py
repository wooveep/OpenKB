"""Nullable, content-free observations for provider model results."""

MODEL_RESULT_OBSERVATION_COLUMNS: tuple[tuple[str, str, str], ...] = tuple(
    (table, name, definition)
    for table in ("model_calls", "model_attempts", "model_usage_records")
    for name, definition in (
        ("finish_reason", "TEXT"),
        ("reasoning_observed", "INTEGER CHECK(reasoning_observed IN (0, 1))"),
        ("final_content_observed", "INTEGER CHECK(final_content_observed IN (0, 1))"),
        ("reasoning_chunk_count", "INTEGER CHECK(reasoning_chunk_count >= 0)"),
        ("final_chunk_count", "INTEGER CHECK(final_chunk_count >= 0)"),
        ("reasoning_character_count", "INTEGER CHECK(reasoning_character_count >= 0)"),
        ("final_character_count", "INTEGER CHECK(final_character_count >= 0)"),
    )
) + tuple(
    (table, name, definition)
    for table in ("model_calls", "model_attempts")
    for name, definition in (
        ("input_tokens", "INTEGER CHECK(input_tokens >= 0)"),
        ("output_tokens", "INTEGER CHECK(output_tokens >= 0)"),
        ("total_tokens", "INTEGER CHECK(total_tokens >= 0)"),
        ("provider_request_id", "TEXT"),
    )
)

MODEL_RESULT_OBSERVATION_MIGRATION_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
    for table, name, definition in MODEL_RESULT_OBSERVATION_COLUMNS
)
