"""Validate the independent branches of one seeded Query Planning result."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, cast

from openkb.models.semantic_structure_contracts import (
    FACET_COVERAGE_VALUES,
    FACET_IMPORTANCE_VALUES,
    SEMANTIC_STRUCTURE_LIMITS,
    normalize_dynamic_semantic_text,
)
from openkb.shared.canonical_json import canonical_json_digest

FacetImportance = Literal["required", "supporting"]
FacetCoverageState = Literal["covered", "partial", "missing"]
SemanticStructureState = Literal["known", "unknown"]


@dataclass(frozen=True)
class QueryRetrievalPlan:
    terms: tuple[str, ...]


@dataclass(frozen=True)
class QuestionFacet:
    facet_id: str
    label: str
    description: str
    importance: FacetImportance


@dataclass(frozen=True)
class QuestionFacetPlan:
    goal: str
    facets: tuple[QuestionFacet, ...]
    digest: str


@dataclass(frozen=True)
class InitialFacetCoverage:
    facet_id: str
    state: FacetCoverageState
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class QueryPlanningResult:
    retrieval_plan: QueryRetrievalPlan | None
    facet_plan: QuestionFacetPlan | None
    coverage: tuple[InitialFacetCoverage, ...]
    semantic_structure_state: SemanticStructureState
    retrieval_issues: tuple[str, ...] = ()
    semantic_issues: tuple[str, ...] = ()


def parse_query_planning_result(
    content: str,
    *,
    question: str,
    conversation_context_digest: str,
    seed_evidence_ids: frozenset[str],
) -> QueryPlanningResult:
    """Accept valid logical children independently and never guess semantic structure."""
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return _invalid_result("invalid_json")
    if not isinstance(value, dict):
        return _invalid_result("invalid_top_level")
    if set(value) != {
        "retrieval_plan",
        "question_facet_plan",
        "initial_answer_coverage",
    }:
        return _invalid_result("invalid_top_level_fields")

    retrieval_plan: QueryRetrievalPlan | None = None
    retrieval_issues: tuple[str, ...] = ()
    try:
        retrieval_plan = _parse_retrieval_plan(value.get("retrieval_plan"))
    except ValueError as error:
        retrieval_issues = (str(error),)

    try:
        facet_plan, coverage = _parse_semantic_plan(
            value.get("question_facet_plan"),
            value.get("initial_answer_coverage"),
            question=question,
            conversation_context_digest=conversation_context_digest,
            seed_evidence_ids=seed_evidence_ids,
        )
    except ValueError as error:
        return QueryPlanningResult(
            retrieval_plan=retrieval_plan,
            facet_plan=None,
            coverage=(),
            semantic_structure_state="unknown",
            retrieval_issues=retrieval_issues,
            semantic_issues=(str(error),),
        )
    return QueryPlanningResult(
        retrieval_plan=retrieval_plan,
        facet_plan=facet_plan,
        coverage=coverage,
        semantic_structure_state="known",
        retrieval_issues=retrieval_issues,
    )


def _invalid_result(issue: str) -> QueryPlanningResult:
    return QueryPlanningResult(
        retrieval_plan=None,
        facet_plan=None,
        coverage=(),
        semantic_structure_state="unknown",
        retrieval_issues=(issue,),
        semantic_issues=(issue,),
    )


def _parse_retrieval_plan(value: object) -> QueryRetrievalPlan:
    if not isinstance(value, dict) or set(value) != {"terms"}:
        raise ValueError("invalid_retrieval_plan")
    raw_terms = value.get("terms")
    if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= 8:
        raise ValueError("invalid_retrieval_terms")
    terms: list[str] = []
    for index, raw_term in enumerate(raw_terms):
        term = normalize_dynamic_semantic_text(
            raw_term,
            field=f"retrieval_plan.terms[{index}]",
            maximum_characters=160,
        )
        if term not in terms:
            terms.append(term)
    if not terms:
        raise ValueError("empty_retrieval_terms")
    return QueryRetrievalPlan(tuple(terms))


def _parse_semantic_plan(
    plan_value: object,
    coverage_value: object,
    *,
    question: str,
    conversation_context_digest: str,
    seed_evidence_ids: frozenset[str],
) -> tuple[QuestionFacetPlan, tuple[InitialFacetCoverage, ...]]:
    limits = SEMANTIC_STRUCTURE_LIMITS
    if not isinstance(plan_value, dict) or set(plan_value) != {"goal", "facets"}:
        raise ValueError("invalid_question_facet_plan")
    goal = normalize_dynamic_semantic_text(
        plan_value.get("goal"),
        field="question_facet_plan.goal",
        maximum_characters=limits.max_facet_description_characters,
    )
    raw_facets = plan_value.get("facets")
    if not isinstance(raw_facets, list) or not 1 <= len(raw_facets) <= limits.max_facets:
        raise ValueError("invalid_question_facets")

    canonical_facets: list[tuple[str, str, FacetImportance]] = []
    for ordinal, raw_facet in enumerate(raw_facets):
        if not isinstance(raw_facet, dict) or set(raw_facet) != {
            "label",
            "description",
            "importance",
        }:
            raise ValueError(f"invalid_facet:{ordinal}")
        label = normalize_dynamic_semantic_text(
            raw_facet.get("label"),
            field=f"facets[{ordinal}].label",
            maximum_characters=limits.max_label_characters,
        )
        description = normalize_dynamic_semantic_text(
            raw_facet.get("description"),
            field=f"facets[{ordinal}].description",
            maximum_characters=limits.max_facet_description_characters,
        )
        importance = raw_facet.get("importance")
        if not isinstance(importance, str) or importance not in FACET_IMPORTANCE_VALUES:
            raise ValueError(f"invalid_facet_importance:{ordinal}")
        canonical_facets.append((label, description, cast(FacetImportance, importance)))

    plan_digest = canonical_json_digest(
        {
            "question": question,
            "conversation_context_digest": conversation_context_digest,
            "goal": goal,
            "facets": canonical_facets,
        }
    )
    facets = tuple(
        QuestionFacet(
            facet_id=_facet_id(plan_digest, ordinal),
            label=label,
            description=description,
            importance=importance,
        )
        for ordinal, (label, description, importance) in enumerate(canonical_facets)
    )
    coverage = _parse_coverage(coverage_value, facets, seed_evidence_ids)
    return QuestionFacetPlan(goal, facets, plan_digest), coverage


def _parse_coverage(
    value: object,
    facets: tuple[QuestionFacet, ...],
    seed_evidence_ids: frozenset[str],
) -> tuple[InitialFacetCoverage, ...]:
    if not isinstance(value, list) or len(value) != len(facets):
        raise ValueError("incomplete_initial_coverage")
    by_ordinal: dict[int, InitialFacetCoverage] = {}
    for raw_entry in value:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "facet_ordinal",
            "state",
            "evidence_ids",
        }:
            raise ValueError("invalid_initial_coverage")
        ordinal = raw_entry.get("facet_ordinal")
        state = raw_entry.get("state")
        raw_ids = raw_entry.get("evidence_ids")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal in by_ordinal:
            raise ValueError("invalid_coverage_ordinal")
        if ordinal < 0 or ordinal >= len(facets):
            raise ValueError("unknown_coverage_facet")
        if not isinstance(state, str) or state not in FACET_COVERAGE_VALUES:
            raise ValueError("invalid_coverage_state")
        if not isinstance(raw_ids, list) or not all(
            isinstance(evidence_id, str) and evidence_id for evidence_id in raw_ids
        ):
            raise ValueError("invalid_coverage_evidence")
        evidence_ids = tuple(dict.fromkeys(raw_ids))
        if any(evidence_id not in seed_evidence_ids for evidence_id in evidence_ids):
            raise ValueError("unknown_coverage_evidence")
        if state == "missing" and evidence_ids:
            raise ValueError("missing_facet_has_evidence")
        if state != "missing" and not evidence_ids:
            raise ValueError("supported_facet_lacks_evidence")
        by_ordinal[ordinal] = InitialFacetCoverage(
            facets[ordinal].facet_id,
            cast(FacetCoverageState, state),
            evidence_ids,
        )
    if set(by_ordinal) != set(range(len(facets))):
        raise ValueError("incomplete_initial_coverage")
    return tuple(by_ordinal[ordinal] for ordinal in range(len(facets)))


def _facet_id(plan_digest: str, ordinal: int) -> str:
    return "facet-" + hashlib.sha256(f"{plan_digest}\x1f{ordinal}".encode()).hexdigest()
