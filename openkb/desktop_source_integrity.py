"""Content-free integrity audit for the imported source-to-evidence chain."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_source_structure_expectations import expected_structure_kinds

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
        "evidence_locator_mismatches": _count(
            connection,
            """
            SELECT COUNT(*) FROM evidence_refs AS evidence
            JOIN document_ir_blocks AS blocks ON blocks.block_id = evidence.block_id
            WHERE evidence.locator_json <> blocks.locator_json
            """,
        ),
        **_locator_issue_counts(connection),
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
        relative = Path(str(raw_path))
        expected = (
            set()
            if relative.is_absolute() or ".." in relative.parts
            else expected_structure_kinds(str(source_format), kb_dir / relative)
        )
        present = actual.get(str(document_id), set())
        for kind in expected - present:
            missing[issue_names[kind]] += 1
    return missing


def _locator_issue_counts(connection: sqlite3.Connection) -> dict[str, int]:
    invalid_ranges = 0
    regressed_documents: set[str] = set()
    prior_position: dict[str, tuple[int, ...]] = {}
    rows = connection.execute(
        """
        SELECT blocks.document_id, blocks.locator_json
        FROM document_ir_blocks AS blocks
        JOIN source_documents AS documents USING(document_id)
        WHERE documents.availability = 'available'
        ORDER BY blocks.document_id, blocks.ordinal
        """
    )
    for document_id_value, encoded in rows:
        try:
            locator = json.loads(str(encoded))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(locator, dict):
            continue
        if _locator_has_invalid_range(locator):
            invalid_ranges += 1
        position = _locator_position(locator)
        document_id = str(document_id_value)
        if position is not None and position < prior_position.get(document_id, position):
            regressed_documents.add(document_id)
        if position is not None:
            prior_position[document_id] = position
    return {
        "invalid_locator_ranges": invalid_ranges,
        "documents_with_locator_regressions": len(regressed_documents),
    }


def _locator_has_invalid_range(locator: dict[str, object]) -> bool:
    for start_key, end_key in (
        ("line_start", "line_end"),
        ("paragraph_start", "paragraph_end"),
        ("row_start", "row_end"),
    ):
        start, end = locator.get(start_key), locator.get(end_key)
        if start is not None or end is not None:
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end < start
            ):
                return True
    return False


def _locator_position(locator: dict[str, object]) -> tuple[int, ...] | None:
    for keys in (
        ("line_start",),
        ("body_order",),
        ("page_index", "block_index"),
        ("slide_index", "shape_index", "paragraph_start"),
        ("sheet_index", "row_start"),
    ):
        values = tuple(locator.get(key, 0) for key in keys)
        if any(key in locator for key in keys) and all(
            isinstance(value, int) and not isinstance(value, bool) for value in values
        ):
            positions: list[int] = []
            for value in values:
                assert isinstance(value, int) and not isinstance(value, bool)
                positions.append(value)
            return tuple(positions)
    return None


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
