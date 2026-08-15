"""SQLite persistence for completed Desktop grounded answers and citations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_answer_types import (
    DesktopAnswerError,
    DesktopAnswerSourceImage,
    DesktopEvidenceRef,
    DesktopGroundedAnswer,
    DesktopRetrievalPlan,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock


class DesktopGroundedAnswerStore:
    """Keep completed answers auditable even after source material changes later."""

    def __init__(self, kb_dir: Path) -> None:
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)

    def save(self, answer: DesktopGroundedAnswer) -> DesktopGroundedAnswer:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO grounded_answers (
                            answer_id, question, answer_text, retrieval_plan_json,
                            degradations_json, created_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            answer.answer_id,
                            answer.question,
                            answer.answer_text,
                            json.dumps(answer.retrieval_plan.as_dict(), ensure_ascii=False),
                            json.dumps(answer.degradations, ensure_ascii=False),
                            answer.created_at,
                            answer.created_at,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO grounded_answer_citations (
                            answer_id, evidence_id, ordinal, document_id, document_name, section,
                            locator_json, excerpt, channels_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                answer.answer_id,
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
                        INSERT INTO grounded_answer_source_images (
                            answer_id, source_image_id, evidence_id, ordinal
                        ) VALUES (?, ?, ?, ?)
                        """,
                        [
                            (
                                answer.answer_id,
                                image.source_image_id,
                                image.evidence_id,
                                ordinal,
                            )
                            for ordinal, image in enumerate(answer.source_images)
                        ],
                    )
            finally:
                connection.close()
        return answer

    def list(self) -> tuple[DesktopGroundedAnswer, ...]:
        connection = _connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT answer_id, question, answer_text, retrieval_plan_json, degradations_json,
                    created_at
                FROM grounded_answers
                ORDER BY created_at DESC
                """
            ).fetchall()
            return tuple(_answer_from_row(connection, row, self._state_dir.parent) for row in rows)
        finally:
            connection.close()


def new_answer(
    *,
    answer_id: str,
    question: str,
    answer_text: str,
    retrieval_plan: DesktopRetrievalPlan,
    citations: tuple[DesktopEvidenceRef, ...],
    degradations: tuple[str, ...],
    source_images: tuple[DesktopAnswerSourceImage, ...] = (),
) -> DesktopGroundedAnswer:
    return DesktopGroundedAnswer(
        answer_id=answer_id,
        question=question,
        answer_text=answer_text,
        retrieval_plan=retrieval_plan,
        citations=citations,
        degradations=degradations,
        created_at=_timestamp(),
        source_images=source_images,
    )


def _answer_from_row(
    connection: sqlite3.Connection, row: tuple[object, ...], kb_dir: Path
) -> DesktopGroundedAnswer:
    answer_id = str(row[0])
    plan_payload = _json_object(str(row[3]))
    plan = DesktopRetrievalPlan(
        query=str(plan_payload.get("query", row[1])),
        terms=_string_values(plan_payload.get("terms")),
        source=str(plan_payload.get("source", "deterministic")),
    )
    citations = tuple(
        DesktopEvidenceRef(
            evidence_id=str(citation[0]),
            document_id=str(citation[2]),
            document_name=str(citation[3]),
            section=str(citation[4]),
            locator=_json_object(str(citation[5])),
            excerpt=str(citation[6]),
            channels=_string_values(_json_list(str(citation[7]))),
        )
        for citation in connection.execute(
            """
            SELECT evidence_id, ordinal, document_id, document_name, section, locator_json,
                excerpt, channels_json
            FROM grounded_answer_citations
            WHERE answer_id = ?
            ORDER BY ordinal
            """,
            (answer_id,),
        ).fetchall()
    )
    return DesktopGroundedAnswer(
        answer_id=answer_id,
        question=str(row[1]),
        answer_text=str(row[2]),
        retrieval_plan=plan,
        citations=citations,
        degradations=tuple(value for value in _json_list(str(row[4])) if isinstance(value, str)),
        created_at=str(row[5]),
        source_images=_source_images_for_answer(connection, answer_id, kb_dir),
    )


def _source_images_for_answer(
    connection: sqlite3.Connection, answer_id: str, kb_dir: Path
) -> tuple[DesktopAnswerSourceImage, ...]:
    """Hydrate only image snapshots whose cited source remains available."""
    rows = connection.execute(
        """
        SELECT snapshots.source_image_id, snapshots.evidence_id,
            source_images.document_id, source_documents.display_name,
            source_images.display_name, source_images.media_type,
            source_images.storage_path, source_images.alt_text, source_images.locator_json
        FROM grounded_answer_source_images AS snapshots
        JOIN grounded_answer_citations AS citations
            ON citations.answer_id = snapshots.answer_id
            AND citations.evidence_id = snapshots.evidence_id
        JOIN source_images
            ON source_images.source_image_id = snapshots.source_image_id
            AND source_images.document_id = citations.document_id
        JOIN source_documents ON source_documents.document_id = source_images.document_id
        WHERE snapshots.answer_id = ? AND source_documents.availability = 'available'
        ORDER BY snapshots.ordinal
        """,
        (answer_id,),
    ).fetchall()
    images: list[DesktopAnswerSourceImage] = []
    for row in rows:
        file_path = kb_dir / str(row[6])
        if not file_path.is_file():
            continue
        images.append(
            DesktopAnswerSourceImage(
                source_image_id=str(row[0]),
                evidence_id=str(row[1]),
                document_id=str(row[2]),
                document_name=str(row[3]),
                name=str(row[4]),
                media_type=str(row[5]),
                file_path=str(file_path),
                alt_text=str(row[7]) if row[7] is not None else None,
                locator={**_json_object(str(row[8])), "source_image_id": str(row[0])},
            )
        )
    return tuple(images)


def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DesktopAnswerError(
            "desktop_knowledge_base_not_found",
            "Open a Desktop Knowledge Base before asking a question.",
        )
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
