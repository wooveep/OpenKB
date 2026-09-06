"""Named provider adapters and the conservative Custom compatibility protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from openkb.models.gateway import DesktopProviderStreamEvent

StructuredOutputMode = Literal["json_schema", "json_object", "prompt_contract"]


class DesktopModelProtocol(Protocol):
    """Protocol behavior used by a named adapter or the Custom compatibility path."""

    @property
    def identity(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def structured_output_mode(self) -> StructuredOutputMode | None: ...

    @property
    def supports_structured_analysis(self) -> bool: ...

    @property
    def supported_reasoning(self) -> frozenset[str]: ...

    @property
    def provider_default_reasoning_allowance_tokens(self) -> int: ...

    @property
    def minimum_capability_reasoning_allowance_tokens(self) -> int: ...

    @property
    def analysis_unavailable_reason(self) -> str | None: ...

    def request_parameters(
        self,
        *,
        structured_output_mode: str | None,
        response_schema: dict[str, object] | None,
        response_schema_name: str | None,
        reasoning: str | None,
    ) -> dict[str, object]: ...

    def stream_event(self, chunk: object) -> DesktopProviderStreamEvent: ...

    def finish_reason_is_output_limit(self, finish_reason: str | None) -> bool: ...


@dataclass(frozen=True)
class DeepSeekModelProviderAdapter:
    """Own the complete DeepSeek JSON, thinking, stream, and terminal protocol."""

    identity: str = "deepseek"
    version: str = "deepseek.v2"
    structured_output_mode: StructuredOutputMode | None = "json_object"
    supports_structured_analysis: bool = True
    supported_reasoning: frozenset[str] = frozenset({"off", "low", "medium", "high"})
    provider_default_reasoning_allowance_tokens: int = 8_192
    minimum_capability_reasoning_allowance_tokens: int = 8_192
    analysis_unavailable_reason: str | None = None

    def request_parameters(
        self,
        *,
        structured_output_mode: str | None,
        response_schema: dict[str, object] | None,
        response_schema_name: str | None,
        reasoning: str | None,
    ) -> dict[str, object]:
        parameters: dict[str, object] = {}
        if structured_output_mode == "json_schema" and response_schema is not None:
            parameters["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema_name or "openkb_structured_output",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        elif structured_output_mode == "json_object":
            parameters["response_format"] = {"type": "json_object"}

        if reasoning == "off":
            parameters["extra_body"] = {"thinking": {"type": "disabled"}}
        elif reasoning in {"low", "medium", "high"}:
            parameters["extra_body"] = {"thinking": {"type": "enabled"}}
            parameters["reasoning_effort"] = "low" if reasoning == "low" else "high"
        return parameters

    def stream_event(self, chunk: object) -> DesktopProviderStreamEvent:
        choices = _value(chunk, "choices")
        if not isinstance(choices, list) or not choices:
            return DesktopProviderStreamEvent()
        choice = choices[0]
        delta = _value(choice, "delta")
        final_content = _value(delta, "content")
        if not isinstance(final_content, str):
            final_content = ""
        candidate = _value(delta, "reasoning_content") or _value(delta, "reasoning")
        reasoning_content = candidate if isinstance(candidate, str) else ""
        finish_reason = _value(choice, "finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
        return DesktopProviderStreamEvent(
            final_content=final_content,
            sensitive_reasoning_content=reasoning_content,
            reasoning_character_count=len(reasoning_content),
            finish_reason=finish_reason,
            output_limit_reached=self.finish_reason_is_output_limit(finish_reason),
        )

    def finish_reason_is_output_limit(self, finish_reason: str | None) -> bool:
        return finish_reason in {"length", "max_tokens", "max_output_tokens"}


@dataclass(frozen=True)
class CustomModelCompatibilityProtocol:
    """Conservatively stream natural language without claiming a named provider adapter."""

    identity: str = "custom"
    version: str = "custom.compatibility.v1"
    structured_output_mode: StructuredOutputMode | None = None
    supports_structured_analysis: bool = False
    supported_reasoning: frozenset[str] = frozenset()
    provider_default_reasoning_allowance_tokens: int = 0
    minimum_capability_reasoning_allowance_tokens: int = 0
    analysis_unavailable_reason: str | None = (
        "Custom providers do not have a code-owned structured Analysis protocol."
    )

    def request_parameters(
        self,
        *,
        structured_output_mode: str | None,
        response_schema: dict[str, object] | None,
        response_schema_name: str | None,
        reasoning: str | None,
    ) -> dict[str, object]:
        del structured_output_mode, response_schema, response_schema_name, reasoning
        return {}

    def stream_event(self, chunk: object) -> DesktopProviderStreamEvent:
        choices = _value(chunk, "choices")
        if not isinstance(choices, list) or not choices:
            return DesktopProviderStreamEvent()
        choice = choices[0]
        final_content = _value(_value(choice, "delta"), "content")
        if not isinstance(final_content, str):
            final_content = ""
        finish_reason = _value(choice, "finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
        return DesktopProviderStreamEvent(
            final_content=final_content,
            finish_reason=finish_reason,
            output_limit_reached=self.finish_reason_is_output_limit(finish_reason),
        )

    def finish_reason_is_output_limit(self, finish_reason: str | None) -> bool:
        return finish_reason in {"length", "max_tokens", "max_output_tokens"}


_NAMED_PROVIDER_ADAPTERS: dict[str, DesktopModelProtocol] = {
    "deepseek": DeepSeekModelProviderAdapter(),
}
_CUSTOM_MODEL_PROTOCOL = CustomModelCompatibilityProtocol()


def named_provider_adapter_for(provider: str) -> DesktopModelProtocol:
    """Resolve only code-owned named-provider behavior from the adapter registry."""
    try:
        return _NAMED_PROVIDER_ADAPTERS[provider]
    except KeyError as error:
        raise ValueError(f"Unknown Desktop model provider adapter: {provider}") from error


def model_protocol_for(provider: str) -> DesktopModelProtocol:
    """Resolve named adapters without treating Custom compatibility as an adapter."""
    if provider == "custom":
        return _CUSTOM_MODEL_PROTOCOL
    return named_provider_adapter_for(provider)


def _value(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
