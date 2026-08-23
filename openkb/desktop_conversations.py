"""SQLite-authoritative Desktop conversations and immutable answer versions."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_answer_types import DesktopGroundedAnswer
from openkb.desktop_conversation_snapshots import (
    insert_answer_version,
    json_list,
    json_object,
    version_citations,
    version_images,
    version_retrieval_trace,
)
from openkb.desktop_grounded_answer import (
    AnswerCancellationCallback,
    AnswerDeltaCallback,
    DesktopGroundedAnswerService,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_DEFAULT_TITLE = "New conversation"


class DesktopConversationError(RuntimeError):
    """Stable domain error for conversation commands."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DesktopConversationService:
    """Manage ordered messages while delegating retrieval and generation to the answer service."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None = None) -> None:
        self._kb_dir = kb_dir
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)
        self._answers = DesktopGroundedAnswerService(kb_dir, model_gateway=model_gateway)

    def list(self, search: str = "") -> dict[str, object]:
        connection = _connect(self._database_path)
        try:
            term = search.strip()
            if term:
                like = f"%{term}%"
                rows = connection.execute(
                    """
                    SELECT DISTINCT conversations.conversation_id, conversations.title,
                        conversations.draft_text, conversations.created_at,
                        conversations.updated_at,
                        EXISTS (
                            SELECT 1 FROM conversation_messages running
                            WHERE running.conversation_id = conversations.conversation_id
                                AND running.status = 'generating'
                        )
                    FROM conversations
                    LEFT JOIN conversation_messages
                        ON conversation_messages.conversation_id = conversations.conversation_id
                        AND conversation_messages.role = 'user'
                    WHERE conversations.title LIKE ? OR conversation_messages.content LIKE ?
                    ORDER BY conversations.updated_at DESC
                    """,
                    (like, like),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT conversations.conversation_id, conversations.title,
                        conversations.draft_text, conversations.created_at,
                        conversations.updated_at,
                        EXISTS (
                            SELECT 1 FROM conversation_messages running
                            WHERE running.conversation_id = conversations.conversation_id
                                AND running.status = 'generating'
                        )
                    FROM conversations
                    ORDER BY conversations.updated_at DESC
                    """
                ).fetchall()
            state = connection.execute(
                "SELECT last_conversation_id FROM conversation_ui_state WHERE singleton = 1"
            ).fetchone()
            return {
                "conversations": [_summary(row) for row in rows],
                "last_conversation_id": str(state[0]) if state and state[0] is not None else None,
            }
        finally:
            connection.close()

    def create(self, title: str | None = None) -> dict[str, object]:
        conversation_id = uuid.uuid4().hex
        now = _timestamp()
        normalized_title = (title or _DEFAULT_TITLE).strip() or _DEFAULT_TITLE
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO conversations (
                            conversation_id, title, draft_text, created_at, updated_at
                        ) VALUES (?, ?, '', ?, ?)
                        """,
                        (conversation_id, normalized_title, now, now),
                    )
                    _select_conversation_in(connection, conversation_id)
            finally:
                connection.close()
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> dict[str, object]:
        return self._get(conversation_id, select=True)

    def _get(self, conversation_id: str, *, select: bool) -> dict[str, object]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                row = _conversation_row(connection, conversation_id)
                if select:
                    with connection:
                        _select_conversation_in(connection, conversation_id)
                return _conversation_payload(connection, row, self._kb_dir)
            finally:
                connection.close()

    def rename(self, conversation_id: str, title: str) -> dict[str, object]:
        normalized = title.strip()
        if not normalized:
            raise DesktopConversationError(
                "conversation_title_required", "Enter a conversation title."
            )
        self._update_conversation(conversation_id, "title", normalized)
        return self.get(conversation_id)

    def save_draft(self, conversation_id: str, draft_text: str) -> dict[str, object]:
        self._update_conversation(conversation_id, "draft_text", draft_text, touch=False)
        return self._get(conversation_id, select=False)

    def delete(self, conversation_id: str) -> dict[str, object]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM conversations WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    if cursor.rowcount != 1:
                        raise DesktopConversationError(
                            "conversation_not_found", "The conversation was not found."
                        )
            finally:
                connection.close()
        return self.list()

    def ask(
        self,
        conversation_id: str,
        question: str,
        *,
        on_delta: AnswerDeltaCallback | None = None,
        is_cancelled: AnswerCancellationCallback | None = None,
        on_model_event: Callable[[object], None] | None = None,
    ) -> dict[str, object]:
        normalized = question.strip()
        if not normalized:
            raise DesktopConversationError("conversation_question_required", "Enter a question.")
        user_message_id, assistant_message_id, context = self._begin_turn(
            conversation_id, normalized
        )

        def emit(_answer_id: str, delta: str, replace: bool, attempt: int) -> None:
            if on_delta is not None:
                on_delta(assistant_message_id, delta, replace, attempt)

        try:
            answer = self._answers.generate(
                normalized,
                conversation_context=context,
                on_delta=emit,
                is_cancelled=is_cancelled,
                on_model_event=on_model_event,
            )
        except BaseException:
            self._mark_generation_failed(assistant_message_id)
            raise
        self._finish_initial_answer(assistant_message_id, answer)
        return self._get(conversation_id, select=False)

    def regenerate(
        self,
        conversation_id: str,
        assistant_message_id: str,
        *,
        on_delta: AnswerDeltaCallback | None = None,
        is_cancelled: AnswerCancellationCallback | None = None,
        on_model_event: Callable[[object], None] | None = None,
    ) -> dict[str, object]:
        question, context = self._begin_regeneration(conversation_id, assistant_message_id)

        def emit(_answer_id: str, delta: str, replace: bool, attempt: int) -> None:
            if on_delta is not None:
                on_delta(assistant_message_id, delta, replace, attempt)

        try:
            answer = self._answers.generate(
                question,
                conversation_context=context,
                on_delta=emit,
                is_cancelled=is_cancelled,
                on_model_event=on_model_event,
            )
        except BaseException:
            self._restore_selected_answer_status(assistant_message_id)
            raise
        if answer.status == "completed":
            self._append_answer_version(assistant_message_id, answer, select=True)
        else:
            self._restore_selected_answer_status(assistant_message_id)
        return self._get(conversation_id, select=False)

    def select_answer_version(
        self, conversation_id: str, assistant_message_id: str, answer_version_id: str
    ) -> dict[str, object]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT versions.status
                        FROM conversation_answer_versions versions
                        JOIN conversation_messages messages
                            ON messages.message_id = versions.assistant_message_id
                        WHERE versions.answer_version_id = ?
                            AND versions.assistant_message_id = ?
                            AND messages.conversation_id = ?
                        """,
                        (answer_version_id, assistant_message_id, conversation_id),
                    ).fetchone()
                    if row is None:
                        raise DesktopConversationError(
                            "answer_version_not_found", "The answer version was not found."
                        )
                    connection.execute(
                        """
                        UPDATE conversation_messages
                        SET selected_answer_version_id = ?, status = ?, updated_at = ?
                        WHERE message_id = ?
                        """,
                        (answer_version_id, str(row[0]), _timestamp(), assistant_message_id),
                    )
            finally:
                connection.close()
        return self.get(conversation_id)

    def _update_conversation(
        self, conversation_id: str, field: str, value: str, *, touch: bool = True
    ) -> None:
        if field not in {"title", "draft_text"}:
            raise AssertionError("unsupported conversation field")
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    sql = f"UPDATE conversations SET {field} = ?"  # noqa: S608 - closed field set
                    params: tuple[object, ...]
                    if touch:
                        sql += ", updated_at = ? WHERE conversation_id = ?"
                        params = (value, _timestamp(), conversation_id)
                    else:
                        sql += " WHERE conversation_id = ?"
                        params = (value, conversation_id)
                    if connection.execute(sql, params).rowcount != 1:
                        raise DesktopConversationError(
                            "conversation_not_found", "The conversation was not found."
                        )
            finally:
                connection.close()

    def _begin_turn(
        self, conversation_id: str, question: str
    ) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    row = _conversation_row(connection, conversation_id)
                    if _has_running_message(connection, conversation_id):
                        raise DesktopConversationError(
                            "conversation_answer_running",
                            "This conversation is already generating an answer.",
                        )
                    context = _conversation_context(
                        connection, conversation_id, before_ordinal=None
                    )
                    ordinal = int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(ordinal), -1) + 1
                            FROM conversation_messages WHERE conversation_id = ?
                            """,
                            (conversation_id,),
                        ).fetchone()[0]
                    )
                    user_message_id = uuid.uuid4().hex
                    assistant_message_id = uuid.uuid4().hex
                    now = _timestamp()
                    connection.execute(
                        """
                        INSERT INTO conversation_messages (
                            message_id, conversation_id, ordinal, role, content, status,
                            reply_to_message_id, selected_answer_version_id, created_at, updated_at
                        ) VALUES (?, ?, ?, 'user', ?, 'completed', NULL, NULL, ?, ?)
                        """,
                        (user_message_id, conversation_id, ordinal, question, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_messages (
                            message_id, conversation_id, ordinal, role, content, status,
                            reply_to_message_id, selected_answer_version_id, created_at, updated_at
                        ) VALUES (?, ?, ?, 'assistant', '', 'generating', ?, NULL, ?, ?)
                        """,
                        (
                            assistant_message_id,
                            conversation_id,
                            ordinal + 1,
                            user_message_id,
                            now,
                            now,
                        ),
                    )
                    title = str(row[1])
                    next_title = question[:60] if title == _DEFAULT_TITLE else title
                    connection.execute(
                        """
                        UPDATE conversations
                        SET title = ?, draft_text = '', updated_at = ?
                        WHERE conversation_id = ?
                        """,
                        (next_title, now, conversation_id),
                    )
                    _select_conversation_in(connection, conversation_id)
                    return user_message_id, assistant_message_id, context
            finally:
                connection.close()

    def _begin_regeneration(
        self, conversation_id: str, assistant_message_id: str
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    if _has_running_message(connection, conversation_id):
                        raise DesktopConversationError(
                            "conversation_answer_running",
                            "This conversation is already generating an answer.",
                        )
                    row = connection.execute(
                        """
                        SELECT assistant.ordinal, user.content
                        FROM conversation_messages assistant
                        JOIN conversation_messages user
                            ON user.message_id = assistant.reply_to_message_id
                        WHERE assistant.message_id = ? AND assistant.conversation_id = ?
                            AND assistant.role = 'assistant'
                            AND assistant.selected_answer_version_id IS NOT NULL
                        """,
                        (assistant_message_id, conversation_id),
                    ).fetchone()
                    if row is None:
                        raise DesktopConversationError(
                            "answer_regeneration_unavailable", "This answer cannot be regenerated."
                        )
                    connection.execute(
                        """
                        UPDATE conversation_messages
                        SET status = 'generating', updated_at = ? WHERE message_id = ?
                        """,
                        (_timestamp(), assistant_message_id),
                    )
                    return str(row[1]), _conversation_context(
                        connection, conversation_id, before_ordinal=int(row[0])
                    )
            finally:
                connection.close()

    def _finish_initial_answer(
        self, assistant_message_id: str, answer: DesktopGroundedAnswer
    ) -> None:
        self._append_answer_version(assistant_message_id, answer, select=True)

    def _append_answer_version(
        self, assistant_message_id: str, answer: DesktopGroundedAnswer, *, select: bool
    ) -> None:
        version_id = uuid.uuid4().hex
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT conversation_id FROM conversation_messages
                        WHERE message_id = ? AND role = 'assistant'
                        """,
                        (assistant_message_id,),
                    ).fetchone()
                    if row is None:
                        raise DesktopConversationError(
                            "conversation_message_not_found", "The assistant message was not found."
                        )
                    version_number = int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(version_number), 0) + 1
                            FROM conversation_answer_versions WHERE assistant_message_id = ?
                            """,
                            (assistant_message_id,),
                        ).fetchone()[0]
                    )
                    insert_answer_version(
                        connection,
                        version_id,
                        assistant_message_id,
                        version_number,
                        answer,
                        self._kb_dir,
                    )
                    if select:
                        connection.execute(
                            """
                            UPDATE conversation_messages
                            SET selected_answer_version_id = ?, status = ?, updated_at = ?
                            WHERE message_id = ?
                            """,
                            (version_id, answer.status, _timestamp(), assistant_message_id),
                        )
                    connection.execute(
                        "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                        (_timestamp(), str(row[0])),
                    )
            finally:
                connection.close()

    def _mark_generation_failed(self, assistant_message_id: str) -> None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE conversation_messages
                        SET status = 'interrupted', updated_at = ? WHERE message_id = ?
                        """,
                        (_timestamp(), assistant_message_id),
                    )
            finally:
                connection.close()

    def _restore_selected_answer_status(self, assistant_message_id: str) -> None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE conversation_messages
                        SET status = COALESCE((
                            SELECT status FROM conversation_answer_versions
                            WHERE answer_version_id =
                                conversation_messages.selected_answer_version_id
                        ), 'interrupted'), updated_at = ?
                        WHERE message_id = ?
                        """,
                        (_timestamp(), assistant_message_id),
                    )
            finally:
                connection.close()


def _conversation_payload(
    connection: sqlite3.Connection, row: tuple[object, ...], kb_dir: Path
) -> dict[str, object]:
    conversation_id = str(row[0])
    messages = []
    for message in connection.execute(
        """
        SELECT message_id, ordinal, role, content, status, selected_answer_version_id,
            created_at, updated_at
        FROM conversation_messages
        WHERE conversation_id = ? ORDER BY ordinal
        """,
        (conversation_id,),
    ).fetchall():
        versions = (
            _answer_versions(connection, str(message[0]), kb_dir)
            if str(message[2]) == "assistant"
            else []
        )
        messages.append(
            {
                "message_id": str(message[0]),
                "ordinal": int(message[1]),
                "role": str(message[2]),
                "content": str(message[3]),
                "status": str(message[4]),
                "selected_answer_version_id": (str(message[5]) if message[5] is not None else None),
                "created_at": str(message[6]),
                "updated_at": str(message[7]),
                "answer_versions": versions,
            }
        )
    return {
        "conversation_id": conversation_id,
        "title": str(row[1]),
        "draft_text": str(row[2]),
        "created_at": str(row[3]),
        "updated_at": str(row[4]),
        "messages": messages,
    }


def _answer_versions(
    connection: sqlite3.Connection, assistant_message_id: str, kb_dir: Path
) -> list[dict[str, object]]:
    versions = []
    for row in connection.execute(
        """
        SELECT answer_version_id, version_number, answer_text, retrieval_plan_json,
            degradations_json, status, interruption_code, interruption_reason, created_at
        FROM conversation_answer_versions
        WHERE assistant_message_id = ? ORDER BY version_number
        """,
        (assistant_message_id,),
    ).fetchall():
        version_id = str(row[0])
        citations = version_citations(connection, version_id)
        versions.append(
            {
                "answer_version_id": version_id,
                "version_number": int(row[1]),
                "answer_text": str(row[2]),
                "retrieval_plan": json_object(str(row[3])),
                "degradations": json_list(str(row[4])),
                "status": str(row[5]),
                "interruption_code": str(row[6]) if row[6] is not None else None,
                "interruption_reason": str(row[7]) if row[7] is not None else None,
                "created_at": str(row[8]),
                "citations": citations,
                "source_images": version_images(connection, version_id, kb_dir),
                "retrieval_trace": version_retrieval_trace(connection, version_id).as_dict(),
            }
        )
    return versions


def _conversation_context(
    connection: sqlite3.Connection,
    conversation_id: str,
    *,
    before_ordinal: int | None,
) -> tuple[tuple[str, str], ...]:
    before_clause = "AND assistant.ordinal < ?" if before_ordinal is not None else ""
    params: tuple[object, ...] = (
        (conversation_id, before_ordinal) if before_ordinal is not None else (conversation_id,)
    )
    rows = connection.execute(
        f"""
        SELECT user.content, versions.answer_text
        FROM conversation_messages assistant
        JOIN conversation_messages user ON user.message_id = assistant.reply_to_message_id
        JOIN conversation_answer_versions versions
            ON versions.answer_version_id = assistant.selected_answer_version_id
        WHERE assistant.conversation_id = ? AND assistant.role = 'assistant'
            AND assistant.status = 'completed' AND versions.status = 'completed'
            {before_clause}
        ORDER BY assistant.ordinal DESC LIMIT 4
        """,  # noqa: S608 - only the fixed optional predicate is interpolated
        params,
    ).fetchall()
    return tuple((str(row[0]), str(row[1])) for row in reversed(rows))


def recover_stale_conversation_generations(kb_dir: Path) -> None:
    with kb_ingest_lock(desktop_state_dir(kb_dir)):
        connection = _connect(desktop_state_database_path(kb_dir))
        try:
            _recover_generating_messages(connection)
        finally:
            connection.close()


def _recover_generating_messages(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """
            UPDATE conversation_messages
            SET status = CASE
                WHEN selected_answer_version_id IS NULL THEN 'interrupted'
                ELSE COALESCE((
                    SELECT status FROM conversation_answer_versions
                    WHERE answer_version_id = conversation_messages.selected_answer_version_id
                ), 'interrupted')
            END,
            updated_at = ?
            WHERE status = 'generating'
            """,
            (_timestamp(),),
        )


def _conversation_row(connection: sqlite3.Connection, conversation_id: str) -> tuple[object, ...]:
    row = connection.execute(
        """
        SELECT conversation_id, title, draft_text, created_at, updated_at
        FROM conversations WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise DesktopConversationError("conversation_not_found", "The conversation was not found.")
    return row


def _has_running_message(connection: sqlite3.Connection, conversation_id: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM conversation_messages
            WHERE conversation_id = ? AND status = 'generating'
            """,
            (conversation_id,),
        ).fetchone()
        is not None
    )


def _select_conversation_in(connection: sqlite3.Connection, conversation_id: str) -> None:
    connection.execute(
        "UPDATE conversation_ui_state SET last_conversation_id = ? WHERE singleton = 1",
        (conversation_id,),
    )


def _summary(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "conversation_id": str(row[0]),
        "title": str(row[1]),
        "draft_text": str(row[2]),
        "created_at": str(row[3]),
        "updated_at": str(row[4]),
        "generating": bool(row[5]),
    }


def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DesktopConversationError(
            "desktop_knowledge_base_not_found", "Open a Desktop Knowledge Base first."
        )
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
