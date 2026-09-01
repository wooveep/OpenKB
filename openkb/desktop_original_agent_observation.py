"""Frozen, privacy-safe observations from the original OpenKB agent."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopEvaluationEvidenceSelector:
    """A durable corpus-snapshot anchor, resolved to a run-local EvidenceRef."""

    document_name: str
    text_contains: str


@dataclass(frozen=True)
class DesktopOriginalAgentObservation:
    """Reviewed original-agent behavior without retaining raw answers or traces."""

    critical_evidence: tuple[DesktopEvaluationEvidenceSelector, ...]
    answer_points: tuple[str, ...]
    unsupported_claim_markers: tuple[str, ...]
    citation_precision: float
    unsupported_claim_count: int
    latency_ms: float
    model_calls: int
    absent_answer_correct: bool = False
    original_commit_sha: str = ""
    model_profile_digest: str = ""
    sample_count: int = 1
    latency_variance_ms: float = 0.0


def evidence_selector(value: object) -> DesktopEvaluationEvidenceSelector:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation evidence selectors must be objects.")
    return DesktopEvaluationEvidenceSelector(
        document_name=_required_string(value, "document_name"),
        text_contains=_required_string(value, "text_contains"),
    )


def original_agent_observation(
    value: object,
    *,
    required: bool,
    expect_absent_answer: bool,
    require_reference_identity: bool = False,
) -> DesktopOriginalAgentObservation | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise ValueError("Evaluation case original_observation must be an object.")
    raw_evidence = value.get("critical_evidence")
    if not isinstance(raw_evidence, list):
        raise ValueError("original_observation critical_evidence must be an array.")
    observation = DesktopOriginalAgentObservation(
        critical_evidence=tuple(evidence_selector(item) for item in raw_evidence),
        answer_points=_required_strings(value, "answer_points"),
        unsupported_claim_markers=_required_strings(value, "unsupported_claim_markers"),
        citation_precision=_bounded_fraction(value, "citation_precision"),
        unsupported_claim_count=_nonnegative_int(value, "unsupported_claim_count"),
        latency_ms=_positive_float(value, "latency_ms"),
        model_calls=_nonnegative_int(value, "model_calls"),
        absent_answer_correct=_boolean(value, "absent_answer_correct", False),
        original_commit_sha=_digest(
            value, "original_commit_sha", length=40, required=require_reference_identity
        ),
        model_profile_digest=_digest(
            value, "model_profile_digest", length=64, required=require_reference_identity
        ),
        sample_count=_sample_count(value, required=require_reference_identity),
        latency_variance_ms=_nonnegative_float(
            value, "latency_variance_ms", required=require_reference_identity
        ),
    )
    if observation.absent_answer_correct != expect_absent_answer:
        raise ValueError("original_observation absent-answer result is inconsistent.")
    if expect_absent_answer:
        if observation.critical_evidence or observation.answer_points:
            raise ValueError("An absent original observation cannot contain evidence or points.")
    elif not observation.critical_evidence or not observation.answer_points:
        raise ValueError("A grounded original observation needs evidence and answer points.")
    return observation


def _digest(value: dict[object, object], key: str, *, length: int, required: bool) -> str:
    candidate = value.get(key)
    if candidate is None and not required:
        return ""
    if (
        not isinstance(candidate, str)
        or re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", candidate) is None
    ):
        raise ValueError(f"original_observation field {key} must be a {length}-character digest.")
    return candidate.casefold()


def _sample_count(value: dict[object, object], *, required: bool) -> int:
    candidate = value.get("sample_count")
    if candidate is None and not required:
        return 1
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 2:
        raise ValueError("original_observation field sample_count must be at least two.")
    return candidate


def _nonnegative_float(value: dict[object, object], key: str, *, required: bool) -> float:
    candidate = value.get(key)
    if candidate is None and not required:
        return 0.0
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ValueError(f"original_observation field {key} must be numeric.")
    result = float(candidate)
    if result < 0:
        raise ValueError(f"original_observation field {key} must be nonnegative.")
    return result


def _required_string(value: dict[object, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"original_observation field {key} must be a string.")
    return candidate.strip()


def _required_strings(value: dict[object, object], key: str) -> tuple[str, ...]:
    candidates = value.get(key)
    if not isinstance(candidates, list):
        raise ValueError(f"original_observation field {key} must be an array.")
    values = tuple(item.strip() for item in candidates if isinstance(item, str) and item.strip())
    if len(values) != len(candidates) or len({item.casefold() for item in values}) != len(values):
        raise ValueError(f"original_observation field {key} contains invalid values.")
    return values


def _bounded_fraction(value: dict[object, object], key: str) -> float:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ValueError(f"original_observation field {key} must be numeric.")
    result = float(candidate)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"original_observation field {key} must be between zero and one.")
    return result


def _positive_float(value: dict[object, object], key: str) -> float:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ValueError(f"original_observation field {key} must be numeric.")
    result = float(candidate)
    if result <= 0:
        raise ValueError(f"original_observation field {key} must be positive.")
    return result


def _nonnegative_int(value: dict[object, object], key: str) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        raise ValueError(f"original_observation field {key} must be a nonnegative integer.")
    return candidate


def _boolean(value: dict[object, object], key: str, default: bool) -> bool:
    candidate = value.get(key, default)
    if not isinstance(candidate, bool):
        raise ValueError(f"original_observation field {key} must be boolean.")
    return candidate
