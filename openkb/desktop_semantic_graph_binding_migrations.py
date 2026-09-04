"""Pin document semantic assertions to the exact immutable Graph result."""

SEMANTIC_GRAPH_RESULT_BINDING_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_document_relationships
        ADD COLUMN graph_result_id TEXT
            REFERENCES knowledge_graph_results(result_id) ON DELETE RESTRICT
    """,
    """
    UPDATE knowledge_document_relationships
    SET graph_result_id = (
        SELECT current.result_id
        FROM knowledge_graph_current AS current
        JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
        WHERE current.document_id = knowledge_document_relationships.document_id
          AND results.candidate_generation_id IS
              knowledge_document_relationships.candidate_generation_id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_document_relationships_graph_result_idx
        ON knowledge_document_relationships(graph_result_id)
    """,
)
