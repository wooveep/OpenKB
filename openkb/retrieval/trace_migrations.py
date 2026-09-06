"""Answer-owned Retrieval Trace storage without derived-generation foreign keys."""

RETRIEVAL_TRACE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE grounded_answer_retrieval_traces (
        answer_id TEXT PRIMARY KEY
            REFERENCES grounded_answers(answer_id) ON DELETE CASCADE,
        trace_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE conversation_answer_retrieval_traces (
        answer_version_id TEXT PRIMARY KEY
            REFERENCES conversation_answer_versions(answer_version_id) ON DELETE CASCADE,
        trace_json TEXT NOT NULL
    )
    """,
)
