"""Model-assisted routing over immutable Document PageTrees."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_answer_types import (
    DesktopEvidenceRef,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
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
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_model_operation_contract,
    suspend_structured_model_operation,
)
from openkb.desktop_page_tree import PageTreeGeneration
from openkb.desktop_page_tree_store import lease_current_page_tree
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.desktop_workspace import desktop_state_database_path

_MAX_TREES = 3
_MAX_NODES_PER_TREE = 96
_MAX_SELECTED_NODES = 12
_MAX_SELECTED_EVIDENCE = 24
_MAX_SUMMARY_CHARS = 320
_MULTI_HOP = re.compile(
    r"\b(compare|relationship|relation|across|between|both|difference|affect)\b"
    r"|比较|对比|关系|联系|两者|之间|区别|跨",
    re.IGNORECASE,
)
_CONFLICT = re.compile(
    r"\b(conflict|contradiction|contradictory|inconsistent|disagree)\b"
    r"|冲突|矛盾|不一致|相互否定",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)
PageTreeLeaseFactory = Callable[[Path, str], AbstractContextManager[PageTreeGeneration | None]]


@dataclass(frozen=True)
class PageTreeSelectionResult:
    """PageTree routing output; evidence IDs still require Available-source resolution."""

    evidence_ids: tuple[str, ...] = ()
    generation_ids: tuple[str, ...] = ()
    selected_node_ids: tuple[str, ...] = ()
    trigger_reasons: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    model_cost: DesktopRetrievalModelCost = DesktopRetrievalModelCost()


def select_page_tree_evidence(
    kb_dir: Path,
    question: str,
    plan: DesktopRetrievalPlan,
    baseline: tuple[DesktopEvidenceRef, ...],
    model_gateway: DesktopModelGateway | None,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    lease_tree: PageTreeLeaseFactory = lease_current_page_tree,
    retry_scope: str | None = None,
) -> PageTreeSelectionResult:
    """Call PageTree Selection at most once, then return only bound Evidence identities."""
    try:
        document_ids = _candidate_document_ids(kb_dir, plan.terms, baseline)
        if not document_ids:
            return PageTreeSelectionResult()
        with ExitStack() as stack:
            trees = tuple(
                tree
                for document_id in document_ids
                if (tree := stack.enter_context(lease_tree(kb_dir, document_id))) is not None
            )
            if not trees:
                return PageTreeSelectionResult()
            triggers = _trigger_reasons(question, plan.terms, baseline, trees)
            if not triggers:
                return PageTreeSelectionResult()
            generation_ids = tuple(tree.generation_id for tree in trees)
            if model_gateway is None:
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_unavailable",),
                )
            if not gateway_analysis_capability_verified(model_gateway):
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_unverified",),
                )
            if not model_operation_dispatch_possible(
                kb_dir,
                model_gateway,
                operation="page_tree_selection",
                retry_scope=retry_scope,
            ):
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_suspended",),
                )
            prompt = _selection_prompt(question, trees)
            attempts = 0
            response_characters = 0

            try:

                def invoke(request: DesktopModelRequest):
                    nonlocal attempts, response_characters
                    require_model_operation_dispatch(
                        kb_dir,
                        model_gateway,
                        request,
                        retry_scope=retry_scope,
                    )
                    call_attempts = 0

                    def observe(event) -> None:
                        nonlocal call_attempts
                        if event.status in {
                            "connecting",
                            "awaiting_model_result",
                            "model_output_activity",
                            "validating",
                        }:
                            call_attempts = max(call_attempts, event.attempt)
                        if on_model_event is not None:
                            on_model_event(event)

                    call = (
                        model_gateway.analyze_once
                        if request.operation == "page_tree_selection"
                        else model_gateway.analyze
                    )
                    try:
                        result = call(
                            request,
                            on_event=observe,
                            is_cancelled=is_cancelled,
                        )
                    except BaseException:
                        attempts += call_attempts
                        raise
                    attempts += max(call_attempts, result.attempt_count)
                    response_characters += len(result.content)
                    return result

                output = run_structured_output(
                    operation="page_tree_selection",
                    document_name="Current Knowledge Base",
                    source_material=prompt,
                    invoke=invoke,
                    validate=lambda content: _selected_nodes(content, trees),
                )
                selected = output.value
                mark_structured_output_operations_ready(
                    kb_dir,
                    model_gateway,
                    output,
                    authority=DesktopModelOperationCompletionAuthority.for_retry_scope(
                        retry_scope
                    ),
                )
            except DesktopModelCancelledError:
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_cancelled",),
                    model_cost=_selection_cost(prompt, attempts),
                )
            except DesktopModelOperationSuspendedError:
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_suspended",),
                    model_cost=_selection_cost(prompt, attempts),
                )
            except DesktopModelCallError as error:
                suspend_analysis_operation_failure(kb_dir, model_gateway, error)
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_failed",),
                    model_cost=_selection_cost(prompt, attempts),
                )
            except DesktopStructuredOutputInvalidError as error:
                suspend_structured_model_operation(
                    kb_dir,
                    model_gateway,
                    error,
                    operation="page_tree_selection",
                    failure_code="model_response_invalid",
                    reason="The PageTree Selection response could not be validated.",
                )
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_invalid",),
                    model_cost=_selection_cost(prompt, attempts, response_characters),
                )
            except (ValueError, json.JSONDecodeError):
                suspend_model_operation_contract(
                    kb_dir,
                    model_gateway,
                    operation="page_tree_selection",
                    failure_code="model_response_invalid",
                    reason="The PageTree Selection response could not be validated.",
                )
                return PageTreeSelectionResult(
                    generation_ids=generation_ids,
                    trigger_reasons=triggers,
                    degradation_reasons=("page_tree_selection_invalid",),
                    model_cost=_selection_cost(prompt, attempts, response_characters),
                )
            return PageTreeSelectionResult(
                evidence_ids=_selected_evidence_ids(trees, selected),
                generation_ids=generation_ids,
                selected_node_ids=tuple(
                    node_id for _document_id, node_ids in selected for node_id in node_ids
                ),
                trigger_reasons=triggers,
                model_cost=_selection_cost(prompt, attempts, response_characters),
            )
    except (OSError, sqlite3.Error, ValueError):
        logger.warning(
            "Document PageTree selection failed; using baseline retrieval.", exc_info=True
        )
        return PageTreeSelectionResult(degradation_reasons=("page_tree_query_failed",))


def _selection_cost(
    prompt: str, attempts: int, response_characters: int = 0
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=attempts,
        input_characters=len(prompt) * attempts,
        output_characters=response_characters,
    )


def _candidate_document_ids(
    kb_dir: Path,
    terms: tuple[str, ...],
    baseline: tuple[DesktopEvidenceRef, ...],
) -> tuple[str, ...]:
    selected = list(dict.fromkeys(reference.document_id for reference in baseline))[:_MAX_TREES]
    if len(selected) == _MAX_TREES or not terms:
        return tuple(selected)
    connection = sqlite3.connect(desktop_state_database_path(kb_dir))
    try:
        score_parts: list[str] = []
        parameters: list[object] = []
        for term in terms:
            score_parts.append(
                "SUM(CASE WHEN instr(lower(nodes.title || ' ' || "
                "COALESCE(nodes.summary, '')), ?) > 0 THEN 1 ELSE 0 END)"
            )
            parameters.append(term)
        rows = connection.execute(
            f"""
            SELECT current.document_id, ({" + ".join(score_parts)}) AS lexical_score
            FROM document_page_tree_current AS current
            JOIN document_page_tree_nodes AS nodes
                ON nodes.generation_id = current.generation_id
            JOIN source_documents AS documents ON documents.document_id = current.document_id
            WHERE documents.availability = 'available'
            GROUP BY current.document_id
            HAVING lexical_score > 0
            ORDER BY lexical_score DESC, current.document_id
            LIMIT ?
            """,
            (*parameters, _MAX_TREES),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        document_id = str(row[0])
        if document_id not in selected:
            selected.append(document_id)
        if len(selected) == _MAX_TREES:
            break
    return tuple(selected)


def _trigger_reasons(
    question: str,
    terms: tuple[str, ...],
    baseline: tuple[DesktopEvidenceRef, ...],
    trees: tuple[PageTreeGeneration, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if any(len(tree.nodes) >= 80 for tree in trees):
        reasons.append("long_document")
    if any(
        len(tree.nodes) >= 40 or max((node.depth for node in tree.nodes), default=0) >= 4
        for tree in trees
    ):
        reasons.append("structurally_complex")
    if _MULTI_HOP.search(question):
        reasons.append("multi_hop")
    if _CONFLICT.search(question):
        reasons.append("conflicting")
    document_count = len({reference.document_id for reference in baseline})
    if 0 < len(terms) <= 2 and document_count >= 2:
        reasons.append("ambiguous")
    searchable = " ".join(
        f"{reference.document_name} {reference.section} {reference.excerpt}".casefold()
        for reference in baseline
    )
    covered = sum(1 for term in terms if term in searchable)
    if len(terms) >= 4 and (not terms or covered / len(terms) < 0.34):
        reasons.append("low_coverage")
    return tuple(reasons)


def _selection_prompt(question: str, trees: tuple[PageTreeGeneration, ...]) -> str:
    payload = {
        "question": question,
        "instructions": (
            "Select only nodes that route to original evidence needed to answer the question. "
            'Return JSON only: {"selections":[{"document_id":"...",'
            '"node_ids":["..."]}]}. Do not invent identifiers.'
        ),
        "trees": [
            {
                "document_id": tree.document_version_id,
                "generation_id": tree.generation_id,
                "nodes": [
                    {
                        "node_id": node.node_id,
                        "parent_node_id": node.parent_node_id,
                        "depth": node.depth,
                        "kind": node.kind,
                        "title": node.title,
                        "summary": (node.summary or "")[:_MAX_SUMMARY_CHARS],
                    }
                    for node in tree.nodes[:_MAX_NODES_PER_TREE]
                ],
            }
            for tree in trees[:_MAX_TREES]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _selected_nodes(
    content: str, trees: tuple[PageTreeGeneration, ...]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict) or set(payload) != {"selections"}:
        raise ValueError("PageTree Selection response is invalid.")
    selections = payload.get("selections")
    if not isinstance(selections, list) or len(selections) > _MAX_TREES:
        raise ValueError("PageTree Selection response is invalid.")
    nodes_by_document = {
        tree.document_version_id: {node.node_id for node in tree.nodes[:_MAX_NODES_PER_TREE]}
        for tree in trees
    }
    selected: list[tuple[str, tuple[str, ...]]] = []
    selected_documents: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"document_id", "node_ids"}:
            raise ValueError("PageTree Selection response is invalid.")
        document_id = selection.get("document_id")
        node_ids = selection.get("node_ids")
        if (
            not isinstance(document_id, str)
            or document_id in selected_documents
            or document_id not in nodes_by_document
            or not isinstance(node_ids, list)
            or len(node_ids) > _MAX_SELECTED_NODES
            or not all(isinstance(node_id, str) for node_id in node_ids)
        ):
            raise ValueError("PageTree Selection response is invalid.")
        unique_node_ids = tuple(dict.fromkeys(node_ids))
        if any(node_id not in nodes_by_document[document_id] for node_id in unique_node_ids):
            raise ValueError("PageTree Selection response is invalid.")
        selected_documents.add(document_id)
        selected.append((document_id, unique_node_ids))
    return tuple(selected)


def _selected_evidence_ids(
    trees: tuple[PageTreeGeneration, ...],
    selections: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[str, ...]:
    trees_by_document = {tree.document_version_id: tree for tree in trees}
    evidence_ids: list[str] = []
    for document_id, node_ids in selections:
        tree = trees_by_document[document_id]
        selected = set(node_ids)
        children_by_parent: dict[str, list[str]] = {}
        for node in tree.nodes:
            if node.parent_node_id is not None:
                children_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)
        descendants: set[str] = set()
        pending = list(selected)
        while pending:
            node_id = pending.pop()
            if node_id in descendants:
                continue
            descendants.add(node_id)
            pending.extend(children_by_parent.get(node_id, ()))
        for node in tree.nodes:
            if node.node_id not in descendants:
                continue
            for binding in node.evidence:
                if binding.evidence_id not in evidence_ids:
                    evidence_ids.append(binding.evidence_id)
                if len(evidence_ids) == _MAX_SELECTED_EVIDENCE:
                    return tuple(evidence_ids)
    return tuple(evidence_ids)


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped
