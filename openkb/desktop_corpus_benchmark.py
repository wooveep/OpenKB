"""Persistable quality measurements for candidate corpus generations."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass

from openkb.desktop_real_corpus_benchmark import (
    RealCorpusBenchmarkAttestation,
    load_real_corpus_benchmark,
)

CORPUS_BENCHMARK_SCHEMA_VERSION = "openkb.corpus-benchmark.v2"
MAX_NOISE_LEAKAGE_RATE = 0.02
MAX_DUPLICATE_IDENTITY_RATE = 0.05
MIN_MULTI_DOCUMENT_TOPIC_COVERAGE = 0.85
MIN_PROCEDURE_STAGE_COVERAGE = 0.85
_PROCEDURE_STAGE_ROLES = frozenset(
    ("purpose", "prerequisite", "step", "validation", "rollback", "troubleshooting")
)
_PROCEDURE_STAGE_HEADINGS = {
    "purpose": frozenset(("目标", "goal")),
    "prerequisite": frozenset(("前置条件", "prerequisites")),
    "step": frozenset(("操作步骤", "steps")),
    "validation": frozenset(("验证", "validation")),
    "rollback": frozenset(("回滚", "rollback")),
    "troubleshooting": frozenset(("故障排查", "troubleshooting")),
}
_MARKDOWN_SECTION = re.compile(r"(?m)^##\s+")


@dataclass(frozen=True)
class CorpusBenchmarkReport:
    """Deterministic release criteria measured against one immutable generation."""

    schema_version: str
    evidence_integrity_passed: bool
    noise_leakage_rate: float
    duplicate_identity_rate: float
    multi_document_topic_coverage: float
    procedure_stage_coverage: float
    structural_gate_passed: bool
    real_corpus_benchmark: RealCorpusBenchmarkAttestation
    passed: bool

    def as_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def corpus_benchmark_report_in(
    connection: sqlite3.Connection, generation_id: int
) -> CorpusBenchmarkReport:
    """Measure generation structure and bind it to the fixed real-corpus release gate."""
    total_items = _count(
        connection,
        "SELECT COUNT(*) FROM knowledge_generation_items WHERE generation_id = ?",
        generation_id,
    )
    invalid_sources = _count(
        connection,
        """
        SELECT COUNT(*) FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ? AND (
            trim(sources.claim_text) = '' OR NOT EXISTS (
                SELECT 1 FROM evidence_occurrences AS occurrences
                JOIN source_documents AS documents
                  ON documents.document_id = occurrences.document_id
                WHERE occurrences.evidence_id = sources.evidence_id
                  AND documents.availability = 'available'
            )
        )
        """,
        generation_id,
    )
    missing_sources = _count(
        connection,
        """
        SELECT COUNT(*) FROM knowledge_generation_items AS items
        WHERE items.generation_id = ? AND NOT EXISTS (
            SELECT 1 FROM knowledge_generation_item_sources AS sources
            WHERE sources.generation_id = items.generation_id
              AND sources.item_key = items.item_key
        )
        """,
        generation_id,
    )
    noise_items = _count(
        connection,
        """
        SELECT COUNT(*) FROM knowledge_generation_items
        WHERE generation_id = ? AND (
            provenance_state != 'source_backed' OR identity_id IS NULL
        )
        """,
        generation_id,
    )
    duplicate_items = _count(
        connection,
        """
        SELECT COALESCE(SUM(item_count - 1), 0) FROM (
            SELECT COUNT(*) AS item_count FROM knowledge_generation_items
            WHERE generation_id = ? GROUP BY identity_id HAVING item_count > 1
        )
        """,
        generation_id,
    )
    multi_document_coverage = _multi_document_topic_coverage(connection, generation_id)
    procedure_stage_coverage = _procedure_stage_coverage(connection, generation_id)
    real_corpus_benchmark = load_real_corpus_benchmark()
    noise_rate = noise_items / total_items if total_items else 1.0
    duplicate_rate = duplicate_items / total_items if total_items else 1.0
    evidence_passed = total_items > 0 and invalid_sources == 0 and missing_sources == 0
    structural_gate_passed = (
        evidence_passed
        and noise_rate <= MAX_NOISE_LEAKAGE_RATE
        and duplicate_rate <= MAX_DUPLICATE_IDENTITY_RATE
        and multi_document_coverage >= MIN_MULTI_DOCUMENT_TOPIC_COVERAGE
        and procedure_stage_coverage >= MIN_PROCEDURE_STAGE_COVERAGE
    )
    return CorpusBenchmarkReport(
        schema_version=CORPUS_BENCHMARK_SCHEMA_VERSION,
        evidence_integrity_passed=evidence_passed,
        noise_leakage_rate=noise_rate,
        duplicate_identity_rate=duplicate_rate,
        multi_document_topic_coverage=multi_document_coverage,
        procedure_stage_coverage=procedure_stage_coverage,
        structural_gate_passed=structural_gate_passed,
        real_corpus_benchmark=real_corpus_benchmark,
        passed=structural_gate_passed and real_corpus_benchmark.passed,
    )


def _multi_document_topic_coverage(connection: sqlite3.Connection, generation_id: int) -> float:
    eligible = connection.execute(
        """
        SELECT bindings.identity_id
        FROM knowledge_identity_candidates AS bindings
        JOIN knowledge_document_candidates AS candidates
          ON candidates.candidate_id = bindings.candidate_id
        JOIN source_documents AS documents ON documents.document_id = candidates.document_id
        WHERE candidates.admission_state = 'admitted' AND documents.availability = 'available'
        GROUP BY bindings.identity_id
        HAVING COUNT(DISTINCT candidates.document_id) >= 2
        """
    ).fetchall()
    if not eligible:
        return 1.0
    covered = 0
    for (identity_id,) in eligible:
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT occurrences.document_id)
            FROM knowledge_generation_items AS items
            JOIN knowledge_generation_item_sources AS sources
              ON sources.generation_id = items.generation_id
             AND sources.item_key = items.item_key
            JOIN evidence_occurrences AS occurrences
              ON occurrences.evidence_id = sources.evidence_id
            WHERE items.generation_id = ? AND items.identity_id = ?
            """,
            (generation_id, str(identity_id)),
        ).fetchone()
        covered += row is not None and int(row[0]) >= 2
    return covered / len(eligible)


def _procedure_stage_coverage(connection: sqlite3.Connection, generation_id: int) -> float:
    rows = connection.execute(
        """
        SELECT items.content_markdown, claims.role, item_sources.source_id
        FROM knowledge_generation_items AS items
        JOIN knowledge_identity_candidates AS bindings ON bindings.identity_id = items.identity_id
        JOIN knowledge_document_candidate_claims AS claims
          ON claims.candidate_id = bindings.candidate_id
        JOIN knowledge_document_candidate_claim_sources AS claim_sources
          ON claim_sources.candidate_id = claims.candidate_id
         AND claim_sources.claim_ordinal = claims.claim_ordinal
        JOIN knowledge_generation_item_sources AS item_sources
          ON item_sources.generation_id = items.generation_id
         AND item_sources.item_key = items.item_key
         AND item_sources.evidence_id = claim_sources.evidence_id
        WHERE items.generation_id = ? AND items.kind = 'procedure'
          AND claims.role IN ('purpose', 'prerequisite', 'step', 'validation',
                              'rollback', 'troubleshooting')
        ORDER BY items.item_key, claims.claim_ordinal, claim_sources.evidence_id
        """,
        (generation_id,),
    ).fetchall()
    supported = [row for row in rows if str(row[1]) in _PROCEDURE_STAGE_ROLES]
    if not supported:
        return 1.0
    covered = sum(
        _procedure_section_has_source(str(row[0]), str(row[1]), str(row[2])) for row in supported
    )
    return covered / len(supported)


def _procedure_section_has_source(content: str, role: str, source_id: str) -> bool:
    expected_headings = _PROCEDURE_STAGE_HEADINGS[role]
    marker = f"[^{source_id}]"
    for section in _MARKDOWN_SECTION.split(content):
        heading, _separator, body = section.partition("\n")
        if heading.strip().casefold() in expected_headings and marker in body:
            return True
    return False


def _count(connection: sqlite3.Connection, query: str, generation_id: int) -> int:
    row = connection.execute(query, (generation_id,)).fetchone()
    return int(row[0]) if row is not None else 0
