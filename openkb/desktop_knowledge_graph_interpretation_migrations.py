"""Evidence-safe graph interpretation metadata and immutable attempt history."""

KNOWLEDGE_GRAPH_INTERPRETATION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_graph_nodes
    ADD COLUMN support_start INTEGER CHECK(support_start IS NULL OR support_start >= 0)
    """,
    """
    ALTER TABLE knowledge_graph_nodes
    ADD COLUMN support_end INTEGER CHECK(support_end IS NULL OR support_end > 0)
    """,
    """
    ALTER TABLE knowledge_graph_nodes
    ADD COLUMN verification_state TEXT NOT NULL DEFAULT 'legacy_evidence_bound'
        CHECK(verification_state IN ('source_anchored', 'ambiguous', 'legacy_evidence_bound'))
    """,
    """
    ALTER TABLE knowledge_graph_edges ADD COLUMN relation_label TEXT
    """,
    """
    ALTER TABLE knowledge_graph_edges
    ADD COLUMN support_start INTEGER CHECK(support_start IS NULL OR support_start >= 0)
    """,
    """
    ALTER TABLE knowledge_graph_edges
    ADD COLUMN support_end INTEGER CHECK(support_end IS NULL OR support_end > 0)
    """,
    """
    ALTER TABLE knowledge_graph_edges
    ADD COLUMN verification_state TEXT NOT NULL DEFAULT 'legacy_evidence_bound'
        CHECK(verification_state IN ('source_anchored', 'ambiguous', 'legacy_evidence_bound'))
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN quality TEXT NOT NULL DEFAULT 'full' CHECK(quality IN ('full', 'degraded'))
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN retained_count INTEGER NOT NULL DEFAULT 0 CHECK(retained_count >= 0)
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN weakened_count INTEGER NOT NULL DEFAULT 0 CHECK(weakened_count >= 0)
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN rejected_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_count >= 0)
    """,
    """
    ALTER TABLE knowledge_graph_results ADD COLUMN document_version TEXT
    """,
    """
    ALTER TABLE knowledge_graph_results ADD COLUMN evidence_snapshot_digest TEXT
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN canonical_schema_version TEXT NOT NULL DEFAULT 'legacy'
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN normalizer_version TEXT NOT NULL DEFAULT 'legacy'
    """,
    """
    ALTER TABLE knowledge_graph_results
    ADD COLUMN verification_policy_version TEXT NOT NULL DEFAULT 'legacy'
    """,
    """
    UPDATE knowledge_graph_results
    SET retained_count = node_count + edge_count
    """,
    """
    CREATE TABLE knowledge_graph_attempts (
        attempt_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        result_id TEXT UNIQUE REFERENCES knowledge_graph_results(result_id) ON DELETE SET NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('completed', 'completed_empty', 'failed')),
        quality TEXT CHECK(quality IN ('full', 'degraded')),
        capability_identity TEXT,
        prompt_contract_digest TEXT,
        extraction_method TEXT NOT NULL CHECK(extraction_method IN (
            'model', 'deterministic', 'legacy'
        )),
        node_count INTEGER NOT NULL CHECK(node_count >= 0),
        edge_count INTEGER NOT NULL CHECK(edge_count >= 0),
        retained_count INTEGER NOT NULL CHECK(retained_count >= 0),
        weakened_count INTEGER NOT NULL CHECK(weakened_count >= 0),
        rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
        failure_signature TEXT,
        document_version TEXT,
        evidence_snapshot_digest TEXT,
        canonical_schema_version TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        verification_policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(
            (lifecycle = 'failed' AND quality IS NULL AND result_id IS NULL)
            OR (lifecycle <> 'failed' AND quality IS NOT NULL AND result_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX knowledge_graph_attempts_document_idx
        ON knowledge_graph_attempts(document_id, created_at DESC, attempt_id)
    """,
    """
    CREATE TABLE knowledge_graph_attempt_issues (
        attempt_id TEXT NOT NULL
            REFERENCES knowledge_graph_attempts(attempt_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        code TEXT NOT NULL,
        contract_path TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK(disposition IN ('weakened', 'rejected', 'fatal')),
        failure_class TEXT NOT NULL,
        PRIMARY KEY(attempt_id, ordinal)
    )
    """,
    """
    INSERT OR IGNORE INTO knowledge_graph_attempts (
        attempt_id, document_id, result_id, lifecycle, quality,
        capability_identity, prompt_contract_digest, extraction_method,
        node_count, edge_count, retained_count, weakened_count, rejected_count,
        failure_signature, document_version, evidence_snapshot_digest,
        canonical_schema_version, normalizer_version, verification_policy_version, created_at
    )
    SELECT 'legacy-result:' || result_id, document_id, result_id, status, quality,
        capability_identity, prompt_contract_digest, extraction_method,
        node_count, edge_count, retained_count, weakened_count, rejected_count,
        NULL, document_version, evidence_snapshot_digest,
        canonical_schema_version, normalizer_version, verification_policy_version, created_at
    FROM knowledge_graph_results
    """,
)
