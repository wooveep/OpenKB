"""Durable, revision-checked corpus work coalesced across document imports."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from openkb.locks import kb_ingest_lock
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

CORPUS_WORK_QUEUE_MIGRATION_STATEMENTS = (
    "CREATE TABLE knowledge_corpus_work (document_id TEXT PRIMARY KEY "
    "REFERENCES source_documents(document_id) ON DELETE CASCADE, revision INTEGER NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('pending', 'failed')), error_code TEXT)",
)


def enqueue_corpus_work_in(connection: sqlite3.Connection, document_id: str) -> None:
    """Queue in the same transaction that publishes new candidates or relations."""
    connection.execute(
        "INSERT INTO knowledge_corpus_work (document_id, revision, status) "
        "VALUES (?, 1, 'pending') "
        "ON CONFLICT(document_id) DO UPDATE SET revision = revision + 1, "
        "status = 'pending', error_code = NULL",
        (document_id,),
    )


class CorpusWorkQueue:
    def __init__(self, kb_dir: Path):
        self._database_path = desktop_state_database_path(kb_dir)
        self._state_dir = desktop_state_dir(kb_dir)

    def pending(self) -> dict[str, int]:
        with closing(connect_database(self._database_path)) as connection:
            return dict(
                connection.execute(
                    "SELECT document_id, revision FROM knowledge_corpus_work "
                    "WHERE status = 'pending' ORDER BY document_id"
                ).fetchall()
            )

    def finish(self, revisions: dict[str, int], *, error_code: str | None = None) -> None:
        """A late completion must never consume a more recent document change."""
        with kb_ingest_lock(self._state_dir), closing(connect_database(self._database_path)) as db:
            with db:
                for document_id, revision in revisions.items():
                    if error_code is None:
                        db.execute(
                            "DELETE FROM knowledge_corpus_work "
                            "WHERE document_id = ? AND revision = ?",
                            (document_id, revision),
                        )
                    else:
                        db.execute(
                            "UPDATE knowledge_corpus_work SET status = 'failed', "
                            "error_code = ? WHERE document_id = ? AND revision = ?",
                            (error_code, document_id, revision),
                        )
