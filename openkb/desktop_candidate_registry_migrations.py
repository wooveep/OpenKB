"""Additive schema for immutable document Candidate Registry Generations."""

CANDIDATE_REGISTRY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate_generations (
        candidate_generation_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        analysis_provenance_json TEXT NOT NULL,
        analysis_provenance_digest TEXT NOT NULL,
        registry_digest TEXT NOT NULL,
        candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
        admitted_count INTEGER NOT NULL CHECK(admitted_count >= 0),
        schema_version TEXT NOT NULL,
        ontology_version TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        admission_policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(admitted_count <= candidate_count),
        UNIQUE(document_id, candidate_generation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_candidate_generations_document_idx
        ON knowledge_candidate_generations(document_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate_generation_candidates (
        candidate_generation_id TEXT NOT NULL
            REFERENCES knowledge_candidate_generations(candidate_generation_id)
            ON DELETE CASCADE,
        candidate_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        entity_subtype TEXT,
        aliases_json TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        admission_state TEXT NOT NULL CHECK(admission_state IN ('admitted', 'rejected')),
        admission_reason TEXT NOT NULL,
        PRIMARY KEY(candidate_generation_id, candidate_id),
        UNIQUE(candidate_generation_id, kind, normalized_title)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate_generation_claims (
        candidate_generation_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
        role TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        PRIMARY KEY(candidate_generation_id, candidate_id, claim_ordinal),
        FOREIGN KEY(candidate_generation_id, candidate_id)
            REFERENCES knowledge_candidate_generation_candidates(
                candidate_generation_id, candidate_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate_generation_claim_sources (
        candidate_generation_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL,
        evidence_id TEXT NOT NULL
            REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(
            candidate_generation_id, candidate_id, claim_ordinal, evidence_id
        ),
        FOREIGN KEY(candidate_generation_id, candidate_id, claim_ordinal)
            REFERENCES knowledge_candidate_generation_claims(
                candidate_generation_id, candidate_id, claim_ordinal
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_candidate_generation_sources_evidence_idx
        ON knowledge_candidate_generation_claim_sources(
            candidate_generation_id, evidence_id
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidate_registry_state (
        document_id TEXT PRIMARY KEY
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        provenance_state TEXT NOT NULL CHECK(provenance_state IN (
            'semantic', 'explicit_legacy', 'dependency_unavailable'
        )),
        current_candidate_generation_id TEXT
            REFERENCES knowledge_candidate_generations(candidate_generation_id)
            ON DELETE RESTRICT,
        updated_at TEXT NOT NULL,
        CHECK(
            (provenance_state = 'semantic' AND current_candidate_generation_id IS NOT NULL)
            OR
            (provenance_state != 'semantic' AND current_candidate_generation_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_manifests (
        generation_id INTEGER PRIMARY KEY
            REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
        parent_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
            'pending', 'identity_ready', 'qualified', 'active', 'failed',
            'cancelled', 'superseded'
        )),
        dossier_state TEXT NOT NULL CHECK(dossier_state IN ('pending', 'ready', 'failed')),
        graph_state TEXT NOT NULL CHECK(graph_state IN (
            'pending', 'ready', 'completed_empty', 'degraded', 'unavailable_optional'
        )),
        manifest_digest TEXT NOT NULL,
        compatibility_digest TEXT NOT NULL,
        qualification_policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_candidate_inputs (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        candidate_generation_id TEXT NOT NULL
            REFERENCES knowledge_candidate_generations(candidate_generation_id)
            ON DELETE RESTRICT,
        candidate_generation_digest TEXT NOT NULL,
        PRIMARY KEY(generation_id, document_id),
        UNIQUE(generation_id, candidate_generation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_identity_mappings (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        identity_id TEXT NOT NULL,
        candidate_generation_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        match_basis TEXT NOT NULL,
        PRIMARY KEY(generation_id, identity_id, candidate_generation_id, candidate_id),
        FOREIGN KEY(candidate_generation_id, candidate_id)
            REFERENCES knowledge_candidate_generation_candidates(
                candidate_generation_id, candidate_id
            ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_candidate_registry_document_insert
    AFTER INSERT ON source_documents
    BEGIN
        INSERT INTO knowledge_candidate_registry_state (
            document_id, provenance_state, current_candidate_generation_id, updated_at
        ) VALUES (
            NEW.document_id, 'dependency_unavailable', NULL,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        );
    END
    """,
    """
    ALTER TABLE knowledge_graph_extraction_tasks
        ADD COLUMN input_provenance TEXT NOT NULL DEFAULT 'dependency_unavailable'
        CHECK(input_provenance IN ('semantic', 'explicit_legacy', 'dependency_unavailable'))
    """,
    """
    ALTER TABLE knowledge_graph_extraction_tasks
        ADD COLUMN candidate_generation_id TEXT
    """,
    """
    ALTER TABLE knowledge_graph_extraction_tasks
        ADD COLUMN candidate_generation_digest TEXT
    """,
    """
    ALTER TABLE knowledge_graph_results ADD COLUMN candidate_generation_id TEXT
    """,
    """
    ALTER TABLE knowledge_graph_results ADD COLUMN candidate_generation_digest TEXT
    """,
    """
    ALTER TABLE knowledge_graph_attempts ADD COLUMN candidate_generation_id TEXT
    """,
    """
    ALTER TABLE knowledge_graph_attempts ADD COLUMN candidate_generation_digest TEXT
    """,
    """
    ALTER TABLE knowledge_document_relationships ADD COLUMN candidate_generation_id TEXT
    """,
)
