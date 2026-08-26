"""Code-owned provider protocol capabilities for Desktop model execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openkb.desktop_model_gateway import DesktopProviderStreamEvent

StructuredOutputMode = Literal["json_schema", "json_object", "prompt_contract"]


@dataclass(frozen=True)
class DesktopModelProviderAdapter:
    """Stable provider protocol identity selected from explicit configuration."""

    identity: str
    version: str
    structured_output_mode: StructuredOutputMode | None
    supports_structured_analysis: bool
    supported_reasoning: frozenset[str]
    analysis_unavailable_reason: str | None = None

    def request_parameters(
        self,
        *,
        structured_output_mode: str | None,
        response_schema: dict[str, object] | None,
        response_schema_name: str | None,
        reasoning: str | None,
    ) -> dict[str, object]:
        """Encode only controls explicitly owned by this adapter version."""
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

        if self.identity == "deepseek":
            if reasoning == "off":
                parameters["thinking"] = {"type": "disabled"}
            elif reasoning in {"low", "medium", "high"}:
                parameters["thinking"] = {"type": "enabled"}
        return parameters

    def stream_event(self, chunk: object) -> DesktopProviderStreamEvent:
        """Extract provider-labelled final/reasoning fields without returning reasoning text."""
        choices = _value(chunk, "choices")
        if not isinstance(choices, list) or not choices:
            return DesktopProviderStreamEvent()
        choice = choices[0]
        delta = _value(choice, "delta")
        final_content = _value(delta, "content")
        if not isinstance(final_content, str):
            final_content = ""
        reasoning_content = ""
        if self.identity == "deepseek":
            candidate = _value(delta, "reasoning_content") or _value(delta, "reasoning")
            if isinstance(candidate, str):
                reasoning_content = candidate
        finish_reason = _value(choice, "finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
        return DesktopProviderStreamEvent(
            final_content=final_content,
            reasoning_character_count=len(reasoning_content),
            finish_reason=finish_reason,
            output_limit_reached=self.finish_reason_is_output_limit(finish_reason),
        )

    def finish_reason_is_output_limit(self, finish_reason: str | None) -> bool:
        return finish_reason in {"length", "max_tokens", "max_output_tokens"}


_DEEPSEEK_ADAPTER = DesktopModelProviderAdapter(
    identity="deepseek",
    version="deepseek.v1",
    structured_output_mode="json_object",
    supports_structured_analysis=True,
    supported_reasoning=frozenset({"off", "low", "medium", "high"}),
)

_CUSTOM_ADAPTER = DesktopModelProviderAdapter(
    identity="custom",
    version="custom.v1",
    structured_output_mode=None,
    supports_structured_analysis=False,
    supported_reasoning=frozenset(),
    analysis_unavailable_reason=(
        "Custom providers do not have a code-owned structured Analysis protocol."
    ),
)


def provider_adapter_for(provider: str) -> DesktopModelProviderAdapter:
    """Resolve provider behavior only from the explicit provider setting."""
    return _DEEPSEEK_ADAPTER if provider == "deepseek" else _CUSTOM_ADAPTER


def _value(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
