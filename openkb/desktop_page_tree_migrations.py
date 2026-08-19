"""SQLite schema for immutable deterministic Document PageTree generations."""

PAGE_TREE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE document_page_tree_generations (
        generation_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        provider_kind TEXT NOT NULL,
        provider_version TEXT NOT NULL,
        structural_ir_fingerprint TEXT NOT NULL,
        locator_mapping_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('current', 'superseded')),
        created_at TEXT NOT NULL,
        UNIQUE(document_id, generation_id)
    )
    """,
    """
    CREATE TABLE document_page_tree_nodes (
        generation_id TEXT NOT NULL
            REFERENCES document_page_tree_generations(generation_id) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        parent_node_id TEXT,
        node_order INTEGER NOT NULL CHECK(node_order >= 0),
        depth INTEGER NOT NULL CHECK(depth >= 0),
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT,
        locator_json TEXT NOT NULL,
        PRIMARY KEY(generation_id, node_id),
        UNIQUE(generation_id, node_order),
        FOREIGN KEY(generation_id, parent_node_id)
            REFERENCES document_page_tree_nodes(generation_id, node_id)
    )
    """,
    """
    CREATE TABLE document_page_tree_node_evidence (
        generation_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id),
        block_ordinal INTEGER NOT NULL CHECK(block_ordinal >= 0),
        association_order INTEGER NOT NULL CHECK(association_order >= 0),
        PRIMARY KEY(generation_id, node_id, evidence_id),
        FOREIGN KEY(generation_id, node_id)
            REFERENCES document_page_tree_nodes(generation_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE document_page_tree_node_images (
        generation_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        source_image_id TEXT NOT NULL REFERENCES source_images(source_image_id),
        image_ordinal INTEGER NOT NULL CHECK(image_ordinal >= 0),
        association_order INTEGER NOT NULL CHECK(association_order >= 0),
        PRIMARY KEY(generation_id, node_id, source_image_id),
        FOREIGN KEY(generation_id, node_id)
            REFERENCES document_page_tree_nodes(generation_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE document_page_tree_current (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        generation_id TEXT NOT NULL UNIQUE,
        activated_at TEXT NOT NULL,
        FOREIGN KEY(document_id, generation_id)
            REFERENCES document_page_tree_generations(document_id, generation_id)
    )
    """,
    """
    CREATE TABLE document_page_tree_rebuild_tasks (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'failed', 'completed')),
        reason TEXT NOT NULL,
        error_code TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX document_page_tree_generations_document_idx
        ON document_page_tree_generations(document_id, created_at DESC)
    """,
    """
    CREATE INDEX document_page_tree_nodes_order_idx
        ON document_page_tree_nodes(generation_id, node_order)
    """,
    """
    CREATE INDEX document_page_tree_rebuild_status_idx
        ON document_page_tree_rebuild_tasks(status, updated_at)
    """,
    """
    CREATE INDEX import_jobs_document_completed_idx
        ON import_jobs(document_id, completed_at DESC, created_at DESC)
    """,
    """
    INSERT INTO stage_runs (
        stage_run_id, job_id, stage, status, progress, error_code, started_at, completed_at
    )
    SELECT lower(hex(randomblob(16))), jobs.job_id, 'deterministic_page_tree',
        CASE WHEN COALESCE(runtime.status, jobs.status) = 'completed'
                OR jobs.document_id IS NOT NULL
            THEN 'skipped' ELSE 'pending' END,
        CASE WHEN COALESCE(runtime.status, jobs.status) = 'completed'
                OR jobs.document_id IS NOT NULL
            THEN 100 ELSE 0 END,
        NULL, NULL,
        CASE WHEN COALESCE(runtime.status, jobs.status) = 'completed'
                OR jobs.document_id IS NOT NULL
            THEN COALESCE(jobs.completed_at, jobs.created_at) ELSE NULL END
    FROM import_jobs AS jobs
    LEFT JOIN import_job_runtime AS runtime ON runtime.job_id = jobs.job_id
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_runs AS existing
        WHERE existing.job_id = jobs.job_id
            AND existing.stage = 'deterministic_page_tree'
    )
    """,
    """
    INSERT INTO stage_run_runtime (
        stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
    )
    SELECT stages.stage_run_id, stages.job_id, stages.status, NULL, NULL,
        COALESCE(stages.completed_at, runtime.updated_at, jobs.created_at)
    FROM stage_runs AS stages
    JOIN import_jobs AS jobs ON jobs.job_id = stages.job_id
    LEFT JOIN import_job_runtime AS runtime ON runtime.job_id = stages.job_id
    WHERE stages.stage = 'deterministic_page_tree'
        AND NOT EXISTS (
            SELECT 1 FROM stage_run_runtime AS existing
            WHERE existing.stage_run_id = stages.stage_run_id
        )
    """,
    """
    INSERT INTO document_page_tree_rebuild_tasks (
        document_id, status, reason, error_code, attempt_count,
        created_at, updated_at, completed_at
    )
    SELECT document_id, 'pending', 'schema_upgrade', NULL, 0,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL
    FROM source_documents WHERE availability = 'available'
    """,
)
