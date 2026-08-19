"""Schema additions for source-backed structured Knowledge Analysis."""

from __future__ import annotations

import hashlib
import sqlite3

from openkb.desktop_knowledge_metadata import decode_knowledge_labels


def register_knowledge_analysis_migration_functions(connection: sqlite3.Connection) -> None:
    """Register deterministic helpers used only by Knowledge Analysis data migrations."""
    connection.create_function(
        "openkb_strip_legacy_analysis_metadata",
        3,
        _strip_legacy_analysis_metadata,
        deterministic=True,
    )
    connection.create_function(
        "openkb_knowledge_content_sha256",
        1,
        _knowledge_content_sha256,
        deterministic=True,
    )


def _strip_legacy_analysis_metadata(
    content_markdown: str, aliases_json: str, tags_json: str
) -> str:
    prefixes: list[str] = []
    aliases = decode_knowledge_labels(aliases_json)
    tags = decode_knowledge_labels(tags_json)
    if aliases:
        prefixes.append(f"Aliases: {', '.join(aliases)}")
    if tags:
        prefixes.append(f"Tags: {', '.join(tags)}")
    prefix = "\n\n".join(prefixes)
    if prefix and content_markdown.startswith(f"{prefix}\n\n"):
        return content_markdown[len(prefix) + 2 :]
    return content_markdown


def _knowledge_content_sha256(value: str) -> str:
    normalized = "\n".join(
        " ".join(line.split()) for line in value.splitlines() if line.strip()
    ).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

KNOWLEDGE_ANALYSIS_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_reconciliation_candidates
    ADD COLUMN entity_subtype TEXT
    """,
    """
    CREATE TABLE knowledge_reconciliation_candidate_sources (
        candidate_id TEXT NOT NULL
            REFERENCES knowledge_reconciliation_candidates(candidate_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        PRIMARY KEY(candidate_id, source_id)
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
        PRIMARY KEY(generation_id, item_key, source_id),
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
    ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'""",
    "ALTER TABLE knowledge_generation_items ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE knowledge_generation_items ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
    "DROP INDEX knowledge_reconciliation_candidate_sources_evidence_idx",
    """ALTER TABLE knowledge_reconciliation_candidate_sources
    RENAME TO knowledge_reconciliation_candidate_sources_v26""",
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
    INSERT INTO knowledge_reconciliation_candidate_sources (
        candidate_id, source_id, evidence_id, claim_text
    )
    SELECT candidate_id, source_id, evidence_id, claim_text
    FROM knowledge_reconciliation_candidate_sources_v26
    """,
    "DROP TABLE knowledge_reconciliation_candidate_sources_v26",
    """
    CREATE INDEX knowledge_reconciliation_candidate_sources_evidence_idx
        ON knowledge_reconciliation_candidate_sources(evidence_id, candidate_id)
    """,
    "DROP INDEX knowledge_generation_item_sources_evidence_idx",
    "ALTER TABLE knowledge_generation_item_sources RENAME TO knowledge_generation_item_sources_v26",
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
    INSERT INTO knowledge_generation_item_sources (
        generation_id, item_key, source_id, evidence_id, claim_text
    )
    SELECT generation_id, item_key, source_id, evidence_id, claim_text
    FROM knowledge_generation_item_sources_v26
    """,
    "DROP TABLE knowledge_generation_item_sources_v26",
    """
    CREATE INDEX knowledge_generation_item_sources_evidence_idx
        ON knowledge_generation_item_sources(evidence_id, generation_id, item_key)
    """,
    "DROP INDEX knowledge_page_working_sources_evidence_idx",
    "ALTER TABLE knowledge_page_working_sources RENAME TO knowledge_page_working_sources_v20",
    """
    CREATE TABLE knowledge_page_working_sources (
        page_id TEXT NOT NULL REFERENCES knowledge_page_working_drafts(page_id)
            ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(page_id, source_id, claim_text)
    )
    """,
    """
    INSERT INTO knowledge_page_working_sources
    SELECT * FROM knowledge_page_working_sources_v20
    """,
    "DROP TABLE knowledge_page_working_sources_v20",
    """
    CREATE INDEX knowledge_page_working_sources_evidence_idx
        ON knowledge_page_working_sources(evidence_id, page_id)
    """,
    "DROP INDEX knowledge_page_revision_sources_evidence_idx",
    "ALTER TABLE knowledge_page_revision_sources RENAME TO knowledge_page_revision_sources_v20",
    """
    CREATE TABLE knowledge_page_revision_sources (
        revision_id TEXT NOT NULL REFERENCES knowledge_page_revisions(revision_id)
            ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        claim_text TEXT NOT NULL,
        document_id TEXT NOT NULL,
        document_name TEXT NOT NULL,
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(revision_id, source_id, claim_text)
    )
    """,
    """
    INSERT INTO knowledge_page_revision_sources
    SELECT * FROM knowledge_page_revision_sources_v20
    """,
    "DROP TABLE knowledge_page_revision_sources_v20",
    """
    CREATE INDEX knowledge_page_revision_sources_evidence_idx
        ON knowledge_page_revision_sources(evidence_id, revision_id)
    """,
    """
    UPDATE knowledge_reconciliation_candidates
    SET entity_subtype = NULL,
        analysis_provenance_json = NULL,
        aliases_json = '[]',
        tags_json = '[]'
    WHERE resolution_status IS NOT NULL
    """,
    """
    UPDATE knowledge_generation_items AS items
    SET aliases_json = COALESCE(
            (
                SELECT json_extract(candidate.value, '$.aliases')
                FROM import_jobs AS jobs
                JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                    AND stages.stage = 'model_analysis'
                JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
                JOIN json_each(
                    runtime.checkpoint_json,
                    CASE items.kind
                        WHEN 'entity' THEN '$.normalized_result.entities'
                        ELSE '$.normalized_result.concepts'
                    END
                ) AS candidate
                WHERE jobs.document_id = COALESCE(
                        (
                            SELECT fingerprints.canonical_document_id
                            FROM document_content_fingerprints AS fingerprints
                            WHERE fingerprints.document_id = items.source_document_id
                        ),
                        items.source_document_id
                    )
                    AND json_extract(candidate.value, '$.title') = items.title
                ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
                LIMIT 1
            ),
            aliases_json
        ),
        tags_json = COALESCE(
            (
                SELECT json_extract(candidate.value, '$.tags')
                FROM import_jobs AS jobs
                JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                    AND stages.stage = 'model_analysis'
                JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
                JOIN json_each(
                    runtime.checkpoint_json,
                    CASE items.kind
                        WHEN 'entity' THEN '$.normalized_result.entities'
                        ELSE '$.normalized_result.concepts'
                    END
                ) AS candidate
                WHERE jobs.document_id = COALESCE(
                        (
                            SELECT fingerprints.canonical_document_id
                            FROM document_content_fingerprints AS fingerprints
                            WHERE fingerprints.document_id = items.source_document_id
                        ),
                        items.source_document_id
                    )
                    AND json_extract(candidate.value, '$.title') = items.title
                ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
                LIMIT 1
            ),
            tags_json
        )
    WHERE items.provenance_state = 'source_backed'
    """,
    """
    UPDATE knowledge_generation_items AS items
    SET analysis_provenance_json = COALESCE(
        (
            SELECT json_object(
                'schema_version', COALESCE(
                    json_extract(runtime.checkpoint_json, '$.schema_version'),
                    'openkb.knowledge-analysis.v1'
                ),
                'provider', COALESCE(
                    json_extract(runtime.checkpoint_json, '$.provider'), 'unknown'
                ),
                'model', COALESCE(json_extract(runtime.checkpoint_json, '$.model'), 'unknown'),
                'prompt_digest', COALESCE(json_extract(runtime.checkpoint_json, '$.prompt_digest'),
                    'unknown'),
                'engine_version', COALESCE(
                    json_extract(runtime.checkpoint_json, '$.engine_version'), 'unknown'
                )
            )
            FROM import_jobs AS jobs
            JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                AND stages.stage = 'model_analysis'
            JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
            WHERE jobs.document_id = COALESCE(
                    (
                        SELECT fingerprints.canonical_document_id
                        FROM document_content_fingerprints AS fingerprints
                        WHERE fingerprints.document_id = items.source_document_id
                    ),
                    items.source_document_id
                )
                AND runtime.checkpoint_json IS NOT NULL
            ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
            LIMIT 1
        ),
        '{"schema_version":"openkb.knowledge-analysis.v1","provider":"unknown","model":"unknown","prompt_digest":"unknown","engine_version":"unknown"}'
    )
    WHERE items.provenance_state = 'source_backed'
        AND items.analysis_provenance_json IS NULL
    """,
    """
    UPDATE knowledge_reconciliation_candidates AS candidates
    SET analysis_provenance_json = COALESCE(
            candidates.analysis_provenance_json,
            (
                SELECT json_object(
                    'schema_version', COALESCE(
                        json_extract(runtime.checkpoint_json, '$.schema_version'),
                        'openkb.knowledge-analysis.v1'
                    ),
                    'provider', COALESCE(
                        json_extract(runtime.checkpoint_json, '$.provider'), 'unknown'
                    ),
                    'model', COALESCE(
                        json_extract(runtime.checkpoint_json, '$.model'), 'unknown'
                    ),
                    'prompt_digest', COALESCE(
                        json_extract(runtime.checkpoint_json, '$.prompt_digest'), 'unknown'
                    ),
                    'engine_version', COALESCE(
                        json_extract(runtime.checkpoint_json, '$.engine_version'), 'unknown'
                    )
                )
                FROM import_jobs AS jobs
                JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                    AND stages.stage = 'model_analysis'
                JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
                WHERE jobs.document_id = COALESCE(
                        (
                            SELECT fingerprints.canonical_document_id
                            FROM document_content_fingerprints AS fingerprints
                            WHERE fingerprints.document_id = candidates.document_id
                        ),
                        candidates.document_id
                    )
                    AND runtime.checkpoint_json IS NOT NULL
                ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
                LIMIT 1
            ),
            '{"schema_version":"openkb.knowledge-analysis.v1","provider":"unknown","model":"unknown","prompt_digest":"unknown","engine_version":"unknown"}'
        ),
        aliases_json = COALESCE(
            (
                SELECT json_extract(candidate.value, '$.aliases')
                FROM import_jobs AS jobs
                JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                    AND stages.stage = 'model_analysis'
                JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
                JOIN json_each(
                    runtime.checkpoint_json,
                    CASE candidates.kind
                        WHEN 'entity' THEN '$.normalized_result.entities'
                        ELSE '$.normalized_result.concepts'
                    END
                ) AS candidate
                WHERE jobs.document_id = COALESCE(
                        (
                            SELECT fingerprints.canonical_document_id
                            FROM document_content_fingerprints AS fingerprints
                            WHERE fingerprints.document_id = candidates.document_id
                        ),
                        candidates.document_id
                    )
                    AND json_extract(candidate.value, '$.title') = candidates.title
                ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
                LIMIT 1
            ),
            aliases_json
        ),
        tags_json = COALESCE(
            (
                SELECT json_extract(candidate.value, '$.tags')
                FROM import_jobs AS jobs
                JOIN stage_runs AS stages ON stages.job_id = jobs.job_id
                    AND stages.stage = 'model_analysis'
                JOIN stage_run_runtime AS runtime ON runtime.stage_run_id = stages.stage_run_id
                JOIN json_each(
                    runtime.checkpoint_json,
                    CASE candidates.kind
                        WHEN 'entity' THEN '$.normalized_result.entities'
                        ELSE '$.normalized_result.concepts'
                    END
                ) AS candidate
                WHERE jobs.document_id = COALESCE(
                        (
                            SELECT fingerprints.canonical_document_id
                            FROM document_content_fingerprints AS fingerprints
                            WHERE fingerprints.document_id = candidates.document_id
                        ),
                        candidates.document_id
                    )
                    AND json_extract(candidate.value, '$.title') = candidates.title
                ORDER BY COALESCE(stages.completed_at, runtime.updated_at) DESC
                LIMIT 1
            ),
            tags_json
        )
    WHERE EXISTS (
            SELECT 1 FROM knowledge_reconciliation_candidate_sources AS sources
            WHERE sources.candidate_id = candidates.candidate_id
        )
    """,
    """
    UPDATE knowledge_generation_items
    SET content_markdown = openkb_strip_legacy_analysis_metadata(
            content_markdown, aliases_json, tags_json
        ),
        content_sha256 = openkb_knowledge_content_sha256(
            openkb_strip_legacy_analysis_metadata(content_markdown, aliases_json, tags_json)
        )
    WHERE provenance_state = 'source_backed'
    """,
    """
    UPDATE knowledge_reconciliation_candidates
    SET content_markdown = openkb_strip_legacy_analysis_metadata(
            content_markdown, aliases_json, tags_json
        ),
        content_sha256 = openkb_knowledge_content_sha256(
            openkb_strip_legacy_analysis_metadata(content_markdown, aliases_json, tags_json)
        )
    WHERE analysis_provenance_json IS NOT NULL
    """,
)

KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS = (
    KNOWLEDGE_ANALYSIS_METADATA_MIGRATION_STATEMENTS[-6:]
)
