"""SQLite persistence for completed Desktop grounded answers and citations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.answers.types import (
    DesktopAnswerError,
    DesktopAnswerSourceImage,
    DesktopEvidenceRef,
    DesktopGroundedAnswer,
    DesktopRetrievalPlan,
)
from openkb.locks import kb_ingest_lock
from openkb.retrieval.channels import normalize_retrieval_channels
from openkb.retrieval.trace import DesktopRetrievalTrace, retrieval_trace_from_json
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir


class DesktopGroundedAnswerStore:
    """Keep completed and interrupted answers auditable across Desktop restarts."""

    def __init__(self, kb_dir: Path) -> None:
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)

    def save(self, answer: DesktopGroundedAnswer) -> DesktopGroundedAnswer:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    _insert_answer(connection, answer)
                    _replace_answer_sources(connection, answer)
                    _replace_answer_trace(connection, answer)
            finally:
                connection.close()
        return answer

    def replace_interrupted(self, answer: DesktopGroundedAnswer) -> DesktopGroundedAnswer:
        """Atomically replace an interrupted card only after a complete retry succeeds."""
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    now = _timestamp()
                    cursor = connection.execute(
                        """
                        UPDATE grounded_answers
                        SET question = ?, answer_text = ?, retrieval_plan_json = ?,
                            degradations_json = ?, status = ?, interruption_code = ?,
                            interruption_reason = ?, completed_at = ?, updated_at = ?
                        WHERE answer_id = ? AND status = 'interrupted'
                        """,
                        (
                            answer.question,
                            answer.answer_text,
                            json.dumps(answer.retrieval_plan.as_dict(), ensure_ascii=False),
                            json.dumps(answer.degradations, ensure_ascii=False),
                            answer.status,
                            answer.interruption_code,
                            answer.interruption_reason,
                            now,
                            now,
                            answer.answer_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DesktopAnswerError(
                            "answer_retry_unavailable",
                            "This interrupted answer is no longer available for retry.",
                        )
                    _replace_answer_sources(connection, answer)
                    _replace_answer_trace(connection, answer)
            finally:
                connection.close()
        return answer

    def interrupted(self, answer_id: str) -> DesktopGroundedAnswer:
        """Load one persisted interrupted answer or report a stable retry error."""
        connection = _connect(self._database_path)
        try:
            row = _answer_row(connection, answer_id)
            if row is None:
                raise DesktopAnswerError(
                    "interrupted_answer_not_found", "The interrupted answer was not found."
                )
            answer = _answer_from_row(connection, row, self._state_dir.parent)
            if answer.status != "interrupted":
                raise DesktopAnswerError(
                    "answer_retry_unavailable",
                    "Only an interrupted answer can be retried.",
                )
            return answer
        finally:
            connection.close()

    def list(self) -> tuple[DesktopGroundedAnswer, ...]:
        connection = _connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT answer_id, question, answer_text, retrieval_plan_json, degradations_json,
                    created_at, status, interruption_code, interruption_reason
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
    retrieval_trace: DesktopRetrievalTrace = DesktopRetrievalTrace(),
    status: str = "completed",
    interruption_code: str | None = None,
    interruption_reason: str | None = None,
    created_at: str | None = None,
) -> DesktopGroundedAnswer:
    return DesktopGroundedAnswer(
        answer_id=answer_id,
        question=question,
        answer_text=answer_text,
        retrieval_plan=retrieval_plan,
        citations=citations,
        degradations=degradations,
        created_at=created_at or _timestamp(),
        source_images=source_images,
        retrieval_trace=retrieval_trace,
        status=status,
        interruption_code=interruption_code,
        interruption_reason=interruption_reason,
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
            channels=normalize_retrieval_channels(_string_values(_json_list(str(citation[7])))),
            version_label=str(citation[8]) if citation[8] is not None else None,
            version_side=str(citation[9]) if citation[9] is not None else None,
        )
        for citation in connection.execute(
            """
            SELECT evidence_id, ordinal, document_id, document_name, section, locator_json,
                excerpt, channels_json, version_label, version_side
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
        retrieval_trace=_answer_trace(connection, answer_id),
        status=str(row[6]),
        interruption_code=str(row[7]) if row[7] is not None else None,
        interruption_reason=str(row[8]) if row[8] is not None else None,
    )


def _insert_answer(connection: sqlite3.Connection, answer: DesktopGroundedAnswer) -> None:
    connection.execute(
        """
        INSERT INTO grounded_answers (
            answer_id, question, answer_text, retrieval_plan_json, degradations_json,
            created_at, completed_at, status, interruption_code, interruption_reason, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            answer.answer_id,
            answer.question,
            answer.answer_text,
            json.dumps(answer.retrieval_plan.as_dict(), ensure_ascii=False),
            json.dumps(answer.degradations, ensure_ascii=False),
            answer.created_at,
            answer.created_at,
            answer.status,
            answer.interruption_code,
            answer.interruption_reason,
            answer.created_at,
        ),
    )


def _replace_answer_sources(connection: sqlite3.Connection, answer: DesktopGroundedAnswer) -> None:
    connection.execute(
        "DELETE FROM grounded_answer_source_images WHERE answer_id = ?", (answer.answer_id,)
    )
    connection.execute(
        "DELETE FROM grounded_answer_citations WHERE answer_id = ?", (answer.answer_id,)
    )
    connection.executemany(
        """
        INSERT INTO grounded_answer_citations (
            answer_id, evidence_id, ordinal, document_id, document_name, section,
            locator_json, excerpt, channels_json, version_label, version_side
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                citation.version_label,
                citation.version_side,
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


def _replace_answer_trace(connection: sqlite3.Connection, answer: DesktopGroundedAnswer) -> None:
    connection.execute(
        """
        INSERT INTO grounded_answer_retrieval_traces (answer_id, trace_json)
        VALUES (?, ?)
        ON CONFLICT(answer_id) DO UPDATE SET trace_json = excluded.trace_json
        """,
        (answer.answer_id, json.dumps(answer.retrieval_trace.as_dict(), ensure_ascii=False)),
    )


def _answer_trace(connection: sqlite3.Connection, answer_id: str) -> DesktopRetrievalTrace:
    row = connection.execute(
        "SELECT trace_json FROM grounded_answer_retrieval_traces WHERE answer_id = ?",
        (answer_id,),
    ).fetchone()
    return retrieval_trace_from_json(str(row[0])) if row is not None else DesktopRetrievalTrace()


def _answer_row(connection: sqlite3.Connection, answer_id: str) -> tuple[object, ...] | None:
    return connection.execute(
        """
        SELECT answer_id, question, answer_text, retrieval_plan_json, degradations_json,
            created_at, status, interruption_code, interruption_reason
        FROM grounded_answers
        WHERE answer_id = ?
        """,
        (answer_id,),
    ).fetchone()


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
    connection = connect_database(database_path)
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
