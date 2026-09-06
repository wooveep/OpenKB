"""Optional evidence-bound semantic relation extraction and retrieval."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.knowledge.graph.semantic_graph_retrieval import semantic_graph_evidence_ids_in
from openkb.knowledge.graph.semantic_graph_service import DesktopSemanticGraphService
from openkb.locks import kb_ingest_lock, try_kb_ingest_lock
from openkb.models.gateway import (
    DesktopModelGateway,
    gateway_analysis_capability_verified,
)
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

_QUERY_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS = 0.05
_GRAPH_QUERY_BUDGET_SECONDS = 0.075

CancellationCallback = Callable[[], bool]
ModelEventCallback = Callable[[object], None]
FailureCallback = Callable[[str, str], None]
PublishOperation = Callable[[sqlite3.Connection], bool]
PublishTransaction = Callable[[PublishOperation], bool]


@dataclass(frozen=True)
class PinnedGraphGenerations:
    """Semantic relation authority captured by one immutable navigation snapshot."""

    semantic_generation_id: int | None


class DesktopKnowledgeGraphQueryError(RuntimeError):
    """A safe graph-query failure that must not interrupt baseline retrieval."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DesktopKnowledgeGraphService:
    """Run the sole current-epoch, model-owned relation analysis path."""

    def __init__(self, kb_dir: Path, *, model_gateway: DesktopModelGateway | None = None) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._model_gateway = model_gateway

    def extract_document(
        self,
        document_id: str,
        *,
        is_cancelled: CancellationCallback | None = None,
        on_model_event: ModelEventCallback | None = None,
        on_failure: FailureCallback | None = None,
        publish_transaction: PublishTransaction | None = None,
        retry_scope: str | None = None,
    ) -> bool:
        """Best-effort relation analysis; unavailable semantics publish no graph."""
        if self._model_gateway is not None and not gateway_analysis_capability_verified(
            self._model_gateway
        ):
            return False
        return bool(
            DesktopSemanticGraphService(
                self._kb_dir,
                model_gateway=self._model_gateway,
            ).extract_document_if_admitted(
                document_id,
                is_cancelled=is_cancelled,
                on_model_event=on_model_event,
                on_failure=on_failure,
                publish_transaction=publish_transaction,
                retry_scope=retry_scope,
            )
        )

    def record_query_diagnostic(self, error_code: str) -> None:
        """Record a query degradation without waiting behind a KB mutation."""
        self._record_diagnostic(
            "query",
            error_code,
            None,
            lock_timeout_seconds=_QUERY_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS,
        )

    def _record_diagnostic(
        self,
        phase: str,
        error_code: str,
        document_id: str | None,
        *,
        lock_timeout_seconds: float | None = None,
    ) -> None:
        """Persist an optional diagnostic under the owning KB mutation lock."""
        try:
            if lock_timeout_seconds is None:
                with kb_ingest_lock(self._state_dir):
                    self._persist_diagnostic(phase, error_code, document_id)
                return
            with try_kb_ingest_lock(
                self._state_dir, timeout_seconds=lock_timeout_seconds
            ) as acquired:
                if acquired:
                    self._persist_diagnostic(
                        phase,
                        error_code,
                        document_id,
                        database_timeout_seconds=0,
                    )
        except (OSError, sqlite3.Error):
            return

    def _persist_diagnostic(
        self,
        phase: str,
        error_code: str,
        document_id: str | None,
        *,
        database_timeout_seconds: float = 5,
    ) -> None:
        connection = self._connect(timeout_seconds=database_timeout_seconds)
        try:
            _insert_diagnostic(connection, phase, error_code, document_id)
            connection.commit()
        finally:
            connection.close()

    def _connect(self, *, timeout_seconds: float = 5) -> sqlite3.Connection:
        connection = connect_database(self._database_path, timeout=timeout_seconds)
        return connection


def start_graph_extraction(
    kb_dir: Path, document_id: str, *, model_gateway: DesktopModelGateway | None
) -> None:
    """Durably queue optional relation work; the Engine owns execution and cancellation."""
    if model_gateway is None:
        return
    from openkb.knowledge.graph.tasks import DesktopKnowledgeGraphExtractionTasks

    DesktopKnowledgeGraphExtractionTasks(kb_dir).queue(document_id, model_gateway)


def record_graph_extraction_diagnostic(kb_dir: Path, document_id: str) -> None:
    """Record a worker-launch failure without changing document availability."""
    DesktopKnowledgeGraphService(kb_dir)._record_diagnostic(
        "extraction", "knowledge_graph_extraction_failed", document_id
    )


def graph_query_deadline(
    query_budget_seconds: float = _GRAPH_QUERY_BUDGET_SECONDS,
) -> float:
    """Return the shared deadline for all SQLite work in one relation lookup."""
    if query_budget_seconds <= 0:
        raise DesktopKnowledgeGraphQueryError("knowledge_graph_query_timeout")
    return time.monotonic() + query_budget_seconds


def local_graph_evidence_ids(
    connection: sqlite3.Connection,
    *,
    terms: tuple[str, ...],
    anchor_evidence_ids: tuple[str, ...],
    allowed_document_ids: frozenset[str] | None = None,
    query_budget_seconds: float = _GRAPH_QUERY_BUDGET_SECONDS,
    deadline: float | None = None,
    generation_snapshot: PinnedGraphGenerations | None = None,
) -> tuple[str, ...]:
    """Return bounded relation Evidence, or nothing when semantic structure is unavailable."""
    normalized_terms = tuple(
        value for value in (_normalized_label(term) for term in terms) if value
    )
    anchors = tuple(dict.fromkeys(anchor_evidence_ids))
    if allowed_document_ids == frozenset() or (not normalized_terms and not anchors):
        return ()
    deadline = deadline if deadline is not None else graph_query_deadline(query_budget_seconds)
    semantic_evidence = semantic_graph_evidence_ids_in(
        connection,
        terms=normalized_terms,
        anchor_evidence_ids=anchors,
        allowed_document_ids=allowed_document_ids,
        generation_id=(
            generation_snapshot.semantic_generation_id if generation_snapshot is not None else None
        ),
        use_current_generation=generation_snapshot is None,
        fetch_rows=lambda statement, parameters: bounded_graph_rows(
            connection, statement, parameters, deadline
        ),
    )
    return semantic_evidence or ()


def record_query_diagnostic(kb_dir: Path, error_code: str) -> None:
    """Best-effort diagnostic write that cannot make a user wait for graph failure."""
    DesktopKnowledgeGraphService(kb_dir).record_query_diagnostic(error_code)


def bounded_graph_rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
    deadline: float,
) -> list[tuple[object, ...]]:
    if time.monotonic() >= deadline:
        raise DesktopKnowledgeGraphQueryError("knowledge_graph_query_timeout")
    timed_out = False

    def abort_if_expired() -> int:
        nonlocal timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    connection.set_progress_handler(abort_if_expired, 1_000)
    try:
        return connection.execute(statement, parameters).fetchall()
    except sqlite3.OperationalError as error:
        code = "knowledge_graph_query_timeout" if timed_out else "knowledge_graph_query_failed"
        raise DesktopKnowledgeGraphQueryError(code) from error
    except sqlite3.Error as error:
        raise DesktopKnowledgeGraphQueryError("knowledge_graph_query_failed") from error
    finally:
        connection.set_progress_handler(None, 0)


def _insert_diagnostic(
    connection: sqlite3.Connection, phase: str, error_code: str, document_id: str | None
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_graph_diagnostics (
            diagnostic_id, phase, error_code, document_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (uuid.uuid4().hex, phase, error_code, document_id, _timestamp()),
    )


def _normalized_label(value: str) -> str:
    return " ".join(value.split()).casefold()[:320]
