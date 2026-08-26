"""Immutable provider protocol and capacity decisions for structured Analysis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from openkb.desktop_model_capabilities import DesktopModelCapabilityProfile
from openkb.desktop_model_provider_adapter import (
    StructuredOutputMode,
    model_protocol_for,
)
from openkb.desktop_prompt_contracts import prompt_contract_for

if TYPE_CHECKING:
    from openkb.desktop_model_settings import DesktopModelSettings

MAX_ANALYSIS_DOCUMENT_INPUT_TOKENS = 12_000
MINIMUM_USEFUL_ANALYSIS_BATCH_TOKENS = 512
_ANALYSIS_PLAN_OPERATIONS = (
    "knowledge_analysis",
    "knowledge_analysis_batch",
    "knowledge_analysis_merge",
    "page_tree_enrichment",
    "knowledge_graph_extraction",
    "retrieval_plan",
    "structured_output_repair",
)
_REASONING_ALLOWANCE_NUMERATORS = {"off": 0, "low": 1, "medium": 2, "high": 4}
ANSWER_CAPABILITY_SYSTEM_PROMPT = "Stream the requested short answer capability value."
ANSWER_CAPABILITY_USER_PROMPT = "Reply with the single word OK."
_ANSWER_CAPABILITY_CHAT_FRAMING_RESERVE_TOKENS = 32
_ANSWER_CAPABILITY_FINAL_OUTPUT_TOKENS = 16
_ANSWER_CAPABILITY_REASONING_ALLOWANCE_TOKENS: dict[str, int] = {
    "off": 0,
    "low": 4_096,
    "medium": 8_192,
    "high": 16_384,
}


class DesktopModelCapacityError(ValueError):
    """The selected immutable model controls cannot fit a useful request."""


@dataclass(frozen=True)
class DesktopModelExecutionProfile:
    """Every provider, prompt, control, and budget decision pinned before dispatch."""

    provider: str
    model: str
    endpoint_digest: str
    adapter_identity: str
    adapter_version: str
    structured_output_mode: StructuredOutputMode
    streaming: bool
    reasoning_effort: str
    prompt_contract_digest: str
    generation_policy_digest: str
    context_capacity: int
    document_input_capacity: int
    prompt_material_tokens: int
    final_output_reserve_tokens: int
    reasoning_allowance_tokens: int
    provider_output_ceiling_tokens: int
    document_input_budget_tokens: int

    @property
    def identity(self) -> str:
        return hashlib.sha256(_json(self._identity_payload()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "identity": self.identity}

    @classmethod
    def from_dict(cls, value: object) -> DesktopModelExecutionProfile:
        if not isinstance(value, dict):
            raise ValueError("Model Execution Profile must be an object.")
        mode = _string(value, "structured_output_mode")
        if mode not in {"json_schema", "json_object", "prompt_contract"}:
            raise ValueError("Model Execution Profile structured output mode is invalid.")
        profile = cls(
            provider=_string(value, "provider"),
            model=_string(value, "model"),
            endpoint_digest=_string(value, "endpoint_digest"),
            adapter_identity=_string(value, "adapter_identity"),
            adapter_version=_string(value, "adapter_version"),
            structured_output_mode=cast(StructuredOutputMode, mode),
            streaming=_boolean(value, "streaming"),
            reasoning_effort=_string(value, "reasoning_effort"),
            prompt_contract_digest=_string(value, "prompt_contract_digest"),
            generation_policy_digest=_string(value, "generation_policy_digest"),
            context_capacity=_integer(value, "context_capacity"),
            document_input_capacity=_integer(value, "document_input_capacity"),
            prompt_material_tokens=_integer(value, "prompt_material_tokens"),
            final_output_reserve_tokens=_integer(value, "final_output_reserve_tokens"),
            reasoning_allowance_tokens=_integer(value, "reasoning_allowance_tokens"),
            provider_output_ceiling_tokens=_integer(value, "provider_output_ceiling_tokens"),
            document_input_budget_tokens=_integer(value, "document_input_budget_tokens"),
        )
        stored_identity = value.get("identity")
        if stored_identity is not None and stored_identity != profile.identity:
            raise ValueError("Model Execution Profile identity does not match its fields.")
        return profile

    def _identity_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint_digest": self.endpoint_digest,
            "adapter_identity": self.adapter_identity,
            "adapter_version": self.adapter_version,
            "structured_output_mode": self.structured_output_mode,
            "streaming": self.streaming,
            "reasoning_effort": self.reasoning_effort,
            "prompt_contract_digest": self.prompt_contract_digest,
            "generation_policy_digest": self.generation_policy_digest,
            "context_capacity": self.context_capacity,
            "document_input_capacity": self.document_input_capacity,
            "prompt_material_tokens": self.prompt_material_tokens,
            "final_output_reserve_tokens": self.final_output_reserve_tokens,
            "reasoning_allowance_tokens": self.reasoning_allowance_tokens,
            "provider_output_ceiling_tokens": self.provider_output_ceiling_tokens,
            "document_input_budget_tokens": self.document_input_budget_tokens,
        }


@dataclass(frozen=True)
class DesktopAnswerCapabilityProfile:
    """Credential-free identity proven by one streamed Answer capability check."""

    provider: str
    model: str
    endpoint_digest: str
    adapter_identity: str
    adapter_version: str
    streaming: bool
    reasoning_effort: str | None
    reasoning_source: str
    context_capacity: int
    reasoning_allowance_tokens: int
    capability_version: str = "openkb.answer-streaming.v2"
    role: str = "answer"

    @property
    def provider_output_ceiling_tokens(self) -> int:
        """Reserve final text after a bounded allowance for the selected reasoning mode."""
        return _ANSWER_CAPABILITY_FINAL_OUTPUT_TOKENS + self.reasoning_allowance_tokens

    @property
    def prompt_material_tokens(self) -> int:
        return (
            estimate_model_tokens(ANSWER_CAPABILITY_SYSTEM_PROMPT)
            + estimate_model_tokens(ANSWER_CAPABILITY_USER_PROMPT)
            + _ANSWER_CAPABILITY_CHAT_FRAMING_RESERVE_TOKENS
        )

    @property
    def prompt_contract_digest(self) -> str:
        return _digest(
            {
                "system": ANSWER_CAPABILITY_SYSTEM_PROMPT,
                "user": ANSWER_CAPABILITY_USER_PROMPT,
                "chat_framing_reserve_tokens": (_ANSWER_CAPABILITY_CHAT_FRAMING_RESERVE_TOKENS),
            }
        )

    @property
    def identity(self) -> str:
        return hashlib.sha256(_json(self._identity_payload()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "identity": self.identity}

    def _identity_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "endpoint_digest": self.endpoint_digest,
            "adapter_identity": self.adapter_identity,
            "adapter_version": self.adapter_version,
            "streaming": self.streaming,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_source": self.reasoning_source,
            "reasoning_allowance_tokens": self.reasoning_allowance_tokens,
            "context_capacity": self.context_capacity,
            "prompt_material_tokens": self.prompt_material_tokens,
            "prompt_contract_digest": self.prompt_contract_digest,
            "provider_output_ceiling_tokens": self.provider_output_ceiling_tokens,
            "capability_version": self.capability_version,
        }


def analysis_prompt_contract_bundle() -> dict[str, object]:
    """Return the canonical prompt material pinned by every Analysis plan."""
    return {
        "primary_operation": "knowledge_analysis_batch",
        "contracts": {
            operation: prompt_contract_for(operation).snapshot()
            for operation in _ANALYSIS_PLAN_OPERATIONS
        },
    }


def build_analysis_execution_profile(
    *,
    provider: str,
    model: str,
    capability: DesktopModelCapabilityProfile,
    reasoning_effort: str,
    api_base_url: str = "",
) -> DesktopModelExecutionProfile:
    """Resolve one complete Analysis profile without making a provider request."""
    adapter = model_protocol_for(provider)
    if not adapter.supports_structured_analysis or adapter.structured_output_mode is None:
        raise DesktopModelCapacityError(
            adapter.analysis_unavailable_reason
            or "The selected provider cannot run structured Analysis."
        )
    if reasoning_effort not in adapter.supported_reasoning:
        raise DesktopModelCapacityError(
            f"The {adapter.identity} adapter cannot honor Analysis reasoning '{reasoning_effort}'."
        )
    if not capability.supports_streaming:
        raise DesktopModelCapacityError(
            "The selected Analysis profile does not support the required streaming path."
        )

    bundle = analysis_prompt_contract_bundle()
    contracts = bundle["contracts"]
    assert isinstance(contracts, dict)
    snapshots = tuple(contracts.values())
    final_reserve = max(_reserve_output_tokens(snapshot) for snapshot in snapshots)
    numerator = _REASONING_ALLOWANCE_NUMERATORS[reasoning_effort]
    reasoning_allowance = final_reserve * numerator // 2
    output_ceiling = final_reserve + reasoning_allowance
    prompt_material = max(_prompt_material_tokens(snapshot) for snapshot in snapshots)
    document_budget = min(
        MAX_ANALYSIS_DOCUMENT_INPUT_TOKENS,
        capability.document_input_capacity,
        capability.context_capacity - prompt_material - output_ceiling,
    )
    if document_budget < MINIMUM_USEFUL_ANALYSIS_BATCH_TOKENS:
        raise DesktopModelCapacityError(
            "The selected context capacity cannot fit the prompt, JSON example, "
            "reasoning allowance, final-output reserve, and a minimum useful Analysis batch. "
            "Choose a larger context capacity or a lower explicit reasoning level."
        )

    return DesktopModelExecutionProfile(
        provider=provider,
        model=model,
        endpoint_digest=hashlib.sha256(api_base_url.encode("utf-8")).hexdigest(),
        adapter_identity=adapter.identity,
        adapter_version=adapter.version,
        structured_output_mode=adapter.structured_output_mode,
        streaming=True,
        reasoning_effort=reasoning_effort,
        prompt_contract_digest=_digest(bundle),
        generation_policy_digest=_digest(
            {
                operation: snapshot.get("generation_parameters")
                for operation, snapshot in contracts.items()
                if isinstance(snapshot, dict)
            }
        ),
        context_capacity=capability.context_capacity,
        document_input_capacity=capability.document_input_capacity,
        prompt_material_tokens=prompt_material,
        final_output_reserve_tokens=final_reserve,
        reasoning_allowance_tokens=reasoning_allowance,
        provider_output_ceiling_tokens=output_ceiling,
        document_input_budget_tokens=document_budget,
    )


def analysis_execution_profile_for_settings(
    settings: DesktopModelSettings,
) -> DesktopModelExecutionProfile:
    """Resolve the exact Analysis profile represented by one settings draft."""
    return build_analysis_execution_profile(
        provider=settings.provider,
        model=settings.analysis_model_name,
        capability=settings.capability_for_role("analysis"),
        reasoning_effort=settings.reasoning_for_role("analysis") or "off",
        api_base_url=settings.api_base_url,
    )


def answer_capability_profile_for_settings(
    settings: DesktopModelSettings,
) -> DesktopAnswerCapabilityProfile:
    """Resolve the exact streamed Answer identity without provider I/O or credentials."""
    adapter = model_protocol_for(settings.provider)
    capability = settings.capability_for_role("answer")
    reasoning_effort = settings.reasoning_for_role("answer")
    selected_reasoning_allowance = (
        adapter.provider_default_reasoning_allowance_tokens
        if reasoning_effort is None
        else _ANSWER_CAPABILITY_REASONING_ALLOWANCE_TOKENS[reasoning_effort]
    )
    reasoning_allowance = max(
        selected_reasoning_allowance,
        adapter.minimum_capability_reasoning_allowance_tokens,
    )
    profile = DesktopAnswerCapabilityProfile(
        provider=settings.provider,
        model=settings.answer_model_name,
        endpoint_digest=hashlib.sha256(settings.api_base_url.encode("utf-8")).hexdigest(),
        adapter_identity=adapter.identity,
        adapter_version=adapter.version,
        streaming=True,
        reasoning_effort=reasoning_effort,
        reasoning_source=settings.reasoning_source_for_role("answer"),
        context_capacity=capability.context_capacity,
        reasoning_allowance_tokens=reasoning_allowance,
    )
    required_capacity = profile.prompt_material_tokens + profile.provider_output_ceiling_tokens
    if required_capacity > profile.context_capacity:
        raise DesktopModelCapacityError(
            "The selected Answer context capacity cannot fit the capability prompt, "
            "reasoning allowance, and final-text reserve. Choose a larger Answer context "
            "capacity or a lower explicit Answer reasoning level."
        )
    return profile


def _reserve_output_tokens(value: object) -> int:
    if not isinstance(value, dict):
        raise ValueError("Prompt Contract snapshot is invalid.")
    policy = value.get("token_budget_policy")
    if not isinstance(policy, dict):
        return 2_048
    reserve = policy.get("reserve_output_tokens")
    return reserve if type(reserve) is int and reserve > 0 else 2_048


def _prompt_material_tokens(value: object) -> int:
    if not isinstance(value, dict):
        raise ValueError("Prompt Contract snapshot is invalid.")
    material = {
        name: value.get(name)
        for name in (
            "instructions",
            "input_shape",
            "output_schema",
            "output_example",
            "validation_rules",
        )
    }
    return estimate_model_tokens(_json(material))


def estimate_model_tokens(value: str) -> int:
    """A deterministic conservative estimator: CJK chars one, other text four-to-one."""
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    other = len(value) - cjk
    return max(1, cjk + (other + 3) // 4)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string(value: dict[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ValueError(f"Model Execution Profile {name} is invalid.")
    return item


def _integer(value: dict[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int or item < 0:
        raise ValueError(f"Model Execution Profile {name} is invalid.")
    return item


def _boolean(value: dict[str, object], name: str) -> bool:
    item = value.get(name)
    if type(item) is not bool:
        raise ValueError(f"Model Execution Profile {name} is invalid.")
    return item
