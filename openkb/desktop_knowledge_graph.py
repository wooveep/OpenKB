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
    gateway_analysis_capability_verified,
    invalidate_analysis_capability,
)
from openkb.desktop_model_result_failure import invalidate_structured_model_result
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock, try_kb_ingest_lock

_NODE_TYPES = frozenset(("entity", "concept", "claim"))
_EDGE_TYPES = frozenset(
    (
        "IS_A",
        "PART_OF",
        "RELATED_TO",
        "DEPENDS_ON",
        "USES",
        "PRODUCES",
        "LOCATED_IN",
        "CREATED_BY",
        "PRECEDES",
        "REPLACES",
        "SUPPORTS",
        "CONTRADICTS",
    )
)
_QUERY_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS = 0.05
_MAX_EXTRACTION_EVIDENCE = 12
_MAX_MODEL_EVIDENCE_CHARS = 1_200
_MAX_MODEL_INPUT_CHARS = 12_000
_MAX_NODES_PER_EVIDENCE = 12
_MAX_EDGES_PER_EVIDENCE = 16
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

        try:
            payload = (
                self._model_payload(
                    evidence,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                )
                if self._model_gateway is not None
                else _deterministic_payload(evidence)
            )
        except DesktopModelCancelledError:
            return False
        except DesktopModelCallError as error:
            if self._model_gateway is not None:
                invalidate_analysis_capability(
                    self._model_gateway,
                    error.failure.code,
                    error.failure.reason,
                )
            _report_failure(on_failure, error.failure.code, error.failure.reason)
            self._record_diagnostic("extraction", error.failure.code, document_id)
            return False
        except DesktopStructuredOutputInvalidError as error:
            if self._model_gateway is not None:
                invalidate_structured_model_result(self._model_gateway, error)
            _report_failure(on_failure, "knowledge_graph_response_invalid")
            self._record_diagnostic("extraction", "knowledge_graph_response_invalid", document_id)
            return False
        except (ValueError, json.JSONDecodeError):
            _report_failure(on_failure, "knowledge_graph_response_invalid")
            self._record_diagnostic("extraction", "knowledge_graph_response_invalid", document_id)
            return False

        if _is_cancelled(is_cancelled):
            return False
        try:
            return self._persist(payload, publish_transaction=publish_transaction)
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
                    AND NOT EXISTS (
                        SELECT 1 FROM knowledge_graph_nodes
                        WHERE knowledge_graph_nodes.evidence_id = evidence_refs.evidence_id
                    )
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
    ) -> _GraphPayload:
        if self._model_gateway is None:
            raise ValueError("Knowledge graph model is unavailable.")
        gateway = self._model_gateway
        source_material = _model_input(evidence)
        output = run_structured_output(
            operation="knowledge_graph_extraction",
            document_name=evidence[0].document_name,
            source_material=source_material,
            invoke=lambda request: gateway.analyze(
                request,
                on_event=on_model_event or (lambda _event: None),
                is_cancelled=is_cancelled,
            ),
            validate=lambda content: _model_payload_from_text(content, evidence),
        )
        return _fill_missing_evidence(output.value, evidence)

    def _persist(
        self,
        payload: _GraphPayload,
        *,
        publish_transaction: PublishTransaction | None = None,
    ) -> bool:
        if publish_transaction is not None:
            return publish_transaction(
                lambda connection: persist_graph_payload_in(connection, payload)
            )
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = persist_graph_payload_in(connection, payload)
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
        FROM knowledge_graph_nodes
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
            FROM knowledge_graph_nodes
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
            FROM knowledge_graph_edges
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
            FROM knowledge_graph_nodes
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


def _model_input(evidence: tuple[_EvidenceInput, ...]) -> str:
    remaining = _MAX_MODEL_INPUT_CHARS
    values: list[dict[str, str]] = []
    for item in evidence:
        if remaining <= 0:
            break
        text = item.text[: min(_MAX_MODEL_EVIDENCE_CHARS, remaining)]
        values.append(
            {
                "evidence_id": item.evidence_id,
                "section": item.section,
                "text": text,
            }
        )
        remaining -= len(text)
    return json.dumps({"evidence": values}, ensure_ascii=False)


def _model_payload_from_text(content: str, evidence: tuple[_EvidenceInput, ...]) -> _GraphPayload:
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict):
        raise ValueError("Knowledge graph payload must be an object.")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("Knowledge graph payload must include nodes and edges arrays.")
    evidence_ids = {item.evidence_id for item in evidence}
    nodes = _model_nodes(raw_nodes, evidence_ids)
    if not nodes:
        raise ValueError("Knowledge graph payload must include at least one node.")
    edges = _model_edges(raw_edges, nodes)
    return _GraphPayload(nodes=nodes, edges=edges)


def _model_nodes(values: list[object], evidence_ids: set[str]) -> tuple[_GraphNode, ...]:
    nodes: list[_GraphNode] = []
    identifiers: set[str] = set()
    by_evidence: dict[str, int] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Knowledge graph node must be an object.")
        local_id = _required_string(value, "id", max_chars=80)
        evidence_id = _required_string(value, "evidence_id", max_chars=80)
        node_type = _required_string(value, "type", max_chars=16).casefold()
        label = _required_string(value, "label", max_chars=_MAX_LABEL_CHARS)
        if (
            local_id in identifiers
            or evidence_id not in evidence_ids
            or node_type not in _NODE_TYPES
        ):
            raise ValueError("Knowledge graph node is invalid.")
        count = by_evidence.get(evidence_id, 0) + 1
        if count > _MAX_NODES_PER_EVIDENCE:
            raise ValueError("Knowledge graph node budget exceeded.")
        identifiers.add(local_id)
        by_evidence[evidence_id] = count
        nodes.append(_GraphNode(local_id, evidence_id, node_type, label, "model"))
    return tuple(nodes)


def _model_edges(values: list[object], nodes: tuple[_GraphNode, ...]) -> tuple[_GraphEdge, ...]:
    node_evidence = {node.local_id: node.evidence_id for node in nodes}
    edges: list[_GraphEdge] = []
    by_evidence: dict[str, int] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Knowledge graph edge must be an object.")
        evidence_id = _required_string(value, "evidence_id", max_chars=80)
        source = _required_string(value, "source_id", max_chars=80)
        target = _required_string(value, "target_id", max_chars=80)
        edge_type = _required_string(value, "type", max_chars=24).upper()
        if (
            source == target
            or edge_type not in _EDGE_TYPES
            or node_evidence.get(source) != evidence_id
            or node_evidence.get(target) != evidence_id
        ):
            raise ValueError("Knowledge graph edge is invalid.")
        count = by_evidence.get(evidence_id, 0) + 1
        if count > _MAX_EDGES_PER_EVIDENCE:
            raise ValueError("Knowledge graph edge budget exceeded.")
        by_evidence[evidence_id] = count
        edges.append(_GraphEdge(evidence_id, source, target, edge_type, 0.75, "model"))
    return tuple(edges)


def _fill_missing_evidence(
    payload: _GraphPayload, evidence: tuple[_EvidenceInput, ...]
) -> _GraphPayload:
    populated = {node.evidence_id for node in payload.nodes}
    missing = tuple(item for item in evidence if item.evidence_id not in populated)
    if not missing:
        return payload
    fallback = _deterministic_payload(missing)
    return _GraphPayload(
        nodes=(*payload.nodes, *fallback.nodes),
        edges=(*payload.edges, *fallback.edges),
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


def _required_string(value: dict[object, object], key: str, *, max_chars: int) -> str:
    candidate = value.get(key)
    if (
        not isinstance(candidate, str)
        or not (normalized := candidate.strip())
        or len(normalized) > max_chars
    ):
        raise ValueError(f"Knowledge graph {key} is invalid.")
    return normalized


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


def _json_object_text(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        normalized = normalized.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return normalized


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
