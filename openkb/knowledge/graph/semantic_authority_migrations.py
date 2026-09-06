"""Current-epoch storage for evidence-bound semantic plans and outcomes."""

SEMANTIC_AUTHORITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_document_relation_assertions (
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        candidate_generation_id TEXT NOT NULL
            REFERENCES knowledge_candidate_generations(candidate_generation_id)
            ON DELETE CASCADE,
        graph_result_id TEXT NOT NULL
            REFERENCES knowledge_graph_results(result_id) ON DELETE CASCADE,
        assertion_id TEXT NOT NULL,
        source_candidate_id TEXT NOT NULL,
        target_candidate_id TEXT NOT NULL,
        label TEXT NOT NULL,
        normalized_label TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        PRIMARY KEY(document_id, assertion_id),
        UNIQUE(
            document_id, candidate_generation_id, source_candidate_id,
            target_candidate_id, normalized_label
        ),
        CHECK(source_candidate_id <> target_candidate_id)
    )
    """,
    """
    CREATE TABLE knowledge_document_relation_sources (
        document_id TEXT NOT NULL,
        assertion_id TEXT NOT NULL,
        support_candidate_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
        evidence_id TEXT NOT NULL
            REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(
            document_id, assertion_id, support_candidate_id, claim_ordinal, evidence_id
        ),
        FOREIGN KEY(document_id, assertion_id)
            REFERENCES knowledge_document_relation_assertions(document_id, assertion_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_generation_relation_assertions (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        assertion_id TEXT NOT NULL,
        source_identity_id TEXT NOT NULL,
        target_identity_id TEXT NOT NULL,
        label TEXT NOT NULL,
        normalized_label TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        PRIMARY KEY(generation_id, assertion_id),
        UNIQUE(
            generation_id, source_identity_id, target_identity_id, normalized_label
        ),
        CHECK(source_identity_id <> target_identity_id)
    )
    """,
    """
    CREATE TABLE knowledge_generation_relation_sources (
        generation_id INTEGER NOT NULL,
        assertion_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL
            REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(generation_id, assertion_id, evidence_id),
        FOREIGN KEY(generation_id, assertion_id)
            REFERENCES knowledge_generation_relation_assertions(generation_id, assertion_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX knowledge_generation_relation_adjacency_source_idx
        ON knowledge_generation_relation_assertions(generation_id, source_identity_id)
    """,
    """
    CREATE INDEX knowledge_generation_relation_adjacency_target_idx
        ON knowledge_generation_relation_assertions(generation_id, target_identity_id)
    """,
    """
    CREATE TABLE knowledge_generation_page_outcomes (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        identity_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready', 'deferred', 'carried_forward')),
        claim_snapshot_digest TEXT NOT NULL,
        published_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE RESTRICT,
        error_codes_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, identity_id),
        CHECK(
            (status = 'ready' AND published_generation_id = generation_id)
            OR (status = 'deferred' AND published_generation_id IS NULL)
            OR (
                status = 'carried_forward'
                AND published_generation_id IS NOT NULL
                AND published_generation_id <> generation_id
            )
        )
    )
    """,
    """
    CREATE TABLE knowledge_generation_page_plans (
        generation_id INTEGER NOT NULL,
        identity_id TEXT NOT NULL,
        claim_snapshot_digest TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        planning_operation TEXT NOT NULL CHECK(planning_operation = 'knowledge_page_planning'),
        prompt_contract_digest TEXT NOT NULL,
        execution_profile_json TEXT NOT NULL,
        execution_profile_digest TEXT NOT NULL,
        planner_provenance_json TEXT NOT NULL,
        rendered_content_digest TEXT NOT NULL,
        factual_unit_count INTEGER NOT NULL CHECK(factual_unit_count >= 0),
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, identity_id),
        FOREIGN KEY(generation_id, identity_id)
            REFERENCES knowledge_generation_page_outcomes(generation_id, identity_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_generation_page_sections (
        generation_id INTEGER NOT NULL,
        identity_id TEXT NOT NULL,
        section_id TEXT NOT NULL,
        parent_section_id TEXT,
        section_ordinal INTEGER NOT NULL CHECK(section_ordinal >= 0),
        depth INTEGER NOT NULL CHECK(depth IN (1, 2)),
        title TEXT NOT NULL,
        PRIMARY KEY(generation_id, identity_id, section_id),
        FOREIGN KEY(generation_id, identity_id)
            REFERENCES knowledge_generation_page_plans(generation_id, identity_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_generation_page_units (
        generation_id INTEGER NOT NULL,
        identity_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        section_id TEXT,
        unit_ordinal INTEGER NOT NULL CHECK(unit_ordinal >= 0),
        presentation TEXT NOT NULL CHECK(
            presentation IN ('paragraph', 'unordered_list', 'ordered_list')
        ),
        PRIMARY KEY(generation_id, identity_id, unit_id),
        FOREIGN KEY(generation_id, identity_id)
            REFERENCES knowledge_generation_page_plans(generation_id, identity_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_generation_page_unit_claims (
        generation_id INTEGER NOT NULL,
        identity_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
        PRIMARY KEY(generation_id, identity_id, unit_id, claim_id),
        UNIQUE(generation_id, identity_id, claim_id),
        FOREIGN KEY(generation_id, identity_id, unit_id)
            REFERENCES knowledge_generation_page_units(generation_id, identity_id, unit_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_generation_page_unit_relations (
        generation_id INTEGER NOT NULL,
        identity_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        assertion_id TEXT NOT NULL,
        relation_ordinal INTEGER NOT NULL CHECK(relation_ordinal >= 0),
        PRIMARY KEY(generation_id, identity_id, unit_id, assertion_id),
        UNIQUE(generation_id, identity_id, assertion_id),
        FOREIGN KEY(generation_id, identity_id, unit_id)
            REFERENCES knowledge_generation_page_units(generation_id, identity_id, unit_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX knowledge_generation_page_outcomes_status_idx
        ON knowledge_generation_page_outcomes(generation_id, status, identity_id)
    """,
)
