"""Bounded model decisions for the private adaptive retrieval session."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_answer_types import (
    DesktopEvidencePack,
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
from openkb.desktop_retrieval_trace import DesktopAnswerCoverageTrace
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)

NAVIGATION_STEP_SCHEMA_VERSION = "openkb.knowledge-navigation-step.v1"
NAVIGATION_MAX_ROUNDS = 3
NAVIGATION_MAX_MODEL_CALLS = 8
NAVIGATION_MAX_LOGICAL_READS = 24
NAVIGATION_MAX_SOURCE_TOKENS = 24_000
NAVIGATION_MAX_SEARCH_ACTIONS = 6
NAVIGATION_MAX_WALL_SECONDS = 120.0
NAVIGATION_MAX_BATCH_KNOWLEDGE_READS = 8
NAVIGATION_MAX_BATCH_SOURCE_READS = 4

_COVERAGE_STATES = frozenset({"covered", "partial", "missing", "not_applicable"})
_ANSWER_KINDS = frozenset(
    {"factual_lookup", "how_to", "comparison", "troubleshooting", "explanation"}
)
_HOW_TO = re.compile(
    r"\b(how\s+to|steps?|install|deploy|configure|configuration|set\s*up|build|migrate)\b"
    r"|如何|怎么|怎样|步骤|流程|安装|部署|配置|搭建|迁移",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\b(compare|comparison|difference|versus|vs\.?|both)\b|比较|对比|区别|差异|两者",
    re.IGNORECASE,
)
_TROUBLESHOOTING = re.compile(
    r"\b(troubleshoot|diagnose|debug|failure|failed|error|repair|recover|fix)\b"
    r"|排查|诊断|故障|失败|报错|修复|恢复",
    re.IGNORECASE,
)
_EXPLANATION = re.compile(r"\b(why|explain|relationship|works?)\b|为什么|原理|解释|关系")
_ACTION_WORDS = re.compile(
    r"install|deploy|configure|set\s*up|build|migrate|validate|verify|repair|recover|"
    r"安装|部署|配置|搭建|迁移|验证|检查|修复|恢复",
    re.IGNORECASE,
)
_CONSTRAINTS = re.compile(
    r"\b(?:v?\d+(?:\.\d+)*|single|double|two[- ]node|windows|linux)\b|"
    r"双节点|单节点|两节点|版本|集群|离线|在线",
    re.IGNORECASE,
)
_QUESTION_FORM = re.compile(
    r"\b(?:what|who|when|where|which|why|how)\b|什么|谁|何时|哪里|哪个|为什么|如何|怎么|怎样",
    re.IGNORECASE,
)
_LOOKUP_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*|[\u4e00-\u9fff]{2,}")
_LOOKUP_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "did",
        "do",
        "does",
        "for",
        "from",
        "in",
        "is",
        "of",
        "the",
        "to",
        "was",
        "were",
    }
)
_SQL_TERM = re.compile(
    r"^\s*(?:pragma\b|select\s+(?:\*|\d+|['\"])|select\b.+\b(?:from|where|join)\b)",
    re.IGNORECASE,
)


class _NavigationModelBudgetExhausted(Exception):
    """The structured decision requested a physical call beyond its envelope."""


@dataclass(frozen=True)
class NavigationObjective:
    """Intent and answer scope kept separate from lexical retrieval terms."""

    answer_kind: str
    subject: str
    requested_scope: str
    named_entities: tuple[str, ...]
    concepts: tuple[str, ...]
    user_actions: tuple[str, ...]
    constraints: tuple[str, ...]
    required_aspects: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "answer_kind": self.answer_kind,
            "subject": self.subject,
            "requested_scope": self.requested_scope,
            "named_entities": list(self.named_entities),
            "concepts": list(self.concepts),
            "user_actions": list(self.user_actions),
            "constraints": list(self.constraints),
            "required_aspects": list(self.required_aspects),
        }


@dataclass(frozen=True)
class NavigationAction:
    """One validated query-scoped expansion request."""

    kind: str
    terms: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        values = (*self.terms, *self.routes, *self.evidence_ids)
        normalized = tuple(sorted(value.casefold() for value in values))
        return f"{self.kind}:{'|'.join(normalized)}"


@dataclass(frozen=True)
class NavigationDecision:
    """Validated observation, coverage and next action for one round."""

    objective: NavigationObjective
    coverage: tuple[DesktopAnswerCoverageTrace, ...]
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


def initial_navigation_objective(question: str, plan: DesktopRetrievalPlan) -> NavigationObjective:
    """Create a deterministic seed objective; model steps may refine only its scope."""
    answer_kind = _answer_kind(question)
    constraints = _unique(match.group(0) for match in _CONSTRAINTS.finditer(question))
    user_actions = _unique(match.group(0) for match in _ACTION_WORDS.finditer(question))
    terms = _unique(plan.terms)
    return NavigationObjective(
        answer_kind=answer_kind,
        subject=" ".join(question.split()),
        requested_scope=" ".join(question.split()),
        named_entities=terms[:8],
        concepts=terms[8:12],
        user_actions=user_actions[:8],
        constraints=constraints[:8],
        required_aspects=_required_aspects(answer_kind),
    )


def navigation_requires_model(objective: NavigationObjective, pack: DesktopEvidencePack) -> bool:
    """Let a supported simple fact finish from the deterministic seed."""
    return not _simple_lookup_supported(objective, pack)


def deterministic_seed_coverage(
    objective: NavigationObjective, pack: DesktopEvidencePack
) -> tuple[DesktopAnswerCoverageTrace, ...]:
    """Record only the one coverage conclusion deterministic retrieval can prove."""
    if _simple_lookup_supported(objective, pack):
        evidence_ids = tuple(item.evidence_id for item in pack.evidence)
        return tuple(
            DesktopAnswerCoverageTrace(
                aspect=aspect,
                status="covered" if aspect == "requested_fact" else "not_applicable",
                evidence_ids=evidence_ids if aspect == "requested_fact" else (),
            )
            for aspect in objective.required_aspects
        )
    return tuple(
        DesktopAnswerCoverageTrace(aspect=aspect, status="missing")
        for aspect in objective.required_aspects
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
    current_coverage: tuple[DesktopAnswerCoverageTrace, ...],
    visited_action_ids: frozenset[str],
    budget: NavigationBudget,
    round_number: int,
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    retry_scope: str | None = None,
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
    prompt = _navigation_prompt(
        question=question,
        snapshot_id=snapshot_id,
        objective=objective,
        pack=pack,
        current_coverage=current_coverage,
        visited_action_ids=visited_action_ids,
        budget=budget,
        round_number=round_number,
    )
    attempts = 0
    response_characters = 0
    try:

        def invoke(request: DesktopModelRequest):
            nonlocal attempts, response_characters
            if attempts >= budget.model_calls:
                raise _NavigationModelBudgetExhausted
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
                available_routes=frozenset(item.route for item in pack.route_options),
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
    except _NavigationModelBudgetExhausted:
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
    except (ValueError, json.JSONDecodeError):
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


def coverage_complete(coverage: tuple[DesktopAnswerCoverageTrace, ...]) -> bool:
    return bool(coverage) and all(item.status in {"covered", "not_applicable"} for item in coverage)


def coverage_gate_state(coverage: tuple[DesktopAnswerCoverageTrace, ...]) -> str:
    if not coverage:
        return "not_applicable"
    if coverage_complete(coverage):
        return "covered"
    if any(item.status in {"covered", "partial"} for item in coverage):
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
    current_coverage: tuple[DesktopAnswerCoverageTrace, ...],
    visited_action_ids: frozenset[str],
    budget: NavigationBudget,
    round_number: int,
) -> str:
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
            for item in pack.evidence[:24]
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
            for item in pack.route_options[:24]
        ],
        "visited_action_ids": sorted(visited_action_ids),
        "remaining_budget": budget.as_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _navigation_decision(
    content: str,
    *,
    snapshot_id: str,
    seed_objective: NavigationObjective,
    known_evidence_ids: frozenset[str],
    visited_action_ids: frozenset[str],
    available_routes: frozenset[str],
    budget: NavigationBudget,
) -> NavigationDecision:
    payload = json.loads(content)
    _require_keys(
        payload,
        {"schema_version", "snapshot_id", "objective", "coverage", "actions", "decision"},
    )
    if payload["schema_version"] != NAVIGATION_STEP_SCHEMA_VERSION:
        raise ValueError("Navigation schema version is invalid.")
    if payload["snapshot_id"] != snapshot_id:
        raise ValueError("Navigation Snapshot is stale.")
    objective = _objective(payload["objective"], seed_objective)
    coverage = _coverage(payload["coverage"], objective, known_evidence_ids)
    actions = _actions(
        payload["actions"],
        visited_action_ids,
        available_routes,
        known_evidence_ids,
        budget,
    )
    decision = payload["decision"]
    if decision not in {"continue", "stop"}:
        raise ValueError("Navigation decision is invalid.")
    if decision == "continue" and not actions:
        raise ValueError("A continue decision requires at least one action.")
    if decision == "stop" and actions:
        raise ValueError("A stop decision cannot request more actions.")
    if coverage_complete(coverage) and decision != "stop":
        raise ValueError("Covered navigation must stop.")
    return NavigationDecision(objective, coverage, actions, decision)


def _objective(value: object, seed: NavigationObjective) -> NavigationObjective:
    _require_keys(
        value,
        {
            "answer_kind",
            "subject",
            "requested_scope",
            "named_entities",
            "concepts",
            "user_actions",
            "constraints",
            "required_aspects",
        },
    )
    assert isinstance(value, dict)
    answer_kind = _bounded_string(value["answer_kind"], 40)
    if answer_kind not in _ANSWER_KINDS or answer_kind != seed.answer_kind:
        raise ValueError("Navigation answer kind cannot drift.")
    required_aspects = _string_array(value["required_aspects"], maximum=12, item_limit=80)
    if not set(seed.required_aspects) <= set(required_aspects):
        raise ValueError("Navigation removed a code-owned required aspect.")
    return NavigationObjective(
        answer_kind=answer_kind,
        subject=_bounded_string(value["subject"], 240),
        requested_scope=_bounded_string(value["requested_scope"], 400),
        named_entities=_string_array(value["named_entities"], maximum=12, item_limit=120),
        concepts=_string_array(value["concepts"], maximum=12, item_limit=120),
        user_actions=_string_array(value["user_actions"], maximum=12, item_limit=120),
        constraints=_string_array(value["constraints"], maximum=12, item_limit=120),
        required_aspects=required_aspects,
    )


def _coverage(
    value: object,
    objective: NavigationObjective,
    known_evidence_ids: frozenset[str],
) -> tuple[DesktopAnswerCoverageTrace, ...]:
    if not isinstance(value, list) or len(value) != len(objective.required_aspects):
        raise ValueError("Navigation coverage must describe every required aspect exactly once.")
    by_aspect: dict[str, DesktopAnswerCoverageTrace] = {}
    for item in value:
        _require_keys(item, {"aspect", "status", "evidence_ids"})
        assert isinstance(item, dict)
        aspect = _bounded_string(item["aspect"], 80)
        status = item["status"]
        if aspect in by_aspect or aspect not in objective.required_aspects:
            raise ValueError("Navigation coverage aspect is unknown or duplicated.")
        if status not in _COVERAGE_STATES:
            raise ValueError("Navigation coverage state is invalid.")
        evidence_ids = _string_array(item["evidence_ids"], maximum=16, item_limit=160)
        if not set(evidence_ids) <= known_evidence_ids:
            raise ValueError("Navigation coverage cites unknown Evidence.")
        if status in {"covered", "partial"} and not evidence_ids:
            raise ValueError("Source-supported coverage requires Evidence IDs.")
        if status in {"missing", "not_applicable"} and evidence_ids:
            raise ValueError("Missing or inapplicable coverage cannot cite Evidence.")
        by_aspect[aspect] = DesktopAnswerCoverageTrace(aspect, str(status), evidence_ids)
    return tuple(by_aspect[aspect] for aspect in objective.required_aspects)


def _actions(
    value: object,
    visited_action_ids: frozenset[str],
    available_routes: frozenset[str],
    known_evidence_ids: frozenset[str],
    budget: NavigationBudget,
) -> tuple[NavigationAction, ...]:
    if not isinstance(value, list) or len(value) > min(3, budget.search_actions):
        raise ValueError("Navigation action batch exceeds its remaining budget.")
    actions: list[NavigationAction] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Navigation action is invalid.")
        kind = item.get("kind")
        if kind == "search_routes":
            _require_keys(item, {"kind", "terms"})
            terms = _string_array(item["terms"], maximum=8, item_limit=120)
            if not terms:
                raise ValueError("Route search requires terms.")
            if any(_unsafe_term(term) for term in terms):
                raise ValueError("Route search term is unsafe.")
            action = NavigationAction("search_routes", terms=terms)
        elif kind == "read_routes":
            _require_keys(item, {"kind", "routes"})
            routes = _string_array(item["routes"], maximum=4, item_limit=320)
            if not routes or not set(routes) <= available_routes:
                raise ValueError("Navigation route is unavailable or unpublished.")
            action = NavigationAction("read_routes", routes=routes)
        elif kind == "read_source_sections":
            _require_keys(item, {"kind", "evidence_ids"})
            evidence_ids = _string_array(item["evidence_ids"], maximum=4, item_limit=160)
            if not evidence_ids or not set(evidence_ids) <= known_evidence_ids:
                raise ValueError("Source section anchor is not known Available Evidence.")
            action = NavigationAction(
                "read_source_sections",
                evidence_ids=evidence_ids,
            )
        else:
            raise ValueError("Navigation action kind is not allowed.")
        if action.identity in seen or action.identity in visited_action_ids:
            raise ValueError("Navigation action was already visited.")
        seen.add(action.identity)
        actions.append(action)
    return tuple(actions)


def _require_keys(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Navigation object fields are invalid.")


def _string_array(value: object, *, maximum: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Navigation string array is invalid.")
    items = tuple(_bounded_string(item, item_limit) for item in value)
    if len(items) != len(set(item.casefold() for item in items)):
        raise ValueError("Navigation string array contains duplicates.")
    return items


def _bounded_string(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Navigation string is invalid.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError("Navigation string is empty or too long.")
    return normalized


def _unsafe_term(term: str) -> bool:
    normalized = term.casefold()
    return (
        len(term) > 120
        or term.startswith(("/", "\\"))
        or ":\\" in term
        or "file://" in normalized
        or _SQL_TERM.search(normalized) is not None
        or "../" in term
        or "..\\" in term
    )


def _simple_lookup_supported(objective: NavigationObjective, pack: DesktopEvidencePack) -> bool:
    if objective.answer_kind != "factual_lookup" or not pack.evidence:
        return False
    subject = objective.subject.strip()
    if len(subject) > 160 or objective.constraints:
        return False
    evidence_text = " ".join(
        f"{item.document_name} {item.section} {item.excerpt}" for item in pack.evidence
    ).casefold()
    question_terms = tuple(
        token.casefold()
        for token in _LOOKUP_TOKEN.findall(subject)
        if token.casefold() not in _LOOKUP_STOP_WORDS and _QUESTION_FORM.fullmatch(token) is None
    )
    supported = sum(term in evidence_text for term in dict.fromkeys(question_terms))
    return bool(question_terms) and supported >= min(2, len(set(question_terms)))


def _answer_kind(question: str) -> str:
    if _HOW_TO.search(question):
        return "how_to"
    if _TROUBLESHOOTING.search(question):
        return "troubleshooting"
    if _COMPARISON.search(question):
        return "comparison"
    if _EXPLANATION.search(question):
        return "explanation"
    return "factual_lookup"


def _required_aspects(answer_kind: str) -> tuple[str, ...]:
    return {
        "how_to": (
            "prerequisites",
            "ordered_actions",
            "commands_or_configuration",
            "validation",
            "safety_warnings",
        ),
        "comparison": ("subjects", "comparison_dimensions", "differences", "scope"),
        "troubleshooting": (
            "symptoms",
            "possible_causes",
            "diagnostics",
            "remedies",
            "verification",
        ),
        "explanation": ("subject", "mechanism", "scope"),
        "factual_lookup": ("requested_fact", "scope"),
    }[answer_kind]


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
