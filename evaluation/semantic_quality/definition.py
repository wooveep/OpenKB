"""Validated repository-owned inputs for the live semantic quality gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SemanticQualityError(RuntimeError):
    """A safe, actionable semantic-quality gate failure."""


@dataclass(frozen=True)
class LiveEvaluationProfile:
    provider: str
    api_base_url: str
    model: str
    structured_output_mode: str
    thinking: str
    repetitions: int
    temperature: float
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class EvaluationEvidence:
    evidence_id: str
    document_name: str
    section: str
    excerpt: str


@dataclass(frozen=True)
class EvaluationPageClaim:
    text: str
    evidence_ids: tuple[str, ...]
    applicability: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EvaluationPage:
    identity_id: str
    title: str
    claims: tuple[EvaluationPageClaim, ...]


@dataclass(frozen=True)
class EvaluationCase:
    suite_id: str
    case_id: str
    domain: str
    language: str
    question: str
    evidence: tuple[EvaluationEvidence, ...]
    page: EvaluationPage


@dataclass(frozen=True)
class EvaluationPair:
    pair_id: str
    left: EvaluationCase
    right: EvaluationCase
    relationship: str


@dataclass(frozen=True)
class EvaluationDefinition:
    profile: LiveEvaluationProfile
    cases: tuple[EvaluationCase, ...]
    metamorphic_pairs: tuple[EvaluationPair, ...]
    suite_dimensions: tuple[str, ...]
    pair_dimensions: tuple[str, ...]
    profile_digest: str
    matrix_digest: str
    rubric_digest: str


def load_evaluation_definition(repository_root: Path) -> EvaluationDefinition:
    """Load and validate the complete repository-owned evaluation definition."""
    root = repository_root.resolve() / "evaluation" / "semantic_quality"
    profile_path = root / "profile.json"
    matrix_path = root / "matrix.json"
    rubric_path = root / "rubric.json"
    profile_value, profile_digest = _read_json(profile_path)
    matrix_value, matrix_digest = _read_json(matrix_path)
    rubric_value, rubric_digest = _read_json(rubric_path)
    profile = _parse_profile(profile_value)
    cases, pairs = _parse_matrix(matrix_value)
    suite_dimensions, pair_dimensions = _parse_rubric(rubric_value)
    return EvaluationDefinition(
        profile=profile,
        cases=cases,
        metamorphic_pairs=pairs,
        suite_dimensions=suite_dimensions,
        pair_dimensions=pair_dimensions,
        profile_digest=profile_digest,
        matrix_digest=matrix_digest,
        rubric_digest=rubric_digest,
    )


def _parse_profile(value: object) -> LiveEvaluationProfile:
    mapping = _mapping(value, "profile")
    if set(mapping) != {
        "schema_version",
        "provider",
        "api_base_url",
        "model",
        "structured_output_mode",
        "thinking",
        "repetitions",
        "temperature",
        "max_output_tokens",
        "timeout_seconds",
    }:
        raise SemanticQualityError("The Live Evaluation Profile has unexpected fields.")
    if mapping.get("schema_version") != "openkb.semantic-quality-profile.v1":
        raise SemanticQualityError("The Live Evaluation Profile schema is unsupported.")
    profile = LiveEvaluationProfile(
        provider=_text(mapping.get("provider"), "profile.provider"),
        api_base_url=_text(mapping.get("api_base_url"), "profile.api_base_url"),
        model=_text(mapping.get("model"), "profile.model"),
        structured_output_mode=_text(
            mapping.get("structured_output_mode"), "profile.structured_output_mode"
        ),
        thinking=_text(mapping.get("thinking"), "profile.thinking"),
        repetitions=_integer(mapping.get("repetitions"), "profile.repetitions"),
        temperature=_number(mapping.get("temperature"), "profile.temperature"),
        max_output_tokens=_integer(mapping.get("max_output_tokens"), "profile.max_output_tokens"),
        timeout_seconds=_number(mapping.get("timeout_seconds"), "profile.timeout_seconds"),
    )
    if profile.provider != "deepseek":
        raise SemanticQualityError("The release profile must use the pinned DeepSeek adapter.")
    if profile.structured_output_mode != "json_object" or profile.thinking != "disabled":
        raise SemanticQualityError(
            "The release profile must use json_object output with thinking disabled."
        )
    if profile.repetitions != 3:
        raise SemanticQualityError("Candidate evaluation requires exactly three repetitions.")
    if profile.max_output_tokens <= 0 or profile.timeout_seconds <= 0:
        raise SemanticQualityError("Profile output and timeout bounds must be positive.")
    return profile


def _parse_matrix(value: object) -> tuple[tuple[EvaluationCase, ...], tuple[EvaluationPair, ...]]:
    mapping = _mapping(value, "matrix")
    _expect_keys(
        mapping,
        {"schema_version", "minimum_unique_domains", "cases", "metamorphic_pairs"},
        "matrix",
    )
    if mapping.get("schema_version") != "openkb.semantic-quality-matrix.v1":
        raise SemanticQualityError("The Semantic Quality Evaluation Matrix schema is unsupported.")
    minimum_domains = _integer(
        mapping.get("minimum_unique_domains"), "matrix.minimum_unique_domains"
    )
    raw_cases = mapping.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SemanticQualityError("The evaluation matrix must contain cases.")
    cases = tuple(_parse_case(item, index) for index, item in enumerate(raw_cases))
    if len({case.case_id for case in cases}) != len(cases):
        raise SemanticQualityError("Evaluation case IDs must be unique.")
    if len({case.suite_id for case in cases}) != len(cases):
        raise SemanticQualityError("Each evaluation case must have its own suite ID.")
    if len({case.domain for case in cases}) < max(5, minimum_domains):
        raise SemanticQualityError("The evaluation matrix does not cover enough unique domains.")
    by_id = {case.case_id: case for case in cases}
    raw_pairs = mapping.get("metamorphic_pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise SemanticQualityError("The matrix must declare a metamorphic language pair.")
    pairs: list[EvaluationPair] = []
    for index, item in enumerate(raw_pairs):
        pair = _mapping(item, f"matrix.metamorphic_pairs[{index}]")
        _expect_keys(
            pair,
            {"pair_id", "left_case_id", "right_case_id", "relationship"},
            f"matrix.metamorphic_pairs[{index}]",
        )
        left_id = _text(pair.get("left_case_id"), f"pair[{index}].left_case_id")
        right_id = _text(pair.get("right_case_id"), f"pair[{index}].right_case_id")
        try:
            left, right = by_id[left_id], by_id[right_id]
        except KeyError as error:
            raise SemanticQualityError("A metamorphic pair references an unknown case.") from error
        relationship = _text(pair.get("relationship"), f"pair[{index}].relationship")
        if relationship != "structurally_equivalent_translation":
            raise SemanticQualityError("Unsupported metamorphic relationship.")
        if {left.language, right.language} != {"en", "zh"}:
            raise SemanticQualityError("Translation pairs must contain English and Chinese cases.")
        pairs.append(
            EvaluationPair(
                pair_id=_text(pair.get("pair_id"), f"pair[{index}].pair_id"),
                left=left,
                right=right,
                relationship=relationship,
            )
        )
    if len({pair.pair_id for pair in pairs}) != len(pairs):
        raise SemanticQualityError("Metamorphic pair IDs must be unique.")
    return cases, tuple(pairs)


def _parse_case(value: object, index: int) -> EvaluationCase:
    case = _mapping(value, f"matrix.cases[{index}]")
    _expect_keys(
        case,
        {"suite_id", "case_id", "domain", "language", "question", "evidence", "page"},
        f"matrix.cases[{index}]",
    )
    evidence_value = case.get("evidence")
    if not isinstance(evidence_value, list) or not evidence_value:
        raise SemanticQualityError(f"Evaluation case {index} has no evidence.")
    evidence_items: list[EvaluationEvidence] = []
    for evidence_index, raw_item in enumerate(evidence_value):
        item = _mapping(raw_item, f"case.evidence[{evidence_index}]")
        _expect_keys(
            item,
            {"evidence_id", "document_name", "section", "excerpt"},
            f"case.evidence[{evidence_index}]",
        )
        evidence_items.append(
            EvaluationEvidence(
                evidence_id=_text(item.get("evidence_id"), "evidence.evidence_id"),
                document_name=_text(item.get("document_name"), "evidence.document_name"),
                section=_text(item.get("section"), "evidence.section"),
                excerpt=_text(item.get("excerpt"), "evidence.excerpt"),
            )
        )
    evidence = tuple(evidence_items)
    evidence_ids = {item.evidence_id for item in evidence}
    if len(evidence_ids) != len(evidence):
        raise SemanticQualityError(f"Evaluation case {index} has duplicate Evidence IDs.")
    page_value = _mapping(case.get("page"), f"matrix.cases[{index}].page")
    _expect_keys(page_value, {"identity_id", "title", "claims"}, f"matrix.cases[{index}].page")
    claims_value = page_value.get("claims")
    if not isinstance(claims_value, list) or not claims_value:
        raise SemanticQualityError(f"Evaluation case {index} has no page claims.")
    claims: list[EvaluationPageClaim] = []
    for claim_index, raw_claim in enumerate(claims_value):
        claim = _mapping(raw_claim, f"page.claims[{claim_index}]")
        _expect_keys(
            claim,
            {"text", "evidence_ids", "applicability"},
            f"page.claims[{claim_index}]",
        )
        raw_evidence_ids = claim.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list) or not raw_evidence_ids:
            raise SemanticQualityError("Every evaluation claim needs Evidence IDs.")
        claim_evidence_ids = tuple(_text(item, "claim.evidence_ids") for item in raw_evidence_ids)
        if any(item not in evidence_ids for item in claim_evidence_ids):
            raise SemanticQualityError("An evaluation claim references unknown Evidence.")
        raw_applicability = claim.get("applicability")
        if not isinstance(raw_applicability, list):
            raise SemanticQualityError("Claim applicability must be a list.")
        applicability_items: list[tuple[str, str]] = []
        for applicability_index, raw_item in enumerate(raw_applicability):
            item = _mapping(raw_item, f"claim.applicability[{applicability_index}]")
            _expect_keys(
                item,
                {"dimension", "value"},
                f"claim.applicability[{applicability_index}]",
            )
            applicability_items.append(
                (
                    _text(item.get("dimension"), "dimension"),
                    _text(item.get("value"), "value"),
                )
            )
        applicability = tuple(applicability_items)
        claims.append(
            EvaluationPageClaim(
                text=_text(claim.get("text"), "claim.text"),
                evidence_ids=claim_evidence_ids,
                applicability=applicability,
            )
        )
    return EvaluationCase(
        suite_id=_text(case.get("suite_id"), f"case[{index}].suite_id"),
        case_id=_text(case.get("case_id"), f"case[{index}].case_id"),
        domain=_text(case.get("domain"), f"case[{index}].domain"),
        language=_text(case.get("language"), f"case[{index}].language"),
        question=_text(case.get("question"), f"case[{index}].question"),
        evidence=evidence,
        page=EvaluationPage(
            identity_id=_text(page_value.get("identity_id"), "page.identity_id"),
            title=_text(page_value.get("title"), "page.title"),
            claims=tuple(claims),
        ),
    )


def _parse_rubric(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rubric = _mapping(value, "rubric")
    _expect_keys(
        rubric,
        {"schema_version", "suite_dimensions", "pair_dimensions", "decision_rule"},
        "rubric",
    )
    if rubric.get("schema_version") != "openkb.semantic-quality-rubric.v1":
        raise SemanticQualityError("The semantic-quality rubric schema is unsupported.")
    _text(rubric.get("decision_rule"), "rubric.decision_rule")
    return (
        _dimension_ids(rubric.get("suite_dimensions"), "suite_dimensions"),
        _dimension_ids(rubric.get("pair_dimensions"), "pair_dimensions"),
    )


def _dimension_ids(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SemanticQualityError(f"Rubric {field} must not be empty.")
    result: list[str] = []
    for index, raw_item in enumerate(value):
        item = _mapping(raw_item, f"{field}[{index}]")
        _expect_keys(item, {"id", "prompt"}, f"{field}[{index}]")
        result.append(_text(item.get("id"), f"{field}.id"))
        _text(item.get("prompt"), f"{field}.prompt")
    if len(set(result)) != len(result):
        raise SemanticQualityError(f"Rubric {field} IDs must be unique.")
    return tuple(result)


def _read_json(path: Path) -> tuple[object, str]:
    try:
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticQualityError(f"Cannot load evaluation definition: {path.name}") from error
    return value, hashlib.sha256(content).hexdigest()


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticQualityError(f"{field} must be an object.")
    return value


def _expect_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise SemanticQualityError(f"{field} has unexpected or missing fields.")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticQualityError(f"{field} must be non-empty text.")
    return value.strip()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticQualityError(f"{field} must be an integer.")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticQualityError(f"{field} must be numeric.")
    return float(value)
