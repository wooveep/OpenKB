"""Typed, evidence-bound relationships for one deterministic Catalog snapshot."""

CATALOG_RELATIONSHIP_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_catalog_relationships (
        generation_id TEXT NOT NULL
            REFERENCES knowledge_catalog_generations(generation_id) ON DELETE CASCADE,
        source_node_id TEXT NOT NULL,
        target_node_id TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        source_route TEXT NOT NULL,
        target_route TEXT NOT NULL,
        provenance TEXT NOT NULL,
        lifecycle_eligible INTEGER NOT NULL CHECK(lifecycle_eligible IN (0, 1)),
        weight REAL NOT NULL CHECK(weight > 0 AND weight <= 1),
        PRIMARY KEY(generation_id, source_node_id, target_node_id, relation_kind),
        FOREIGN KEY(generation_id, source_node_id)
            REFERENCES knowledge_catalog_nodes(generation_id, node_id) ON DELETE CASCADE,
        FOREIGN KEY(generation_id, target_node_id)
            REFERENCES knowledge_catalog_nodes(generation_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_catalog_relationship_sources (
        generation_id TEXT NOT NULL,
        source_node_id TEXT NOT NULL,
        target_node_id TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        binding_role TEXT NOT NULL CHECK(binding_role IN ('source', 'target', 'supporting')),
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        availability TEXT NOT NULL CHECK(availability IN ('available', 'failed')),
        PRIMARY KEY(
            generation_id, source_node_id, target_node_id, relation_kind,
            binding_role, source_id, evidence_id
        ),
        FOREIGN KEY(generation_id, source_node_id, target_node_id, relation_kind)
            REFERENCES knowledge_catalog_relationships(
                generation_id, source_node_id, target_node_id, relation_kind
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_catalog_relationship_routes_idx
        ON knowledge_catalog_relationships(
            generation_id, source_route, relation_kind, lifecycle_eligible
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_catalog_relationship_evidence_idx
        ON knowledge_catalog_relationship_sources(generation_id, evidence_id, relation_kind)
    """,
)
