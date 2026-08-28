"""Evidence-anchored local graph extraction and bounded graph retrieval.

The graph is an optional retrieval aid.  It never becomes a source of truth on
its own: every node and edge keeps the EvidenceRef that produced it, and every
query result is resolved back to an available evidence occurrence before it can
reach an answer.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_knowledge_graph_contract import validate_knowledge_graph_response
from openkb.desktop_knowledge_graph_store import (
    GraphEdge as _GraphEdge,
)
from openkb.desktop_knowledge_graph_store import (
    GraphNode as _GraphNode,
)
from openkb.desktop_knowledge_graph_store import (
    GraphPayload as _GraphPayload,
)
from openkb.desktop_knowledge_graph_store import (
    persist_graph_payload_in,
)
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
)
from openkb.desktop_model_result_failure import (
    DesktopModelOperationSuspendedError,
    authorize_model_operation_retry,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_model_operation_contract,
    suspend_structured_model_operation,
)
from openkb.desktop_prompt_contracts import prompt_contract_for
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
_MAX_CLAIM_CHARS = 900
_MAX_GRAPH_HOPS = 2
_MAX_GRAPH_ROOTS = 12
_MAX_GRAPH_EXPANDED_NODES = 32
_MAX_GRAPH_CANDIDATES = 8
_GRAPH_QUERY_BUDGET_SECONDS = 0.075
_ENTITY_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*){0,2}\b")
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u3400-\u9fff]{2,}")
_NON_ENTITY_WORDS = frozenset(("The", "This", "That", "These", "Those", "Document"))
CancellationCallback = Callable[[], bool]
ModelEventCallback = Callable[[object], None]
FailureCallback = Callable[[str, str], None]
PublishOperation = Callable[[sqlite3.Connection], bool]
PublishTransaction = Callable[[PublishOperation], bool]


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

        model_output: DesktopValidatedStructuredOutput[_GraphPayload] | None = None
        try:
            if self._model_gateway is not None:
                model_output = self._model_payload(
                    evidence,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    retry_scope=retry_scope,
                )
                payload = model_output.value
            else:
                payload = _deterministic_payload(evidence)
        except DesktopModelCancelledError:
            return False
        except DesktopModelCallError as error:
            if self._model_gateway is not None:
                suspend_analysis_operation_failure(self._kb_dir, self._model_gateway, error)
            _report_failure(on_failure, error.failure.code, error.failure.reason)
            self._record_diagnostic("extraction", error.failure.code, document_id)
            return False
        except DesktopModelOperationSuspendedError:
            _report_failure(on_failure, "model_operation_suspended")
            return False
        except DesktopStructuredOutputInvalidError as error:
            if self._model_gateway is not None:
                suspend_structured_model_operation(
                    self._kb_dir,
                    self._model_gateway,
                    error,
                    operation="knowledge_graph_extraction",
                    failure_code="knowledge_graph_response_invalid",
                    reason=(
                        "Knowledge Graph response did not satisfy its evidence-bound contract."
                    ),
                )
            _report_failure(on_failure, "knowledge_graph_response_invalid")
            self._record_diagnostic("extraction", "knowledge_graph_response_invalid", document_id)
            return False
        except (ValueError, json.JSONDecodeError):
            if self._model_gateway is not None:
                suspend_model_operation_contract(
                    self._kb_dir,
                    self._model_gateway,
                    operation="knowledge_graph_extraction",
                    failure_code="knowledge_graph_response_invalid",
                    reason=(
                        "Knowledge Graph response did not satisfy its evidence-bound contract."
                    ),
                )
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
            )
        try:
            return self._persist(
                document_id,
                payload,
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
    ) -> DesktopValidatedStructuredOutput[_GraphPayload]:
        if self._model_gateway is None:
            raise ValueError("Knowledge graph model is unavailable.")
        gateway = self._model_gateway
        source_material, prompt_evidence = _model_input(evidence)

        def invoke(request: DesktopModelRequest):
            if retry_scope is not None:
                authorize_model_operation_retry(
                    self._kb_dir,
                    gateway,
                    operation=request.operation,
                    retry_scope=retry_scope,
                    capability_identity=request.capability_identity,
                    prompt_contract_digest=request.prompt_contract_digest,
                )
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
            validate=lambda content: _model_payload_from_text(content, prompt_evidence),
        )

    def _persist(
        self,
        document_id: str,
        payload: _GraphPayload,
        *,
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
    query_budget_seconds: float = _GRAPH_QUERY_BUDGET_SECONDS,
    deadline: float | None = None,
) -> tuple[str, ...]:
    """Return a bounded 1–2-hop evidence set without merging same-name nodes."""
    normalized_terms = tuple(
        value for value in (_normalized_label(term) for term in terms) if value
    )
    anchors = tuple(dict.fromkeys(anchor_evidence_ids))[:_MAX_GRAPH_ROOTS]
    if not normalized_terms and not anchors:
        return ()
    deadline = deadline if deadline is not None else graph_query_deadline(query_budget_seconds)

    conditions: list[str] = []
    parameters: list[object] = []
    if anchors:
        conditions.append(f"evidence_id IN ({_placeholders(anchors)})")
        parameters.extend(anchors)
    for term in normalized_terms:
        conditions.append("instr(normalized_label, ?) > 0")
        parameters.append(term)
    root_rows = bounded_graph_rows(
        connection,
        f"""
        SELECT node_id, evidence_id, normalized_label
        FROM current_knowledge_graph_nodes
        WHERE {" OR ".join(conditions)}
        ORDER BY evidence_id, node_id
        LIMIT ?
        """,
        (*parameters, _MAX_GRAPH_ROOTS),
        deadline,
    )
    if not root_rows:
        return ()

    root_ids = [str(row[0]) for row in root_rows]
    evidence_ids: list[str] = []
    _append_unique(evidence_ids, (str(row[1]) for row in root_rows))
    labels = tuple(dict.fromkeys(str(row[2]) for row in root_rows))
    if labels:
        equivalent_rows = bounded_graph_rows(
            connection,
            f"""
            SELECT node_id, evidence_id
            FROM current_knowledge_graph_nodes
            WHERE normalized_label IN ({_placeholders(labels)})
            ORDER BY evidence_id, node_id
            LIMIT ?
            """,
            (*labels, _MAX_GRAPH_ROOTS),
            deadline,
        )
        _append_unique(root_ids, (str(row[0]) for row in equivalent_rows))
        _append_unique(evidence_ids, (str(row[1]) for row in equivalent_rows))

    root_ids = root_ids[:_MAX_GRAPH_EXPANDED_NODES]
    seen_nodes = set(root_ids)
    frontier = list(root_ids)
    for _hop in range(_MAX_GRAPH_HOPS):
        if not frontier or len(seen_nodes) >= _MAX_GRAPH_EXPANDED_NODES:
            break
        edge_rows = bounded_graph_rows(
            connection,
            f"""
            SELECT evidence_id, source_node_id, target_node_id
            FROM current_knowledge_graph_edges
            WHERE source_node_id IN ({_placeholders(tuple(frontier))})
                OR target_node_id IN ({_placeholders(tuple(frontier))})
            ORDER BY edge_id
            LIMIT ?
            """,
            (*frontier, *frontier, _MAX_GRAPH_EXPANDED_NODES - len(seen_nodes)),
            deadline,
        )
        _append_unique(evidence_ids, (str(row[0]) for row in edge_rows))
        next_ids: list[str] = []
        for _evidence_id, source_id, target_id in edge_rows:
            for node_id in (str(source_id), str(target_id)):
                if node_id not in seen_nodes:
                    seen_nodes.add(node_id)
                    next_ids.append(node_id)
                    if len(seen_nodes) == _MAX_GRAPH_EXPANDED_NODES:
                        break
            if len(seen_nodes) == _MAX_GRAPH_EXPANDED_NODES:
                break
        if not next_ids:
            break
        node_rows = bounded_graph_rows(
            connection,
            f"""
            SELECT node_id, evidence_id
            FROM current_knowledge_graph_nodes
            WHERE node_id IN ({_placeholders(tuple(next_ids))})
            ORDER BY node_id
            """,
            tuple(next_ids),
            deadline,
        )
        _append_unique(evidence_ids, (str(row[1]) for row in node_rows))
        frontier = next_ids
    return tuple(evidence_ids[:_MAX_GRAPH_CANDIDATES])


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
        included.append(item)
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
    return validate_knowledge_graph_response(
        content,
        known_evidence_ids=(item.evidence_id for item in evidence),
    )


def _deterministic_payload(evidence: tuple[_EvidenceInput, ...]) -> _GraphPayload:
    nodes: list[_GraphNode] = []
    edges: list[_GraphEdge] = []
    for ordinal, item in enumerate(evidence):
        claim_id = f"deterministic-claim-{ordinal}"
        concept_id = f"deterministic-concept-{ordinal}"
        nodes.extend(
            (
                _GraphNode(
                    claim_id,
                    item.evidence_id,
                    "claim",
                    _claim_label(item.text),
                    "deterministic",
                ),
                _GraphNode(
                    concept_id,
                    item.evidence_id,
                    "concept",
                    item.section or "Document",
                    "deterministic",
                ),
            )
        )
        edges.append(
            _GraphEdge(
                item.evidence_id,
                concept_id,
                claim_id,
                "SUPPORTS",
                0.8,
                "deterministic",
            )
        )
        for entity_ordinal, label in enumerate(_entity_labels(item.text), start=1):
            entity_id = f"deterministic-entity-{ordinal}-{entity_ordinal}"
            nodes.append(_GraphNode(entity_id, item.evidence_id, "entity", label, "deterministic"))
            edges.extend(
                (
                    _GraphEdge(
                        item.evidence_id,
                        entity_id,
                        concept_id,
                        "RELATED_TO",
                        0.7,
                        "deterministic",
                    ),
                    _GraphEdge(
                        item.evidence_id,
                        entity_id,
                        claim_id,
                        "SUPPORTS",
                        0.7,
                        "deterministic",
                    ),
                )
            )
    return _GraphPayload(nodes=tuple(nodes), edges=tuple(edges))


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


def _section_label(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ""
    return " / ".join(item.strip() for item in parsed if item.strip())[:_MAX_LABEL_CHARS]


def _entity_labels(text: str) -> tuple[str, ...]:
    labels: list[str] = []
    for match in _ENTITY_PATTERN.finditer(text):
        label = match.group(0).strip()
        if label not in _NON_ENTITY_WORDS:
            _append_unique(labels, (label,))
        if len(labels) == 2:
            return tuple(labels)
    for match in _WORD_PATTERN.finditer(text):
        label = match.group(0).strip()
        if label.casefold() not in {"the", "this", "that", "document"}:
            _append_unique(labels, (label,))
        if len(labels) == 2:
            break
    return tuple(labels)


def _claim_label(text: str) -> str:
    normalized = " ".join(text.split())
    return normalized[:_MAX_CLAIM_CHARS] or "Evidence claim"


def _normalized_label(value: str) -> str:
    return " ".join(value.split()).casefold()[:_MAX_LABEL_CHARS]


def _append_unique(values: list[str], incoming: Iterable[str]) -> None:
    for value in incoming:
        if value not in values:
            values.append(value)


def _placeholders(values: tuple[object, ...]) -> str:
    if not values:
        raise ValueError("SQLite placeholders require at least one value.")
    return ", ".join("?" for _ in values)


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _is_cancelled(callback: CancellationCallback | None) -> bool:
    return callback is not None and callback()


def _report_failure(callback: FailureCallback | None, code: str, reason: str | None = None) -> None:
    if callback is not None:
        callback(code, reason or code.replace("_", " ").capitalize() + ".")
