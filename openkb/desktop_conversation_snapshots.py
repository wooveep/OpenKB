"""Immutable EvidenceRef and Source Image snapshots for conversation answers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_answer_types import DesktopGroundedAnswer
from openkb.desktop_retrieval_channels import normalize_retrieval_channels


def insert_answer_version(
    connection: sqlite3.Connection,
    version_id: str,
    assistant_message_id: str,
    version_number: int,
    answer: DesktopGroundedAnswer,
    kb_dir: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO conversation_answer_versions (
            answer_version_id, assistant_message_id, version_number, answer_text,
            retrieval_plan_json, degradations_json, status, interruption_code,
            interruption_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            assistant_message_id,
            version_number,
            answer.answer_text,
            json.dumps(answer.retrieval_plan.as_dict(), ensure_ascii=False),
            json.dumps(answer.degradations, ensure_ascii=False),
            answer.status,
            answer.interruption_code,
            answer.interruption_reason,
            answer.created_at,
        ),
    )
    connection.executemany(
        """
        INSERT INTO conversation_answer_citations (
            answer_version_id, evidence_id, ordinal, document_id, document_name,
            section, locator_json, excerpt, channels_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                version_id,
                citation.evidence_id,
                ordinal,
                citation.document_id,
                citation.document_name,
                citation.section,
                json.dumps(citation.locator, ensure_ascii=False),
                citation.excerpt,
                json.dumps(citation.channels, ensure_ascii=False),
            )
            for ordinal, citation in enumerate(answer.citations)
        ],
    )
    connection.executemany(
        """
        INSERT INTO conversation_answer_source_images (
            answer_version_id, source_image_id, evidence_id, ordinal,
            document_id, document_name, display_name, media_type,
            storage_path, alt_text, locator_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                version_id,
                image.source_image_id,
                image.evidence_id,
                ordinal,
                image.document_id,
                image.document_name,
                image.name,
                image.media_type,
                _relative_image_path(kb_dir, image.file_path),
                image.alt_text,
                json.dumps(image.locator, ensure_ascii=False),
            )
            for ordinal, image in enumerate(answer.source_images)
        ],
    )


def version_citations(connection: sqlite3.Connection, version_id: str) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": str(row[0]),
            "document_id": str(row[1]),
            "document_name": str(row[2]),
            "section": str(row[3]),
            "locator": json_object(str(row[4])),
            "excerpt": str(row[5]),
            "channels": list(
                normalize_retrieval_channels(
                    value for value in json_list(str(row[6])) if isinstance(value, str)
                )
            ),
            "source_available": bool(row[7]),
        }
        for row in connection.execute(
            """
            SELECT snapshots.evidence_id, snapshots.document_id, snapshots.document_name,
                snapshots.section, snapshots.locator_json, snapshots.excerpt,
                snapshots.channels_json,
                COALESCE(source_documents.availability = 'available', 0)
            FROM conversation_answer_citations snapshots
            LEFT JOIN source_documents
                ON source_documents.document_id = snapshots.document_id
            WHERE snapshots.answer_version_id = ? ORDER BY snapshots.ordinal
            """,
            (version_id,),
        ).fetchall()
    ]


def version_images(
    connection: sqlite3.Connection, version_id: str, kb_dir: Path
) -> list[dict[str, object]]:
    images: list[dict[str, object]] = []
    for row in connection.execute(
        """
        SELECT snapshots.source_image_id, snapshots.evidence_id, snapshots.document_id,
            snapshots.document_name, snapshots.display_name, snapshots.media_type,
            snapshots.storage_path, snapshots.alt_text, snapshots.locator_json,
            COALESCE(source_documents.availability = 'available', 0)
        FROM conversation_answer_source_images snapshots
        LEFT JOIN source_documents ON source_documents.document_id = snapshots.document_id
        WHERE snapshots.answer_version_id = ? ORDER BY snapshots.ordinal
        """,
        (version_id,),
    ).fetchall():
        storage_path = str(row[6])
        file_path = kb_dir / storage_path if storage_path else None
        images.append(
            {
                "source_image_id": str(row[0]),
                "evidence_id": str(row[1]),
                "document_id": str(row[2]),
                "document_name": str(row[3]),
                "name": str(row[4]),
                "media_type": str(row[5]),
                "file_path": str(file_path) if file_path and file_path.is_file() else "",
                "alt_text": str(row[7]) if row[7] is not None else None,
                "locator": json_object(str(row[8])),
                "source_available": bool(row[9]) and bool(file_path and file_path.is_file()),
            }
        )
    return images


def json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _relative_image_path(kb_dir: Path, file_path: str) -> str:
    try:
        return Path(file_path).resolve().relative_to(kb_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return ""
