"""Evidence-anchored local graph extraction and bounded graph retrieval.

The graph is an optional retrieval aid.  It never becomes a source of truth on
its own: every node and edge keeps the EvidenceRef that produced it, and every
query result is resolved back to an available evidence occurrence before it can
reach an answer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_knowledge_graph_deterministic import deterministic_graph_payload
from openkb.desktop_knowledge_graph_interpretation import (
    GraphEvidence,
    GraphExtractionBoundary,
    GraphInterpretation,
    KnowledgeGraphInterpretationError,
)
from openkb.desktop_knowledge_graph_store import (
    GraphPayload as _GraphPayload,
)
from openkb.desktop_knowledge_graph_store import (
    persist_failed_graph_interpretation_in,
    persist_graph_interpretation_in,
    persist_graph_payload_in,
)
from openkb.desktop_legacy_graph_retrieval import legacy_graph_evidence_ids_in
from openkb.desktop_logging import log_event
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
)
from openkb.desktop_model_result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    record_structured_model_result_failure,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_model_operation_contract,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_semantic_graph_retrieval import semantic_graph_evidence_ids_in
from openkb.desktop_semantic_graph_service import DesktopSemanticGraphService
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
    run_structured_output,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock, try_kb_ingest_lock

_QUERY_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS = 0.05
_MAX_EXTRACTION_EVIDENCE = 12
_MAX_MODEL_EVIDENCE_CHARS = 1_200
_MAX_MODEL_INPUT_CHARS = 12_000
_MAX_LABEL_CHARS = 320
_GRAPH_QUERY_BUDGET_SECONDS = 0.075
_MAX_LOGGED_GRAPH_ISSUES = 32
_CONFIRMED_GRAPH_PROTOCOL_FAILURES = frozenset(
    {
        "model_authentication_failed",
        "model_configuration_invalid",
        "model_response_invalid",
    }
)
CancellationCallback = Callable[[], bool]


@dataclass(frozen=True)
class PinnedGraphGenerations:
    """Graph authorities captured by one immutable navigation snapshot."""

    semantic_generation_id: int | None
    legacy_result_ids: tuple[str, ...]


ModelEventCallback = Callable[[object], None]
FailureCallback = Callable[[str, str], None]
PublishOperation = Callable[[sqlite3.Connection], bool]
PublishTransaction = Callable[[PublishOperation], bool]
logger = logging.getLogger(__name__)


class DesktopKnowledgeGraphQueryError(RuntimeError):
    """A safe graph-query failure that must not interrupt baseline retrieval."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _EvidenceInput:
    evidence_id: str
    text: str
    document_name: str
    section: str


class DesktopKnowledgeGraphService:
    """Extract evidence-bound local graph records after a document is published."""

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
        """Best-effort extraction; a failure never changes the document's availability."""
        if self._model_gateway is not None and not gateway_analysis_capability_verified(
            self._model_gateway
        ):
            return False
        semantic_result = DesktopSemanticGraphService(
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
        if semantic_result is not None:
            return semantic_result
        try:
            evidence = self._unextracted_evidence(document_id)
        except (OSError, sqlite3.Error):
            _report_failure(on_failure, "knowledge_graph_extraction_failed")
            self._record_diagnostic("extraction", "knowledge_graph_extraction_failed", document_id)
            return False
        if not evidence or _is_cancelled(is_cancelled):
            return False
        if self._model_gateway is not None and not model_operation_dispatch_possible(
            self._kb_dir,
            self._model_gateway,
            operation="knowledge_graph_extraction",
            retry_scope=retry_scope,
        ):
            return False

        model_output: DesktopValidatedStructuredOutput[GraphInterpretation] | None = None
        interpretation: GraphInterpretation | None = None
        try:
            if self._model_gateway is not None:
                model_output = self._model_payload(
                    evidence,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    retry_scope=retry_scope,
                )
                interpretation = model_output.value
                _log_graph_interpretation(interpretation)
                if interpretation.payload is None:
                    raise KnowledgeGraphInterpretationError(interpretation)
                payload = interpretation.payload
            else:
                payload = deterministic_graph_payload(evidence)
        except DesktopModelCancelledError:
            return False
        except DesktopModelCallError as error:
            if (
                self._model_gateway is not None
                and error.failure.code in _CONFIRMED_GRAPH_PROTOCOL_FAILURES
            ):
                suspend_analysis_operation_failure(self._kb_dir, self._model_gateway, error)
            _report_failure(on_failure, error.failure.code, error.failure.reason)
            self._record_diagnostic("extraction", error.failure.code, document_id)
            return False
        except DesktopModelOperationSuspendedError:
            _report_failure(on_failure, "model_operation_suspended")
            return False
        except DesktopStructuredOutputInvalidError as error:
            if self._model_gateway is not None:
                record_structured_model_result_failure(self._model_gateway, error)
                failed_interpretation = _failed_interpretation(error)
                if failed_interpretation is not None:
                    _log_graph_interpretation(
                        failed_interpretation,
                        failure_event_id=error.failure_event_id,
                    )
                try:
                    corroboration_count = (
                        self._persist_failed_interpretation(document_id, failed_interpretation)
                        if failed_interpretation is not None
                        else 0
                    )
                except (OSError, sqlite3.Error):
                    corroboration_count = 0
                    self._record_diagnostic(
                        "extraction", "knowledge_graph_attempt_persistence_failed", document_id
                    )
                if corroboration_count >= 2 and failed_interpretation is not None:
                    suspend_model_operation_contract(
                        self._kb_dir,
                        self._model_gateway,
                        operation="knowledge_graph_extraction",
                        failure_code="knowledge_graph_response_invalid",
                        reason=(
                            "The same structural Knowledge Graph failure was confirmed "
                            "across independent documents."
                        ),
                        failure_signature=failed_interpretation.failure_signature,
                    )
            _report_failure(on_failure, "knowledge_graph_response_invalid")
            self._record_diagnostic("extraction", "knowledge_graph_response_invalid", document_id)
            return False
        except (ValueError, json.JSONDecodeError):
            _report_failure(on_failure, "knowledge_graph_response_invalid")
            self._record_diagnostic("extraction", "knowledge_graph_response_invalid", document_id)
            return False

        if _is_cancelled(is_cancelled):
            return False
        if self._model_gateway is not None and model_output is not None:
            mark_structured_output_operations_ready(
                self._kb_dir,
                self._model_gateway,
                model_output,
                authority=DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope),
            )
        try:
            return self._persist(
                document_id,
                payload,
                interpretation=interpretation,
                publish_transaction=publish_transaction,
            )
        except (OSError, sqlite3.Error):
            _report_failure(on_failure, "knowledge_graph_extraction_failed")
            self._record_diagnostic("extraction", "knowledge_graph_extraction_failed", document_id)
            return False

    def _unextracted_evidence(self, document_id: str) -> tuple[_EvidenceInput, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT evidence_refs.evidence_id, evidence_refs.text, source_documents.display_name,
                    document_ir_blocks.heading_path, MIN(evidence_occurrences.ordinal)
                FROM evidence_occurrences
                JOIN evidence_refs ON evidence_refs.evidence_id = evidence_occurrences.evidence_id
                JOIN source_documents
                    ON source_documents.document_id = evidence_occurrences.document_id
                JOIN document_ir_blocks
                    ON document_ir_blocks.block_id = evidence_occurrences.block_id
                WHERE evidence_occurrences.document_id = ?
                    AND source_documents.availability = 'available'
                GROUP BY evidence_refs.evidence_id, evidence_refs.text,
                    source_documents.display_name,
                    document_ir_blocks.heading_path
                ORDER BY MIN(evidence_occurrences.ordinal), evidence_refs.evidence_id
                LIMIT ?
                """,
                (document_id, _MAX_EXTRACTION_EVIDENCE),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            _EvidenceInput(
                evidence_id=str(row[0]),
                text=str(row[1]),
                document_name=str(row[2]),
                section=_section_label(str(row[3])),
            )
            for row in rows
        )

    def _model_payload(
        self,
        evidence: tuple[_EvidenceInput, ...],
        *,
        is_cancelled: CancellationCallback | None,
        on_model_event: ModelEventCallback | None,
        retry_scope: str | None,
    ) -> DesktopValidatedStructuredOutput[GraphInterpretation]:
        if self._model_gateway is None:
            raise ValueError("Knowledge graph model is unavailable.")
        gateway = self._model_gateway
        source_material, prompt_evidence = _model_input(evidence)

        def invoke(request: DesktopModelRequest):
            require_model_operation_dispatch(
                self._kb_dir,
                gateway,
                request,
                retry_scope=retry_scope,
            )
            return gateway.analyze(
                request,
                on_event=on_model_event or (lambda _event: None),
                is_cancelled=is_cancelled,
            )

        return run_structured_output(
            operation="knowledge_graph_extraction",
            document_name=evidence[0].document_name,
            source_material=source_material,
            invoke=invoke,
            validate=lambda content: _model_interpretation_from_text(content, prompt_evidence),
            should_repair=_knowledge_graph_repair_allowed,
        )

    def _persist_failed_interpretation(
        self,
        document_id: str,
        interpretation: GraphInterpretation,
    ) -> int:
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    return persist_failed_graph_interpretation_in(
                        connection,
                        document_id,
                        interpretation,
                        capability_identity=self._capability_identity(),
                        prompt_contract_digest=prompt_contract_for(
                            "knowledge_graph_extraction"
                        ).digest,
                    )
            finally:
                connection.close()

    def _persist(
        self,
        document_id: str,
        payload: _GraphPayload,
        *,
        interpretation: GraphInterpretation | None = None,
        publish_transaction: PublishTransaction | None = None,
    ) -> bool:
        capability_identity = self._capability_identity()
        prompt_digest = (
            prompt_contract_for("knowledge_graph_extraction").digest
            if self._model_gateway is not None
            else None
        )
        extraction_method = "model" if self._model_gateway is not None else "deterministic"

        def persist(connection: sqlite3.Connection) -> bool:
            if interpretation is not None:
                return persist_graph_interpretation_in(
                    connection,
                    document_id,
                    interpretation,
                    capability_identity=capability_identity,
                    prompt_contract_digest=prompt_digest,
                )
            return persist_graph_payload_in(
                connection,
                document_id,
                payload,
                capability_identity=capability_identity,
                prompt_contract_digest=prompt_digest,
                extraction_method=extraction_method,
            )

        if publish_transaction is not None:
            return publish_transaction(persist)
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = persist(connection)
                if not changed:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _capability_identity(self) -> str | None:
        if self._model_gateway is None:
            return None
        profile_factory = getattr(self._model_gateway, "execution_profile_for_operation", None)
        if not callable(profile_factory):
            return None
        try:
            profile = profile_factory("knowledge_graph_extraction")
        except (TypeError, ValueError):
            return None
        return str(getattr(profile, "capability_evidence_profile", profile).identity)

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
        """Persist one optional diagnostic under the owning KB mutation lock."""
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
            # Graph diagnostics are useful but never worth turning an optional
            # capability failure into a document or answer failure.
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
        connection = sqlite3.connect(self._database_path, timeout=timeout_seconds)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def start_graph_extraction(
    kb_dir: Path, document_id: str, *, model_gateway: DesktopModelGateway | None
) -> None:
    """Durably queue optional graph work; the Engine owns execution and cancellation."""
    if model_gateway is None:
        return
    from openkb.desktop_knowledge_graph_tasks import DesktopKnowledgeGraphExtractionTasks

    DesktopKnowledgeGraphExtractionTasks(kb_dir).queue(document_id, model_gateway)


def record_graph_extraction_diagnostic(kb_dir: Path, document_id: str) -> None:
    """Record a worker-launch failure without changing document availability."""
    DesktopKnowledgeGraphService(kb_dir)._record_diagnostic(
        "extraction", "knowledge_graph_extraction_failed", document_id
    )


def graph_query_deadline(
    query_budget_seconds: float = _GRAPH_QUERY_BUDGET_SECONDS,
) -> float:
    """Return the shared deadline for all SQLite work in one graph lookup."""
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
    """Return a bounded 1–2-hop evidence set without merging same-name nodes."""
    normalized_terms = tuple(
        value for value in (_normalized_label(term) for term in terms) if value
    )
    anchors = tuple(dict.fromkeys(anchor_evidence_ids))
    if allowed_document_ids == frozenset():
        return ()
    if not normalized_terms and not anchors:
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
    if semantic_evidence is not None:
        return semantic_evidence
    return legacy_graph_evidence_ids_in(
        terms=normalized_terms,
        anchor_evidence_ids=anchors,
        allowed_document_ids=allowed_document_ids,
        result_ids=(
            generation_snapshot.legacy_result_ids if generation_snapshot is not None else None
        ),
        fetch_rows=lambda statement, parameters: bounded_graph_rows(
            connection, statement, parameters, deadline
        ),
    )


def record_query_diagnostic(kb_dir: Path, error_code: str) -> None:
    """Best-effort diagnostic write that cannot make a user wait for a graph failure."""
    DesktopKnowledgeGraphService(kb_dir).record_query_diagnostic(error_code)


def _model_input(
    evidence: tuple[_EvidenceInput, ...],
) -> tuple[str, tuple[_EvidenceInput, ...]]:
    remaining = _MAX_MODEL_INPUT_CHARS
    values: list[dict[str, str]] = []
    included: list[_EvidenceInput] = []
    for item in evidence:
        if remaining <= 0:
            break
        text = item.text[: min(_MAX_MODEL_EVIDENCE_CHARS, remaining)]
        included.append(
            _EvidenceInput(
                evidence_id=item.evidence_id,
                text=text,
                document_name=item.document_name,
                section=item.section,
            )
        )
        values.append(
            {
                "evidence_id": item.evidence_id,
                "section": item.section,
                "text": text,
            }
        )
        remaining -= len(text)
    return json.dumps({"evidence": values}, ensure_ascii=False), tuple(included)


def _model_payload_from_text(content: str, evidence: tuple[_EvidenceInput, ...]) -> _GraphPayload:
    interpretation = _model_interpretation_from_text(content, evidence)
    if interpretation.payload is None:
        raise KnowledgeGraphInterpretationError(interpretation)
    return interpretation.payload


def _model_interpretation_from_text(
    content: str,
    evidence: tuple[_EvidenceInput, ...],
) -> GraphInterpretation:
    interpretation = GraphExtractionBoundary.interpret(
        content,
        tuple(GraphEvidence(item.evidence_id, item.text) for item in evidence),
    )
    if interpretation.lifecycle == "failed":
        raise KnowledgeGraphInterpretationError(interpretation)
    return interpretation


def _knowledge_graph_repair_allowed(error: Exception) -> bool:
    return isinstance(error, KnowledgeGraphInterpretationError) and error.interpretation.repairable


def _failed_interpretation(
    error: DesktopStructuredOutputInvalidError,
) -> GraphInterpretation | None:
    cause = error.__cause__
    if isinstance(cause, KnowledgeGraphInterpretationError):
        return cause.interpretation
    return None


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


def _log_graph_interpretation(
    interpretation: GraphInterpretation,
    *,
    failure_event_id: str | None = None,
) -> None:
    if not interpretation.issues:
        return
    logged_issues = interpretation.issues[:_MAX_LOGGED_GRAPH_ISSUES]
    log_event(
        logger,
        logging.WARNING if interpretation.lifecycle == "failed" else logging.INFO,
        "knowledge_graph_interpreted",
        "Knowledge Graph candidates were interpreted at the local publication boundary.",
        component="knowledge",
        fields={
            "failure_event_id": failure_event_id,
            "result_status": interpretation.lifecycle,
            "result_quality": interpretation.quality,
            "retained_count": interpretation.counts.retained,
            "weakened_count": interpretation.counts.weakened,
            "rejected_count": interpretation.counts.rejected,
            "issue_count": len(interpretation.issues),
            "issues_truncated": len(logged_issues) < len(interpretation.issues),
            "issue_codes": [issue.code for issue in logged_issues],
            "issue_paths": [issue.path for issue in logged_issues],
            "issue_dispositions": [issue.disposition for issue in logged_issues],
            "issue_failure_classes": [issue.failure_class for issue in logged_issues],
        },
        terminal=False,
    )


def _section_label(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ""
    return " / ".join(item.strip() for item in parsed if item.strip())[:_MAX_LABEL_CHARS]


def _normalized_label(value: str) -> str:
    return " ".join(value.split()).casefold()[:_MAX_LABEL_CHARS]


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _is_cancelled(callback: CancellationCallback | None) -> bool:
    return callback is not None and callback()


def _report_failure(callback: FailureCallback | None, code: str, reason: str | None = None) -> None:
    if callback is not None:
        callback(code, reason or code.replace("_", " ").capitalize() + ".")
