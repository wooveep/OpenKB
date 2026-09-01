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
from openkb.desktop_model_deadlines import request_with_response_deadline
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
from openkb.desktop_page_tree import PageTreeGeneration, PageTreeNode
from openkb.desktop_page_tree_store import lease_current_page_tree
from openkb.desktop_prompt_contracts import (
    PAGE_TREE_SELECTION_MAX_DOCUMENTS,
    PAGE_TREE_SELECTION_MAX_NODES_PER_DOCUMENT,
)
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.desktop_workspace import desktop_state_database_path

_MAX_TREES = PAGE_TREE_SELECTION_MAX_DOCUMENTS
_MAX_NODES_PER_TREE = 96
_MAX_SELECTED_NODES = PAGE_TREE_SELECTION_MAX_NODES_PER_DOCUMENT
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
    bounded_model_attempts: bool = False,
    response_deadline: float | None = None,
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
            prompt_trees = _prompt_trees(trees, plan.terms)
            prompt = _selection_prompt(question, prompt_trees)
            attempts = 0
            response_characters = 0

            try:

                def invoke(request: DesktopModelRequest):
                    nonlocal attempts, response_characters
                    request = request_with_response_deadline(request, response_deadline)
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
                        if bounded_model_attempts or request.operation == "page_tree_selection"
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
                    validate=lambda content: _selected_nodes(content, prompt_trees),
                )
                selected = output.value
                mark_structured_output_operations_ready(
                    kb_dir,
                    model_gateway,
                    output,
                    authority=DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope),
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


def _prompt_trees(
    trees: tuple[PageTreeGeneration, ...], terms: tuple[str, ...]
) -> tuple[tuple[PageTreeGeneration, tuple[PageTreeNode, ...]], ...]:
    return tuple((tree, _prompt_nodes(tree, terms)) for tree in trees[:_MAX_TREES])


def _prompt_nodes(tree: PageTreeGeneration, terms: tuple[str, ...]) -> tuple[PageTreeNode, ...]:
    """Bound a long tree around lexical anchors instead of its document prefix."""
    if len(tree.nodes) <= _MAX_NODES_PER_TREE:
        return tree.nodes
    normalized_terms = tuple(dict.fromkeys(term.casefold() for term in terms if term))
    if not normalized_terms:
        return tree.nodes[:_MAX_NODES_PER_TREE]
    nodes_by_id = {node.node_id: node for node in tree.nodes}
    children_by_parent: dict[str, list[PageTreeNode]] = {}
    for node in tree.nodes:
        if node.parent_node_id is not None:
            children_by_parent.setdefault(node.parent_node_id, []).append(node)

    def score(node: PageTreeNode) -> int:
        title = node.title.casefold()
        summary = (node.summary or "").casefold()
        return sum(4 for term in normalized_terms if term in title) + sum(
            1 for term in normalized_terms if term in summary and term not in title
        )

    def ancestors(node: PageTreeNode) -> tuple[PageTreeNode, ...]:
        values: list[PageTreeNode] = []
        parent_id = node.parent_node_id
        while parent_id is not None:
            parent = nodes_by_id.get(parent_id)
            if parent is None:
                break
            values.append(parent)
            parent_id = parent.parent_node_id
        values.reverse()
        return tuple(values)

    selected: set[str] = set()

    def add(node: PageTreeNode) -> None:
        if len(selected) < _MAX_NODES_PER_TREE:
            selected.add(node.node_id)

    ranked_sections = sorted(
        (node for node in tree.nodes if node.kind in {"document", "section"} and score(node)),
        key=lambda node: (-score(node), node.order, node.node_id),
    )
    for node in ranked_sections:
        for related in (*ancestors(node), node):
            add(related)
        for child in children_by_parent.get(node.node_id, ()):  # expose procedure branches
            if child.kind == "section":
                add(child)
        if len(selected) == _MAX_NODES_PER_TREE:
            break

    ranked_nodes = sorted(
        (node for node in tree.nodes if score(node)),
        key=lambda node: (-score(node), node.order, node.node_id),
    )
    for node in ranked_nodes:
        for related in (*ancestors(node), node):
            add(related)
        if len(selected) == _MAX_NODES_PER_TREE:
            break
    for node in tree.nodes:
        add(node)
        if len(selected) == _MAX_NODES_PER_TREE:
            break
    return tuple(node for node in tree.nodes if node.node_id in selected)


def _selection_prompt(
    question: str,
    prompt_trees: tuple[tuple[PageTreeGeneration, tuple[PageTreeNode, ...]], ...],
) -> str:
    payload = {
        "question": question,
        "instructions": (
            "Select only nodes that route to original evidence needed to answer the question. "
            "Trees are bounded to query-relevant nodes, their ancestors, and nearby section "
            "branches rather than a complete document. "
            f"Select no more than {_MAX_TREES} documents and no more than "
            f"{_MAX_SELECTED_NODES} node IDs per document; prioritize the most useful nodes "
            "instead of returning every matching node. "
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
                    for node in nodes
                ],
            }
            for tree, nodes in prompt_trees
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _selected_nodes(
    content: str,
    prompt_trees: tuple[tuple[PageTreeGeneration, tuple[PageTreeNode, ...]], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict) or set(payload) != {"selections"}:
        raise ValueError("PageTree Selection must return exactly one selections field.")
    selections = payload.get("selections")
    if not isinstance(selections, list):
        raise ValueError("PageTree Selection selections must be an array.")
    if len(selections) > _MAX_TREES:
        raise ValueError(
            f"PageTree Selection must contain at most {_MAX_TREES} document selections."
        )
    nodes_by_document = {
        tree.document_version_id: {node.node_id for node in nodes} for tree, nodes in prompt_trees
    }
    selected: list[tuple[str, tuple[str, ...]]] = []
    selected_documents: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"document_id", "node_ids"}:
            raise ValueError(
                "Each PageTree Selection item must contain exactly document_id and node_ids."
            )
        document_id = selection.get("document_id")
        node_ids = selection.get("node_ids")
        if not isinstance(document_id, str):
            raise ValueError("PageTree Selection document_id must be a string.")
        if document_id in selected_documents:
            raise ValueError("PageTree Selection document_id values must be unique.")
        if document_id not in nodes_by_document:
            raise ValueError("PageTree Selection must use only supplied document IDs.")
        if not isinstance(node_ids, list):
            raise ValueError("PageTree Selection node_ids must be an array.")
        if len(node_ids) > _MAX_SELECTED_NODES:
            raise ValueError(
                "PageTree Selection node_ids must contain at most "
                f"{_MAX_SELECTED_NODES} supplied IDs per document."
            )
        if not all(isinstance(node_id, str) for node_id in node_ids):
            raise ValueError("PageTree Selection node_ids must contain only strings.")
        unique_node_ids = tuple(dict.fromkeys(node_ids))
        if any(node_id not in nodes_by_document[document_id] for node_id in unique_node_ids):
            raise ValueError("PageTree Selection must use only supplied node IDs.")
        selected_documents.add(document_id)
        selected.append((document_id, unique_node_ids))
    return tuple(selected)


def _selected_evidence_ids(
    trees: tuple[PageTreeGeneration, ...],
    selections: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[str, ...]:
    trees_by_document = {tree.document_version_id: tree for tree in trees}
    streams_by_document: list[list[list[str]]] = []
    for document_id, node_ids in selections:
        tree = trees_by_document[document_id]
        nodes_by_id = {node.node_id: node for node in tree.nodes}
        selected = tuple(dict.fromkeys(node_ids))
        section_children: dict[str, list[str]] = {}
        for node in tree.nodes:
            if node.parent_node_id is not None and node.kind == "section":
                section_children.setdefault(node.parent_node_id, []).append(node.node_id)
        stream_specs: list[tuple[str, frozenset[str]]] = []
        seen_specs: set[tuple[str, frozenset[str]]] = set()

        def add_spec(root_id: str, excluded_children: frozenset[str] = frozenset()) -> None:
            spec = (root_id, excluded_children)
            if spec not in seen_specs:
                seen_specs.add(spec)
                stream_specs.append(spec)

        for node_id in selected:
            children = tuple(section_children.get(node_id, ()))
            if children:
                add_spec(node_id, frozenset(children))
                for child_id in children:
                    add_spec(child_id)
            else:
                add_spec(node_id)
        content_values_by_spec: dict[tuple[str, frozenset[str]], list[str]] = {
            spec: [] for spec in stream_specs
        }
        section_values_by_spec: dict[tuple[str, frozenset[str]], list[str]] = {
            spec: [] for spec in stream_specs
        }
        seen_by_spec: dict[tuple[str, frozenset[str]], set[str]] = {
            spec: set() for spec in stream_specs
        }
        for node in tree.nodes:
            ancestry: set[str] = set()
            current: PageTreeNode | None = node
            while current is not None:
                ancestry.add(current.node_id)
                current = (
                    nodes_by_id.get(current.parent_node_id)
                    if current.parent_node_id is not None
                    else None
                )
            for spec in stream_specs:
                root_id, excluded_children = spec
                if root_id not in ancestry or excluded_children.intersection(ancestry):
                    continue
                for binding in node.evidence:
                    if binding.evidence_id in seen_by_spec[spec]:
                        continue
                    seen_by_spec[spec].add(binding.evidence_id)
                    values = (
                        section_values_by_spec[spec]
                        if node.kind == "section"
                        else content_values_by_spec[spec]
                    )
                    values.append(binding.evidence_id)
        streams_by_document.append(
            [
                [*content_values_by_spec[spec], *section_values_by_spec[spec]]
                for spec in stream_specs
            ]
        )
    document_evidence = [
        list(_round_robin_evidence_ids(streams, _MAX_SELECTED_EVIDENCE))
        for streams in streams_by_document
    ]
    return _round_robin_evidence_ids(document_evidence, _MAX_SELECTED_EVIDENCE)


def _round_robin_evidence_ids(streams: list[list[str]], limit: int) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    positions = [0] * len(streams)
    while len(evidence_ids) < limit:
        added = False
        for ordinal, stream in enumerate(streams):
            while positions[ordinal] < len(stream):
                evidence_id = stream[positions[ordinal]]
                positions[ordinal] += 1
                if evidence_id in evidence_ids:
                    continue
                evidence_ids.append(evidence_id)
                added = True
                break
            if len(evidence_ids) == limit:
                return tuple(evidence_ids)
        if not added:
            break
    return tuple(evidence_ids)


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    return stripped
