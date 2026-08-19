"""Compatibility checks for structured Knowledge Analysis migrations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_analysis_migrations import (
    KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS,
    register_knowledge_analysis_migration_functions,
)
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_v28_metadata_backfill_follows_a_d1_canonical_checkpoint(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    baseline = tmp_path / "baseline.md"
    canonical = tmp_path / "one.md"
    duplicate = tmp_path / "two.md"
    baseline.write_text("# Concept: Migrated\n\nMode: local", encoding="utf-8")
    canonical.write_bytes(b"Incoming source for D1 candidate.\n")
    duplicate.write_bytes(b"Incoming source for D1 candidate.\r\n")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(baseline)

    def analyze(request, _timeout_seconds):
        evidence_id = str(json.loads(request.content)["evidence"][0]["evidence_id"])
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "D1 migration fixture.",
                "concepts": [
                    {
                        "title": "Migrated",
                        "aliases": ["d1 alias"],
                        "tags": ["d1-tag"],
                        "claims": [
                            {"text": "Mode: global", "source_evidence_ids": [evidence_id]}
                        ],
                    }
                ],
                "entities": [],
            }
        )

    canonical_result = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            analyze, provider_name="d1-provider", model_name="d1-model"
        ),
    ).import_text(canonical)
    d1 = DesktopTextImportService(kb_dir).import_text(duplicate)
    assert d1.job.deduplication is not None
    assert d1.job.deduplication.level == "D1"

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        candidate_id = str(
            connection.execute(
                """
                SELECT candidate_id FROM knowledge_reconciliation_candidates
                WHERE document_id = ? AND status = 'pending_conflict'
                """,
                (d1.document.document_id,),
            ).fetchone()[0]
        )
        canonical_candidate_id = str(
            connection.execute(
                """
                SELECT candidate_id FROM knowledge_reconciliation_candidates
                WHERE document_id = ? AND status = 'pending_conflict'
                """,
                (canonical_result.document.document_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET analysis_provenance_json = NULL, aliases_json = '[]', tags_json = '[]',
                content_markdown = 'Aliases: d1 alias\n\nTags: d1-tag\n\n'
                    || content_markdown,
                content_sha256 = 'legacy-v26-hash'
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        )
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET aliases_json = '[]', tags_json = '[]',
                content_markdown = 'Aliases: d1 alias\n\nTags: d1-tag\n\n'
                    || content_markdown,
                content_sha256 = 'legacy-v27-hash'
            WHERE candidate_id = ?
            """,
            (canonical_candidate_id,),
        )
        register_knowledge_analysis_migration_functions(connection)
        for statement in KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS:
            connection.execute(statement)
        row = connection.execute(
            """
            SELECT analysis_provenance_json, aliases_json, tags_json,
                content_markdown, content_sha256
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        v27_row = connection.execute(
            """
            SELECT analysis_provenance_json, aliases_json, tags_json,
                content_markdown, content_sha256
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (canonical_candidate_id,),
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert row is not None
    assert json.loads(str(row[0]))["provider"] == "d1-provider"
    assert row[1:3] == ('["d1 alias"]', '["d1-tag"]')
    assert str(row[3]).startswith("Mode: global[^src-")
    assert row[4] != "legacy-v26-hash"
    assert v27_row is not None
    assert json.loads(str(v27_row[0]))["provider"] == "d1-provider"
    assert v27_row[1:3] == ('["d1 alias"]', '["d1-tag"]')
    assert str(v27_row[3]).startswith("Mode: global[^src-")
    assert v27_row[4] != "legacy-v27-hash"

    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((candidate_id,), "keep_current")
    resolution.commit_staged_decisions()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET entity_subtype = 'Organization', aliases_json = '["discarded alias"]',
                tags_json = '["discarded-tag"]',
                analysis_provenance_json = '{"provider":"discarded-provider"}'
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        )
        register_knowledge_analysis_migration_functions(connection)
        for statement in KNOWLEDGE_ANALYSIS_METADATA_BACKFILL_STATEMENTS:
            connection.execute(statement)
        discarded = connection.execute(
            """
            SELECT entity_subtype, aliases_json, tags_json, analysis_provenance_json
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
    assert discarded == (None, "[]", "[]", None)
