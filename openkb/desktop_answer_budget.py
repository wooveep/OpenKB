"""Resolve one verified Answer capacity policy for retrieval and generation."""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_CONTEXT_CAPACITY_TOKENS = 4_096
_DEFAULT_OUTPUT_RESERVE_TOKENS = 2_048
_EXPANDED_OUTPUT_RESERVE_TOKENS = 4_096
_EXPANDED_CONTEXT_THRESHOLD_TOKENS = 32_768
_LARGE_OUTPUT_RESERVE_TOKENS = 32_768
_LARGE_CONTEXT_THRESHOLD_TOKENS = 262_144
_BASELINE_MAX_EVIDENCE_REFS = 64
_BASELINE_MAX_SOURCE_TOKENS = 24_000
_LARGE_CONTEXT_MAX_EVIDENCE_REFS = 256
_LARGE_CONTEXT_MAX_SOURCE_TOKENS = 192_000


@dataclass(frozen=True)
class DesktopAnswerBudget:
    """The complete bounded policy shared by Navigation and Answer generation."""

    capability_verified: bool
    context_capacity_tokens: int
    final_output_reserve_tokens: int
    provider_output_ceiling_tokens: int
    navigation_max_evidence_refs: int
    navigation_max_source_tokens: int


def answer_budget_for_gateway(model_gateway: object | None) -> DesktopAnswerBudget:
    """Use advertised Answer capacity only after its exact profile was verified."""
    if not _answer_capability_verified(model_gateway):
        return _conservative_budget()
    context_capacity = _answer_context_capacity(model_gateway)
    if context_capacity is None:
        return _conservative_budget(capability_verified=True)
    final_output_reserve = answer_output_reserve_for_context(context_capacity)
    reasoning_allowance, maximum_output = _answer_output_capability(model_gateway)
    provider_output_ceiling = min(
        context_capacity // 2,
        final_output_reserve + reasoning_allowance,
    )
    if maximum_output is not None:
        provider_output_ceiling = min(provider_output_ceiling, maximum_output)
    navigation_max_evidence_refs = _BASELINE_MAX_EVIDENCE_REFS
    navigation_max_source_tokens = _BASELINE_MAX_SOURCE_TOKENS
    if context_capacity >= _LARGE_CONTEXT_THRESHOLD_TOKENS:
        navigation_max_evidence_refs = min(
            _LARGE_CONTEXT_MAX_EVIDENCE_REFS,
            max(_BASELINE_MAX_EVIDENCE_REFS, context_capacity // 2_048),
        )
        navigation_max_source_tokens = min(
            _LARGE_CONTEXT_MAX_SOURCE_TOKENS,
            max(_BASELINE_MAX_SOURCE_TOKENS, context_capacity // 4),
        )
    return DesktopAnswerBudget(
        capability_verified=True,
        context_capacity_tokens=context_capacity,
        final_output_reserve_tokens=final_output_reserve,
        provider_output_ceiling_tokens=provider_output_ceiling,
        navigation_max_evidence_refs=navigation_max_evidence_refs,
        navigation_max_source_tokens=navigation_max_source_tokens,
    )


def answer_output_reserve_for_context(context_capacity_tokens: int) -> int:
    """Reserve final-answer space without consuming the provider's full output ceiling."""
    if context_capacity_tokens >= _LARGE_CONTEXT_THRESHOLD_TOKENS:
        return _LARGE_OUTPUT_RESERVE_TOKENS
    if context_capacity_tokens >= _EXPANDED_CONTEXT_THRESHOLD_TOKENS:
        return _EXPANDED_OUTPUT_RESERVE_TOKENS
    return _DEFAULT_OUTPUT_RESERVE_TOKENS


def _answer_capability_verified(model_gateway: object | None) -> bool:
    verifier = getattr(model_gateway, "answer_capability_verified", None)
    if not callable(verifier):
        return False
    try:
        return bool(verifier())
    except Exception:
        return False


def _answer_context_capacity(model_gateway: object | None) -> int | None:
    resolver = getattr(model_gateway, "capability_for_operation", None)
    if not callable(resolver):
        return None
    try:
        capability = resolver("grounded_answer")
    except Exception:
        return None
    capacity = getattr(capability, "context_capacity", None)
    if type(capacity) is not int or capacity < _DEFAULT_CONTEXT_CAPACITY_TOKENS:
        return None
    return capacity


def _answer_output_capability(model_gateway: object | None) -> tuple[int, int | None]:
    resolver = getattr(model_gateway, "answer_capability_profile", None)
    if not callable(resolver):
        return 0, None
    try:
        profile = resolver()
    except Exception:
        return 0, None
    reasoning_allowance = getattr(profile, "reasoning_allowance_tokens", None)
    maximum_output = getattr(profile, "maximum_output_tokens", None)
    return (
        reasoning_allowance if type(reasoning_allowance) is int and reasoning_allowance >= 0 else 0,
        maximum_output if type(maximum_output) is int and maximum_output > 0 else None,
    )


def _conservative_budget(*, capability_verified: bool = False) -> DesktopAnswerBudget:
    return DesktopAnswerBudget(
        capability_verified=capability_verified,
        context_capacity_tokens=_DEFAULT_CONTEXT_CAPACITY_TOKENS,
        final_output_reserve_tokens=_DEFAULT_OUTPUT_RESERVE_TOKENS,
        provider_output_ceiling_tokens=_DEFAULT_OUTPUT_RESERVE_TOKENS,
        navigation_max_evidence_refs=_BASELINE_MAX_EVIDENCE_REFS,
        navigation_max_source_tokens=_BASELINE_MAX_SOURCE_TOKENS,
    )
