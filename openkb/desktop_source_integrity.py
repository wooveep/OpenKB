"""Content-free integrity audit for the imported source-to-evidence chain."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SOURCE_INTEGRITY_SCHEMA_VERSION = "openkb.source-integrity.v2"


@dataclass(frozen=True)
class DesktopSourceIntegrityReport:
    """Aggregate diagnostics that never expose imported source content."""

    status: str
    counts: dict[str, int]
    issues: dict[str, int]
    block_kind_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": SOURCE_INTEGRITY_SCHEMA_VERSION,
            "status": self.status,
            "counts": dict(self.counts),
            "issues": dict(self.issues),
            "block_kind_counts": dict(self.block_kind_counts),
        }


def audit_source_integrity_in(
    connection: sqlite3.Connection,
    *,
    kb_dir: Path | None = None,
) -> DesktopSourceIntegrityReport:
    """Audit structural preservation and canonical evidence bindings in one snapshot."""
    counts = {
        "available_documents": _count(
            connection,
            "SELECT COUNT(*) FROM source_documents WHERE availability = 'available'",
        ),
        "document_ir_blocks": _count(
            connection,
            """
            SELECT COUNT(*) FROM document_ir_blocks AS blocks
            JOIN source_documents AS documents USING(document_id)
            WHERE documents.availability = 'available'
            """,
        ),
        "evidence_refs": _count(connection, "SELECT COUNT(*) FROM evidence_refs"),
        "evidence_occurrences": _count(
            connection,
            """
            SELECT COUNT(*) FROM evidence_occurrences AS occurrences
            JOIN source_documents AS documents USING(document_id)
            WHERE documents.availability = 'available'
            """,
        ),
        "document_summaries": _count(connection, "SELECT COUNT(*) FROM document_summaries"),
        "document_summary_units": _count(connection, "SELECT COUNT(*) FROM document_summary_units"),
        "current_page_trees": _count(connection, "SELECT COUNT(*) FROM document_page_tree_current"),
    }
    issues = {
        "available_documents_without_blocks": _count(
            connection,
            """
            SELECT COUNT(*) FROM source_documents AS documents
            WHERE documents.availability = 'available'
                AND NOT EXISTS (
                    SELECT 1 FROM document_ir_blocks AS blocks
                    WHERE blocks.document_id = documents.document_id
                )
            """,
        ),
        "available_documents_without_evidence_occurrences": _count(
            connection,
            """
            SELECT COUNT(*) FROM source_documents AS documents
            WHERE documents.availability = 'available'
                AND NOT EXISTS (
                    SELECT 1 FROM evidence_occurrences AS occurrences
                    WHERE occurrences.document_id = documents.document_id
                )
            """,
        ),
        "empty_block_text": _count(
            connection, "SELECT COUNT(*) FROM document_ir_blocks WHERE trim(text) = ''"
        ),
        "empty_evidence_text": _count(
            connection, "SELECT COUNT(*) FROM evidence_refs WHERE trim(text) = ''"
        ),
        "orphan_or_misbound_evidence_refs": _count(
            connection,
            """
            SELECT COUNT(*) FROM evidence_refs AS evidence
            LEFT JOIN document_ir_blocks AS blocks ON blocks.block_id = evidence.block_id
            WHERE blocks.block_id IS NULL OR blocks.document_id <> evidence.document_id
            """,
        ),
        "orphan_or_misbound_evidence_occurrences": _count(
            connection,
            """
            SELECT COUNT(*) FROM evidence_occurrences AS occurrences
            LEFT JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
            LEFT JOIN evidence_refs AS evidence ON evidence.evidence_id = occurrences.evidence_id
            WHERE blocks.block_id IS NULL OR evidence.evidence_id IS NULL
                OR blocks.document_id <> occurrences.document_id
            """,
        ),
        "documents_with_block_ordinal_gaps": _count(
            connection,
            """
            SELECT COUNT(*) FROM (
                SELECT document_id FROM document_ir_blocks
                GROUP BY document_id
                HAVING MIN(ordinal) <> 0 OR MAX(ordinal) + 1 <> COUNT(*)
            )
            """,
        ),
        "documents_with_occurrence_ordinal_gaps": _count(
            connection,
            """
            SELECT COUNT(*) FROM (
                SELECT document_id FROM evidence_occurrences
                GROUP BY document_id
                HAVING MIN(ordinal) <> 0 OR MAX(ordinal) + 1 <> COUNT(*)
            )
            """,
        ),
        "invalid_block_locators": _invalid_json_count(
            connection, "SELECT locator_json FROM document_ir_blocks", expected=dict
        ),
        "invalid_evidence_locators": _invalid_json_count(
            connection, "SELECT locator_json FROM evidence_refs", expected=dict
        ),
        "invalid_heading_paths": _invalid_json_count(
            connection,
            "SELECT heading_path FROM document_ir_blocks",
            expected=list,
            string_items=True,
        ),
        **_missing_expected_structure_counts(connection, kb_dir),
    }
    block_kind_counts = {
        str(kind): int(count)
        for kind, count in connection.execute(
            """
            SELECT blocks.kind, COUNT(*)
            FROM document_ir_blocks AS blocks
            JOIN source_documents AS documents USING(document_id)
            WHERE documents.availability = 'available'
            GROUP BY blocks.kind ORDER BY blocks.kind
            """
        )
    }
    return DesktopSourceIntegrityReport(
        status="healthy" if not any(issues.values()) else "degraded",
        counts=counts,
        issues=issues,
        block_kind_counts=block_kind_counts,
    )


def _missing_expected_structure_counts(
    connection: sqlite3.Connection,
    kb_dir: Path | None,
) -> dict[str, int]:
    issue_names = {
        "heading": "documents_missing_expected_headings",
        "code": "documents_missing_expected_code",
        "table": "documents_missing_expected_tables",
    }
    missing = {name: 0 for name in issue_names.values()}
    if kb_dir is None:
        return missing
    rows = connection.execute(
        """
        SELECT documents.document_id, documents.source_format, raw.raw_path
        FROM source_documents AS documents
        JOIN raw_assets AS raw ON raw.asset_sha256 = documents.asset_sha256
        WHERE documents.availability = 'available'
        ORDER BY documents.document_id
        """
    ).fetchall()
    actual = {
        str(document_id): {str(kind) for (kind,) in kind_rows}
        for document_id in (str(row[0]) for row in rows)
        if (
            kind_rows := connection.execute(
                "SELECT DISTINCT kind FROM document_ir_blocks WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        )
    }
    for document_id, source_format, raw_path in rows:
        expected = _expected_structure_kinds(
            kb_dir,
            source_format=str(source_format),
            raw_path=str(raw_path),
        )
        present = actual.get(str(document_id), set())
        for kind in expected - present:
            missing[issue_names[kind]] += 1
    return missing


def _expected_structure_kinds(
    kb_dir: Path,
    *,
    source_format: str,
    raw_path: str,
) -> set[str]:
    if source_format not in {"markdown", "md"}:
        return set()
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        return set()
    try:
        content = (kb_dir / relative).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    expected: set[str] = set()
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+\S", content):
        expected.add("heading")
    if re.search(r"(?m)^\s{0,3}(?:```|~~~)", content):
        expected.add("code")
    if re.search(r"(?m)^\s*\|?.+\|.+\n\s*\|?\s*:?-{3,}", content):
        expected.add("table")
    return expected


def _count(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    return int(row[0]) if row is not None else 0


def _invalid_json_count(
    connection: sqlite3.Connection,
    statement: str,
    *,
    expected: type[dict] | type[list],
    string_items: bool = False,
) -> int:
    invalid = 0
    for (encoded,) in connection.execute(statement):
        try:
            value = json.loads(str(encoded))
        except (json.JSONDecodeError, TypeError, ValueError):
            invalid += 1
            continue
        if not isinstance(value, expected) or (
            string_items and not all(isinstance(item, str) for item in value)
        ):
            invalid += 1
    return invalid
