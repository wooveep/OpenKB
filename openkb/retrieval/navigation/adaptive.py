"""Bounded model decisions for the private adaptive retrieval session."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.answers.types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeRouteOption,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
from openkb.models.dispatch_budget import (
    ModelDispatchBudgetExhausted,
    require_model_dispatch_budget,
)
from openkb.models.gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
)
from openkb.models.result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    require_model_operation_dispatch,
    suspend_analysis_operation_failure,
    suspend_model_operation_contract,
    suspend_structured_model_operation,
)
from openkb.models.semantic_structure_contracts import FACET_COVERAGE_VALUES
from openkb.models.structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.retrieval.navigation.validation import (
    NavigationAction,
    bounded_string,
    bounded_string_array,
    require_exact_object_fields,
    validated_navigation_actions,
)
from openkb.retrieval.trace import (
    DesktopFacetCoverageTrace,
    DesktopQuestionFacetTrace,
    DesktopRetrievalTrace,
)

logger = logging.getLogger(__name__)

NAVIGATION_STEP_SCHEMA_VERSION = "openkb.knowledge-navigation-step.v2"
NAVIGATION_MAX_ROUNDS = 3
NAVIGATION_MAX_MODEL_CALLS = 8
NAVIGATION_MAX_LOGICAL_READS = 24
NAVIGATION_MAX_LOGICAL_READS_PER_ROUND = NAVIGATION_MAX_LOGICAL_READS // NAVIGATION_MAX_ROUNDS
NAVIGATION_MAX_SOURCE_TOKENS = 24_000
NAVIGATION_MAX_SEARCH_ACTIONS = 6
NAVIGATION_MAX_WALL_SECONDS = 120.0
NAVIGATION_MAX_BATCH_KNOWLEDGE_READS = 8
NAVIGATION_MAX_BATCH_SOURCE_READS = 4
NAVIGATION_MAX_ADVERTISED_ROUTES = 24


class _NavigationModelBudgetExhausted(Exception):
    """The structured decision requested a physical call beyond its envelope."""


@dataclass(frozen=True)
class NavigationObjective:
    """The immutable model-derived Question Facet Plan for one session."""

    semantic_structure_state: str
    goal: str
    facets: tuple[DesktopQuestionFacetTrace, ...]

    @property
    def required_facet_ids(self) -> frozenset[str]:
        return frozenset(facet.facet_id for facet in self.facets if facet.importance == "required")

    def as_dict(self) -> dict[str, object]:
        return {
            "semantic_structure_state": self.semantic_structure_state,
            "goal": self.goal,
            "facets": [facet.as_dict() for facet in self.facets],
        }


@dataclass(frozen=True)
class NavigationDecision:
    """Validated observation, coverage and next action for one round."""

    coverage: tuple[DesktopFacetCoverageTrace, ...]
    actions: tuple[NavigationAction, ...]
    decision: str


@dataclass(frozen=True)
class NavigationStepResult:
    """Optional model decision plus physical call accounting and safe failure state."""

    decision: NavigationDecision | None = None
    degradations: tuple[str, ...] = ()
    model_cost: DesktopRetrievalModelCost = DesktopRetrievalModelCost()
    stop_reason: str | None = None


@dataclass(frozen=True)
class NavigationBudget:
    """Remaining code-owned envelope disclosed to, but never controlled by, the model."""

    rounds: int
    model_calls: int
    logical_reads: int
    source_tokens: int
    search_actions: int

    def as_dict(self) -> dict[str, int]:
        return {
            "rounds": max(0, self.rounds),
            "model_calls": max(0, self.model_calls),
            "logical_reads": max(0, self.logical_reads),
            "source_tokens": max(0, self.source_tokens),
            "search_actions": max(0, self.search_actions),
        }


def initial_navigation_objective(
    question: str,
    plan: DesktopRetrievalPlan,
    trace: DesktopRetrievalTrace | None = None,
) -> NavigationObjective:
    """Use only the accepted immutable Query Planning semantics."""
    del question, plan
    if trace is None or trace.semantic_structure_state != "known":
        return NavigationObjective("unknown", "", ())
    return NavigationObjective(
        semantic_structure_state="known",
        goal=trace.question_goal,
        facets=trace.question_facets,
    )


def navigation_requires_model(
    objective: NavigationObjective,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> bool:
    """Only a missing or partial required model-derived facet authorizes expansion."""
    return objective.semantic_structure_state == "known" and any(
        item.facet_id in objective.required_facet_ids and item.state in {"missing", "partial"}
        for item in coverage
    )


def seed_facet_coverage(
    objective: NavigationObjective, pack: DesktopEvidencePack
) -> tuple[DesktopFacetCoverageTrace, ...]:
    """Retain Query Planning coverage only while its Evidence remains in the seed pack."""
    if objective.semantic_structure_state != "known":
        return ()
    available = frozenset(item.evidence_id for item in pack.evidence)
    by_id = {item.facet_id: item for item in pack.retrieval_trace.facet_coverage}
    return tuple(
        _bounded_seed_coverage(facet.facet_id, by_id.get(facet.facet_id), available)
        for facet in objective.facets
    )


def current_navigation_snapshot_id(database_path: Path) -> str:
    """Fingerprint every mutable authority identity used by a Navigation Session."""
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("BEGIN")
        material = {
            "catalog": _rows(
                connection,
                "SELECT source_revision, current_generation_id, is_stale "
                "FROM knowledge_catalog_state WHERE singleton = 1",
            ),
            "knowledge_generation": _rows(
                connection,
                "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1",
            ),
            "page_trees": _rows(
                connection,
                "SELECT document_id, generation_id FROM document_page_tree_current "
                "ORDER BY document_id",
            ),
            "documents": _rows(
                connection,
                "SELECT document_id, availability, COALESCE(available_at, '') "
                "FROM source_documents ORDER BY document_id",
            ),
            "published_pages": _rows(
                connection,
                "SELECT page_id, current_revision_id, COALESCE(lifecycle_state, 'stable'), "
                "COALESCE(stale_after, '') FROM knowledge_pages ORDER BY page_id",
            ),
        }
    finally:
        connection.rollback()
        connection.close()
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"navigation-session-{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"


def run_navigation_step(
    *,
    kb_dir: Path,
    model_gateway: DesktopModelGateway | None,
    question: str,
    snapshot_id: str,
    objective: NavigationObjective,
    pack: DesktopEvidencePack,
    current_coverage: tuple[DesktopFacetCoverageTrace, ...],
    visited_action_ids: frozenset[str],
    budget: NavigationBudget,
    round_number: int,
    preferred_evidence_ids: tuple[str, ...] = (),
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    retry_scope: str | None = None,
    dispatch_deadline: float | None = None,
) -> NavigationStepResult:
    """Request and validate exactly one bounded adaptive navigation decision."""
    if model_gateway is None:
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_unavailable",),
            stop_reason="model_degraded",
        )
    if not gateway_analysis_capability_verified(model_gateway):
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_unverified",),
            stop_reason="model_degraded",
        )
    if not model_operation_dispatch_possible(
        kb_dir,
        model_gateway,
        operation="knowledge_navigation_step",
        retry_scope=retry_scope,
    ):
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_suspended",),
            stop_reason="model_degraded",
        )
    already_read = frozenset(pack.retrieval_trace.navigation_routes)
    available_routes = tuple(item for item in pack.route_options if item.route not in already_read)[
        :NAVIGATION_MAX_ADVERTISED_ROUTES
    ]
    prompt = _navigation_prompt(
        question=question,
        snapshot_id=snapshot_id,
        objective=objective,
        pack=pack,
        available_routes=available_routes,
        current_coverage=current_coverage,
        visited_action_ids=visited_action_ids,
        budget=budget,
        round_number=round_number,
        preferred_evidence_ids=preferred_evidence_ids,
    )
    attempts = 0
    response_characters = 0
    try:

        def invoke(request: DesktopModelRequest):
            nonlocal attempts, response_characters
            if attempts >= budget.model_calls:
                raise _NavigationModelBudgetExhausted
            require_model_dispatch_budget(dispatch_deadline)
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

            try:
                result = model_gateway.analyze_once(
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
            operation="knowledge_navigation_step",
            document_name="Pinned Knowledge Navigation View",
            source_material=prompt,
            invoke=invoke,
            validate=lambda content: _navigation_decision(
                content,
                snapshot_id=snapshot_id,
                seed_objective=objective,
                known_evidence_ids=frozenset(item.evidence_id for item in pack.evidence),
                visited_action_ids=visited_action_ids,
                available_routes=frozenset(item.route for item in available_routes),
                completed_routes=already_read,
                budget=budget,
            ),
        )
        mark_structured_output_operations_ready(
            kb_dir,
            model_gateway,
            output,
            authority=DesktopModelOperationCompletionAuthority.for_retry_scope(retry_scope),
        )
        return NavigationStepResult(
            decision=output.value,
            model_cost=_model_cost(prompt, attempts, response_characters),
        )
    except (_NavigationModelBudgetExhausted, ModelDispatchBudgetExhausted):
        return NavigationStepResult(
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="budget_exhausted",
        )
    except DesktopModelCancelledError:
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_cancelled",),
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="cancelled",
        )
    except DesktopModelOperationSuspendedError:
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_suspended",),
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="model_degraded",
        )
    except DesktopModelCallError as error:
        suspend_analysis_operation_failure(kb_dir, model_gateway, error)
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_failed",),
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="model_degraded",
        )
    except DesktopStructuredOutputInvalidError as error:
        logger.warning(
            "Knowledge Navigation Step structured output validation failed: %s",
            error.__cause__ or error,
        )
        suspend_structured_model_operation(
            kb_dir,
            model_gateway,
            error,
            operation="knowledge_navigation_step",
            failure_code="model_response_invalid",
            reason="The Knowledge Navigation Step response could not be validated.",
        )
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_invalid",),
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="model_degraded",
        )
    except (ValueError, json.JSONDecodeError) as error:
        logger.warning("Knowledge Navigation Step validation failed: %s", error)
        suspend_model_operation_contract(
            kb_dir,
            model_gateway,
            operation="knowledge_navigation_step",
            failure_code="model_response_invalid",
            reason="The Knowledge Navigation Step response could not be validated.",
        )
        return NavigationStepResult(
            degradations=("knowledge_navigation_step_invalid",),
            model_cost=_model_cost(prompt, attempts, response_characters),
            stop_reason="model_degraded",
        )


def coverage_complete(
    objective: NavigationObjective,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> bool:
    states = {
        item.facet_id: item.state
        for item in coverage
        if item.facet_id in objective.required_facet_ids
    }
    return bool(objective.required_facet_ids) and all(
        states.get(facet_id) == "covered" for facet_id in objective.required_facet_ids
    )


def coverage_gate_state(
    objective: NavigationObjective,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> str:
    if objective.semantic_structure_state != "known":
        return "unknown"
    required = tuple(item for item in coverage if item.facet_id in objective.required_facet_ids)
    if not required:
        return "covered"
    if coverage_complete(objective, coverage):
        return "covered"
    if any(item.state in {"covered", "partial"} for item in required):
        return "partial"
    return "uncovered"


def estimated_source_tokens(pack: DesktopEvidencePack) -> int:
    return sum(max(1, (len(item.excerpt) + 3) // 4) for item in pack.evidence)


def _navigation_prompt(
    *,
    question: str,
    snapshot_id: str,
    objective: NavigationObjective,
    pack: DesktopEvidencePack,
    available_routes: tuple[DesktopKnowledgeRouteOption, ...],
    current_coverage: tuple[DesktopFacetCoverageTrace, ...],
    visited_action_ids: frozenset[str],
    budget: NavigationBudget,
    round_number: int,
    preferred_evidence_ids: tuple[str, ...],
) -> str:
    observation_evidence_ids = _unique(
        (
            *preferred_evidence_ids,
            *(evidence_id for item in current_coverage for evidence_id in item.evidence_ids),
        )
    )
    payload = {
        "schema_version": NAVIGATION_STEP_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "question": question,
        "round": round_number,
        "objective": objective.as_dict(),
        "current_coverage": [item.as_dict() for item in current_coverage],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "document_id": item.document_id,
                "document_name": item.document_name,
                "section": item.section,
                "excerpt": item.excerpt[:2_400],
            }
            for item in _diverse_evidence(
                pack.evidence,
                maximum=24,
                preferred_ids=observation_evidence_ids,
            )
        ],
        "guidance": [
            {
                "route": item.route,
                "kind": item.kind,
                "authority": item.authority,
                "title": item.title,
                "content_markdown": item.content_markdown[:1_600],
                "source_evidence_ids": list(item.source_evidence_ids),
            }
            for item in pack.guidance[:16]
        ],
        "available_routes": [
            {"route": item.route, "kind": item.kind, "title": item.title}
            for item in available_routes
        ],
        "visited_action_ids": sorted(visited_action_ids),
        "remaining_budget": budget.as_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _diverse_evidence(
    evidence: tuple[DesktopEvidenceRef, ...],
    *,
    maximum: int,
    preferred_ids: tuple[str, ...] = (),
) -> tuple[DesktopEvidenceRef, ...]:
    """Expose current coverage evidence, then distinct sections, then extra blocks."""
    if maximum <= 0:
        return ()
    selected: list[DesktopEvidenceRef] = []
    selected_ids: set[str] = set()
    evidence_by_id = {item.evidence_id: item for item in evidence}
    for evidence_id in preferred_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None or item.evidence_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item.evidence_id)
        if len(selected) == maximum:
            return tuple(selected)
    seen_sections = {
        (item.document_id, " ".join(item.section.split()).casefold()) for item in selected
    }
    for item in evidence:
        if item.evidence_id in selected_ids:
            continue
        section_key = (item.document_id, " ".join(item.section.split()).casefold())
        if section_key in seen_sections:
            continue
        seen_sections.add(section_key)
        selected.append(item)
        selected_ids.add(item.evidence_id)
        if len(selected) == maximum:
            return tuple(selected)
    for item in evidence:
        if item.evidence_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item.evidence_id)
        if len(selected) == maximum:
            break
    return tuple(selected)


def _navigation_decision(
    content: str,
    *,
    snapshot_id: str,
    seed_objective: NavigationObjective,
    known_evidence_ids: frozenset[str],
    visited_action_ids: frozenset[str],
    available_routes: frozenset[str],
    completed_routes: frozenset[str] = frozenset(),
    budget: NavigationBudget,
) -> NavigationDecision:
    payload = json.loads(content)
    require_exact_object_fields(
        payload,
        {"schema_version", "snapshot_id", "coverage", "actions", "decision"},
        context="Navigation decision",
    )
    if payload["schema_version"] != NAVIGATION_STEP_SCHEMA_VERSION:
        raise ValueError("Navigation schema version is invalid.")
    if payload["snapshot_id"] != snapshot_id:
        raise ValueError("Navigation Snapshot is stale.")
    coverage = _coverage(payload["coverage"], seed_objective, known_evidence_ids)
    actions = validated_navigation_actions(
        payload["actions"],
        visited_action_ids=visited_action_ids,
        available_routes=available_routes,
        completed_routes=completed_routes,
        known_evidence_ids=known_evidence_ids,
        maximum_actions=min(3, budget.search_actions),
        coverage=coverage,
        required_facet_ids=seed_objective.required_facet_ids,
    )
    decision = payload["decision"]
    if decision not in {"continue", "stop"}:
        raise ValueError("Navigation decision is invalid.")
    if decision == "continue" and not actions:
        decision = "stop"
    if decision == "stop" and actions:
        raise ValueError("A stop decision cannot request more actions.")
    if coverage_complete(seed_objective, coverage) and decision != "stop":
        raise ValueError("Covered navigation must stop.")
    return NavigationDecision(coverage, actions, decision)


def _coverage(
    value: object,
    objective: NavigationObjective,
    known_evidence_ids: frozenset[str],
) -> tuple[DesktopFacetCoverageTrace, ...]:
    if not isinstance(value, list) or len(value) != len(objective.facets):
        raise ValueError("Navigation coverage must describe every facet exactly once.")
    by_facet: dict[str, DesktopFacetCoverageTrace] = {}
    known_facet_ids = frozenset(facet.facet_id for facet in objective.facets)
    for item in value:
        require_exact_object_fields(
            item,
            {"facet_id", "state", "evidence_ids"},
            context="Navigation coverage",
        )
        assert isinstance(item, dict)
        facet_id = bounded_string(item["facet_id"], 160)
        state = item["state"]
        if facet_id in by_facet or facet_id not in known_facet_ids:
            raise ValueError("Navigation coverage facet is unknown or duplicated.")
        if state not in FACET_COVERAGE_VALUES:
            raise ValueError("Navigation coverage state is invalid.")
        proposed_evidence_ids = bounded_string_array(
            item["evidence_ids"], maximum=16, item_limit=160
        )
        if any(evidence_id not in known_evidence_ids for evidence_id in proposed_evidence_ids):
            raise ValueError("Navigation coverage cites unknown Evidence.")
        if state in {"covered", "partial"} and not proposed_evidence_ids:
            state = "missing"
        if state == "missing" and proposed_evidence_ids:
            raise ValueError("Missing coverage cannot cite Evidence.")
        by_facet[facet_id] = DesktopFacetCoverageTrace(facet_id, str(state), proposed_evidence_ids)
    return tuple(by_facet[facet.facet_id] for facet in objective.facets)


def _bounded_seed_coverage(
    facet_id: str,
    coverage: DesktopFacetCoverageTrace | None,
    available_evidence_ids: frozenset[str],
) -> DesktopFacetCoverageTrace:
    if coverage is None:
        return DesktopFacetCoverageTrace(facet_id, "missing")
    evidence_ids = tuple(
        evidence_id
        for evidence_id in coverage.evidence_ids
        if evidence_id in available_evidence_ids
    )
    state = coverage.state
    if state in {"covered", "partial"} and not evidence_ids:
        state = "missing"
    elif state == "covered" and len(evidence_ids) < len(coverage.evidence_ids):
        state = "partial"
    return DesktopFacetCoverageTrace(facet_id, state, evidence_ids)


def _rows(connection: sqlite3.Connection, statement: str) -> tuple[tuple[object, ...], ...]:
    try:
        return tuple(tuple(row) for row in connection.execute(statement).fetchall())
    except sqlite3.OperationalError as error:
        if "no such table" in str(error) or "no such column" in str(error):
            return ()
        raise


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def _model_cost(prompt: str, attempts: int, response_characters: int) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=attempts,
        input_characters=len(prompt) * attempts,
        output_characters=response_characters,
    )
