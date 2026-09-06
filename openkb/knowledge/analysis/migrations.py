"""Current-epoch schema additions for source-backed Knowledge Analysis."""

KNOWLEDGE_ANALYSIS_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_reconciliation_candidate_sources (
        candidate_id TEXT NOT NULL
            REFERENCES knowledge_reconciliation_candidates(candidate_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        PRIMARY KEY(candidate_id, source_id, claim_text)
    )
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidate_sources_evidence_idx
        ON knowledge_reconciliation_candidate_sources(evidence_id, candidate_id)
    """,
    """
    CREATE TABLE knowledge_generation_item_sources (
        generation_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        PRIMARY KEY(generation_id, item_key, source_id, claim_text),
        FOREIGN KEY(generation_id, item_key)
            REFERENCES knowledge_generation_items(generation_id, item_key) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX knowledge_generation_item_sources_evidence_idx
        ON knowledge_generation_item_sources(evidence_id, generation_id, item_key)
    """,
)

KNOWLEDGE_ANALYSIS_PROVENANCE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE knowledge_reconciliation_candidates ADD COLUMN analysis_provenance_json TEXT",
    "ALTER TABLE knowledge_generation_items ADD COLUMN analysis_provenance_json TEXT",
)

KNOWLEDGE_ANALYSIS_METADATA_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """ALTER TABLE knowledge_reconciliation_candidates
    ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'""",
    """ALTER TABLE knowledge_reconciliation_candidates
    ADD COLUMN identity_labels_json TEXT NOT NULL DEFAULT '[]'""",
    "ALTER TABLE knowledge_generation_items ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'",
    """ALTER TABLE knowledge_generation_items
    ADD COLUMN identity_labels_json TEXT NOT NULL DEFAULT '[]'""",
)
