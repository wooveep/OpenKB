"""Persist generation-bound Inventory update and alias targets."""

INVENTORY_IDENTITY_BINDING_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_document_candidates
        ADD COLUMN inventory_target_identity_id TEXT
            REFERENCES knowledge_identities(identity_id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE knowledge_document_candidates
        ADD COLUMN inventory_target_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE knowledge_candidate_generation_candidates
        ADD COLUMN inventory_target_identity_id TEXT
            REFERENCES knowledge_identities(identity_id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE knowledge_candidate_generation_candidates
        ADD COLUMN inventory_target_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE RESTRICT
    """,
)
