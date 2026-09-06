"""One scope-aware occurrence projection shared by every retrieval channel."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace

from openkb.answers.types import DesktopAnswerSourceImage, DesktopEvidenceRef
from openkb.documents.version_scope import VersionScope

VERSION_OCCURRENCES_LOCATOR_KEY = "_openkb_version_occurrences"


@dataclass(frozen=True)
class ScopedEvidenceOccurrence:
    evidence_id: str
    document_id: str
    document_name: str
    block_id: str
    ordinal: int
    heading_path: tuple[str, ...]
    locator: dict[str, object]
    text: str
    version_label: str | None
    side: str | None = None


class ScopedEvidenceView:
    """Filter documents first, then project canonical Evidence to an occurrence."""

    def __init__(self, scope: VersionScope) -> None:
        self.scope = scope

    def sql_cte(self, name: str = "scoped_evidence_occurrences") -> tuple[str, tuple[object, ...]]:
        if name not in {"scoped_evidence_occurrences", "available_evidence_occurrences"}:
            raise ValueError("Unsupported Scoped Evidence CTE name.")
        allowed = tuple(sorted(self.scope.allowed_document_ids))
        if not allowed:
            return _empty_cte(name), ()
        preferred = tuple(
            document_id
            for document_id in self.scope.preferred_occurrence_document_ids
            if document_id in self.scope.allowed_document_ids
        )
        case_parts = [
            f"WHEN occurrences.document_id = ? THEN {index}"
            for index, _document_id in enumerate(preferred)
        ]
        preference = (
            "CASE " + " ".join(case_parts) + f" ELSE {len(preferred)} END" if case_parts else "0"
        )
        placeholders = ", ".join("?" for _ in allowed)
        return (
            f"""
            WITH {name} AS (
                SELECT occurrences.evidence_id, occurrences.document_id,
                    documents.display_name, occurrences.block_id, occurrences.ordinal,
                    blocks.heading_path, blocks.locator_json, blocks.text,
                    members.version_label,
                    ROW_NUMBER() OVER (
                        PARTITION BY occurrences.evidence_id
                        ORDER BY {preference}, occurrences.document_id, occurrences.ordinal
                    ) AS occurrence_rank
                FROM evidence_occurrences AS occurrences
                JOIN source_documents AS documents
                  ON documents.document_id = occurrences.document_id
                JOIN document_ir_blocks AS blocks
                  ON blocks.document_id = occurrences.document_id
                 AND blocks.block_id = occurrences.block_id
                LEFT JOIN document_version_members AS members
                  ON members.document_id = occurrences.document_id
                WHERE documents.availability = 'available'
                  AND occurrences.document_id IN ({placeholders})
            )
            """,
            (*preferred, *allowed),
        )

    def preferred_occurrence_in(
        self, connection: sqlite3.Connection, evidence_id: str
    ) -> ScopedEvidenceOccurrence | None:
        cte, parameters = self.sql_cte()
        row = connection.execute(
            f"""
            {cte}
            SELECT evidence_id, document_id, display_name, block_id, ordinal,
                heading_path, locator_json, text, version_label
            FROM scoped_evidence_occurrences
            WHERE evidence_id = ? AND occurrence_rank = 1
            """,
            (*parameters, evidence_id),
        ).fetchone()
        return _occurrence(row) if row is not None else None

    def occurrences_for_evidence_in(
        self, connection: sqlite3.Connection, evidence_id: str
    ) -> tuple[ScopedEvidenceOccurrence, ...]:
        cte, parameters = self.sql_cte()
        rows = connection.execute(
            f"""
            {cte}
            SELECT evidence_id, document_id, display_name, block_id, ordinal,
                heading_path, locator_json, text, version_label
            FROM scoped_evidence_occurrences
            WHERE evidence_id = ? ORDER BY occurrence_rank
            """,
            (*parameters, evidence_id),
        ).fetchall()
        values = []
        for index, row in enumerate(rows):
            occurrence = _occurrence(row)
            side = (
                ("left" if index == 0 else "right" if index == 1 else None)
                if self.scope.mode == "compare"
                else None
            )
            values.append(replace(occurrence, side=side))
        return tuple(values)


def project_scoped_evidence_in(
    connection: sqlite3.Connection,
    evidence: tuple[DesktopEvidenceRef, ...],
    scoped_view: ScopedEvidenceView,
) -> tuple[DesktopEvidenceRef, ...]:
    """Rebind every canonical EvidenceRef to a selected in-scope occurrence."""
    projected: list[DesktopEvidenceRef] = []
    seen: set[str] = set()
    for reference in evidence:
        if reference.evidence_id in seen:
            continue
        occurrences = scoped_view.occurrences_for_evidence_in(connection, reference.evidence_id)
        if not occurrences:
            continue
        seen.add(reference.evidence_id)
        preferred = occurrences[0]
        locator = dict(preferred.locator)
        if scoped_view.scope.mode == "compare":
            locator[VERSION_OCCURRENCES_LOCATOR_KEY] = [
                {
                    "document_id": occurrence.document_id,
                    "block_id": occurrence.block_id,
                    "ordinal": occurrence.ordinal,
                    "version_label": occurrence.version_label,
                    "version_side": occurrence.side,
                    "locator": occurrence.locator,
                }
                for occurrence in occurrences
            ]
        projected.append(
            replace(
                reference,
                document_id=preferred.document_id,
                document_name=preferred.document_name,
                section=" / ".join(preferred.heading_path),
                locator=locator,
                excerpt=preferred.text.strip(),
                version_label=preferred.version_label,
                version_side=preferred.side,
            )
        )
    return tuple(projected)


def scoped_source_images(
    images: tuple[DesktopAnswerSourceImage, ...],
    evidence: tuple[DesktopEvidenceRef, ...],
    scoped_view: ScopedEvidenceView,
) -> tuple[DesktopAnswerSourceImage, ...]:
    """Enforce the final source-image document and Evidence association postcondition."""
    citations = {(item.evidence_id, item.document_id) for item in evidence}
    return tuple(
        image
        for image in images
        if image.document_id in scoped_view.scope.allowed_document_ids
        and (image.evidence_id, image.document_id) in citations
    )


def _empty_cte(name: str) -> str:
    return f"""
    WITH {name} AS (
        SELECT occurrences.evidence_id, occurrences.document_id,
            documents.display_name, occurrences.block_id, occurrences.ordinal,
            blocks.heading_path, blocks.locator_json, blocks.text,
            members.version_label, 1 AS occurrence_rank
        FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents ON 0
        JOIN document_ir_blocks AS blocks ON 0
        LEFT JOIN document_version_members AS members ON 0
        WHERE 0
    )
    """


def _occurrence(row: tuple[object, ...]) -> ScopedEvidenceOccurrence:
    try:
        heading = json.loads(str(row[5]))
        locator = json.loads(str(row[6]))
    except json.JSONDecodeError as error:
        raise ValueError("Scoped Evidence occurrence metadata is invalid.") from error
    if not isinstance(heading, list) or not all(isinstance(value, str) for value in heading):
        raise ValueError("Scoped Evidence heading path is invalid.")
    if not isinstance(locator, dict):
        raise ValueError("Scoped Evidence locator is invalid.")
    return ScopedEvidenceOccurrence(
        evidence_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        block_id=str(row[3]),
        ordinal=int(str(row[4])),
        heading_path=tuple(heading),
        locator=dict(locator),
        text=str(row[7]),
        version_label=str(row[8]) if row[8] is not None else None,
    )
