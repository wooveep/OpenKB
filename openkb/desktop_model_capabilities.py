"""Conservative model capabilities used by role routing and token planning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopModelCapabilityProfile:
    """Non-secret execution limits for one selected model."""

    context_capacity: int
    document_input_capacity: int
    supports_native_json_schema: bool
    supports_streaming: bool
    supports_reasoning: bool


def model_capability_profile(
    model: str,
    *,
    context_capacity: int | None = None,
    supports_streaming: bool | None = None,
) -> DesktopModelCapabilityProfile:
    """Resolve known metadata or the documented conservative unknown-model profile."""
    normalized = model.rsplit("/", 1)[-1].lower()
    known_context = _known_context_capacity(normalized)
    capacity = context_capacity or known_context or 16_000
    supports_reasoning = normalized.startswith(("gpt-5", "o1", "o3", "o4", "deepseek-"))
    supports_native_schema = normalized.startswith(
        ("gpt-4", "gpt-5", "o1", "o3", "o4", "deepseek-chat")
    )
    document_capacity = (
        capacity // 2
        if context_capacity is not None or known_context is None
        else max(8_000, capacity - max(8_000, capacity // 4))
    )
    return DesktopModelCapabilityProfile(
        context_capacity=capacity,
        document_input_capacity=document_capacity,
        supports_native_json_schema=supports_native_schema,
        supports_streaming=(
            supports_streaming
            if supports_streaming is not None
            else normalized.startswith(("gpt-", "o1", "o3", "o4", "deepseek-"))
        ),
        supports_reasoning=supports_reasoning,
    )


def _known_context_capacity(model: str) -> int | None:
    if model.startswith(("gpt-5", "gpt-4.1", "o3", "o4")):
        return 128_000
    if model.startswith("gpt-4o"):
        return 128_000
    if model.startswith("deepseek-"):
        return 1_000_000
    return None
