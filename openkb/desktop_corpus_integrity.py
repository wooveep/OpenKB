"""Deterministic activation report for one immutable Corpus Generation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from openkb.desktop_corpus_synthesis_generation import (
    corpus_manifest_compatibility_issues_in,
)
from openkb.desktop_knowledge_page_store import generation_knowledge_page_issues_in
from openkb.desktop_knowledge_relationships import generation_relationship_issues_in

CORPUS_INTEGRITY_SCHEMA_VERSION = "openkb.corpus-generation-integrity.v1"


@dataclass(frozen=True)
class CorpusGenerationIntegrityReport:
    """Code-owned proof of identifiers, snapshots, rendering, and Evidence bindings."""

    schema_version: str
    generation_id: int
    item_integrity_passed: bool
    evidence_binding_integrity_passed: bool
    page_plan_integrity_passed: bool
    relation_integrity_passed: bool
    snapshot_integrity_passed: bool
    issues: tuple[str, ...]
    passed: bool

    def as_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def corpus_generation_integrity_report_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> CorpusGenerationIntegrityReport:
    """Evaluate only deterministic publication invariants, never semantic taste."""
    item_issues, evidence_issues = _item_and_evidence_issues_in(connection, generation_id)
    page_issues = generation_knowledge_page_issues_in(connection, generation_id)
    relation_issues = generation_relationship_issues_in(connection, generation_id)
    snapshot_issues = corpus_manifest_compatibility_issues_in(connection, generation_id)
    issues = tuple(
        dict.fromkeys(
            (*item_issues, *evidence_issues, *page_issues, *relation_issues, *snapshot_issues)
        )
    )
    return CorpusGenerationIntegrityReport(
        schema_version=CORPUS_INTEGRITY_SCHEMA_VERSION,
        generation_id=generation_id,
        item_integrity_passed=not item_issues,
        evidence_binding_integrity_passed=not evidence_issues,
        page_plan_integrity_passed=not page_issues,
        relation_integrity_passed=not relation_issues,
        snapshot_integrity_passed=not snapshot_issues,
        issues=issues,
        passed=not issues,
    )


def corpus_generation_integrity_issues_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[str, ...]:
    return corpus_generation_integrity_report_in(connection, generation_id).issues


def _item_and_evidence_issues_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from openkb.desktop_knowledge_generations import knowledge_content_sha256

    item_issues: list[str] = []
    evidence_issues: list[str] = []
    rows = connection.execute(
        "SELECT item_key, content_markdown, content_sha256, identity_id, provenance_state "
        "FROM knowledge_generation_items WHERE generation_id = ?",
        (generation_id,),
    ).fetchall()
    if any(
        not str(row[0]).strip()
        or not str(row[1]).strip()
        or str(row[2]) != knowledge_content_sha256(str(row[1]))
        or row[3] is None
        or str(row[4]) != "source_backed"
        for row in rows
    ):
        item_issues.append("invalid_generation_item")
    duplicate_identity = connection.execute(
        "SELECT 1 FROM knowledge_generation_items WHERE generation_id = ? "
        "GROUP BY identity_id HAVING COUNT(*) > 1 LIMIT 1",
        (generation_id,),
    ).fetchone()
    if duplicate_identity is not None:
        item_issues.append("duplicate_generation_identity")

    missing_source = connection.execute(
        "SELECT 1 FROM knowledge_generation_items AS items "
        "WHERE items.generation_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM knowledge_generation_item_sources AS sources "
        "WHERE sources.generation_id = items.generation_id "
        "AND sources.item_key = items.item_key) LIMIT 1",
        (generation_id,),
    ).fetchone()
    if missing_source is not None:
        evidence_issues.append("missing_item_evidence_binding")
    invalid_source = connection.execute(
        "SELECT 1 FROM knowledge_generation_item_sources AS sources "
        "WHERE sources.generation_id = ? AND (trim(sources.claim_text) = '' OR NOT EXISTS ("
        "SELECT 1 FROM evidence_occurrences AS occurrences "
        "JOIN source_documents AS documents ON documents.document_id = occurrences.document_id "
        "WHERE occurrences.evidence_id = sources.evidence_id "
        "AND documents.availability = 'available')) LIMIT 1",
        (generation_id,),
    ).fetchone()
    if invalid_source is not None:
        evidence_issues.append("invalid_item_evidence_binding")
    return tuple(item_issues), tuple(evidence_issues)
