"""Immutable source-scoped Knowledge Graph result provenance and current selection."""

KNOWLEDGE_GRAPH_RESULT_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_results (
        result_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('completed', 'completed_empty')),
        capability_identity TEXT,
        prompt_contract_digest TEXT,
        extraction_method TEXT NOT NULL CHECK(extraction_method IN (
            'model', 'deterministic', 'legacy'
        )),
        node_count INTEGER NOT NULL CHECK(node_count >= 0),
        edge_count INTEGER NOT NULL CHECK(edge_count >= 0),
        created_at TEXT NOT NULL,
        CHECK(
            (status = 'completed_empty' AND node_count = 0 AND edge_count = 0)
            OR (status = 'completed' AND node_count > 0)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_graph_results_document_idx
        ON knowledge_graph_results(document_id, created_at DESC, result_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_result_nodes (
        result_id TEXT NOT NULL REFERENCES knowledge_graph_results(result_id) ON DELETE CASCADE,
        node_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(node_id) ON DELETE RESTRICT,
        PRIMARY KEY(result_id, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_result_edges (
        result_id TEXT NOT NULL REFERENCES knowledge_graph_results(result_id) ON DELETE CASCADE,
        edge_id TEXT NOT NULL REFERENCES knowledge_graph_edges(edge_id) ON DELETE RESTRICT,
        PRIMARY KEY(result_id, edge_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_current (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        result_id TEXT NOT NULL UNIQUE
            REFERENCES knowledge_graph_results(result_id) ON DELETE RESTRICT
    )
    """,
    """
    INSERT OR IGNORE INTO knowledge_graph_results (
        result_id, document_id, status, capability_identity, prompt_contract_digest,
        extraction_method, node_count, edge_count, created_at
    )
    SELECT 'legacy:' || documents.document_id, documents.document_id,
        CASE WHEN COUNT(DISTINCT nodes.node_id) = 0 THEN 'completed_empty' ELSE 'completed' END,
        NULL, NULL, 'legacy', COUNT(DISTINCT nodes.node_id), COUNT(DISTINCT edges.edge_id),
        COALESCE(MAX(nodes.created_at), MAX(edges.created_at), tasks.completed_at,
            tasks.updated_at, documents.created_at)
    FROM source_documents AS documents
    LEFT JOIN evidence_occurrences AS occurrences
        ON occurrences.document_id = documents.document_id
    LEFT JOIN knowledge_graph_nodes AS nodes ON nodes.evidence_id = occurrences.evidence_id
    LEFT JOIN knowledge_graph_edges AS edges ON edges.evidence_id = occurrences.evidence_id
    LEFT JOIN knowledge_graph_extraction_tasks AS tasks
        ON tasks.document_id = documents.document_id AND tasks.status = 'completed'
    GROUP BY documents.document_id
    HAVING COUNT(DISTINCT nodes.node_id) > 0 OR MAX(tasks.status) = 'completed'
    """,
    """
    INSERT OR IGNORE INTO knowledge_graph_current (document_id, result_id)
    SELECT document_id, result_id FROM knowledge_graph_results
    """,
    """
    INSERT OR IGNORE INTO knowledge_graph_result_nodes (result_id, node_id)
    SELECT 'legacy:' || occurrences.document_id, nodes.node_id
    FROM evidence_occurrences AS occurrences
    JOIN knowledge_graph_nodes AS nodes ON nodes.evidence_id = occurrences.evidence_id
    JOIN knowledge_graph_results AS results
        ON results.result_id = 'legacy:' || occurrences.document_id
    """,
    """
    INSERT OR IGNORE INTO knowledge_graph_result_edges (result_id, edge_id)
    SELECT 'legacy:' || occurrences.document_id, edges.edge_id
    FROM evidence_occurrences AS occurrences
    JOIN knowledge_graph_edges AS edges ON edges.evidence_id = occurrences.evidence_id
    JOIN knowledge_graph_results AS results
        ON results.result_id = 'legacy:' || occurrences.document_id
    """,
    """
    CREATE VIEW IF NOT EXISTS current_knowledge_graph_nodes AS
    SELECT DISTINCT nodes.*
    FROM knowledge_graph_current AS current
    JOIN knowledge_graph_result_nodes AS memberships ON memberships.result_id = current.result_id
    JOIN knowledge_graph_nodes AS nodes ON nodes.node_id = memberships.node_id
    UNION ALL
    SELECT nodes.* FROM knowledge_graph_nodes AS nodes
    WHERE NOT EXISTS (
        SELECT 1 FROM knowledge_graph_result_nodes AS memberships
        WHERE memberships.node_id = nodes.node_id
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS current_knowledge_graph_edges AS
    SELECT DISTINCT edges.*
    FROM knowledge_graph_current AS current
    JOIN knowledge_graph_result_edges AS memberships ON memberships.result_id = current.result_id
    JOIN knowledge_graph_edges AS edges ON edges.edge_id = memberships.edge_id
    UNION ALL
    SELECT edges.* FROM knowledge_graph_edges AS edges
    WHERE NOT EXISTS (
        SELECT 1 FROM knowledge_graph_result_edges AS memberships
        WHERE memberships.edge_id = edges.edge_id
    )
    """,
)


KNOWLEDGE_GRAPH_CURRENT_REVISION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_insert
    AFTER INSERT ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_update
    AFTER UPDATE ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_delete
    AFTER DELETE ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    UPDATE desktop_retrieval_corpus_state
    SET revision = revision + 1 WHERE singleton = 1
    """,
)
