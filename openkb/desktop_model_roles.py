"""Route model-backed operations through configured capability roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from openkb.desktop_model_capabilities import model_capability_profile
from openkb.desktop_model_execution_profile import (
    DesktopAnswerCapabilityProfile,
    DesktopModelCapacityError,
    DesktopModelExecutionProfile,
    analysis_execution_profile_for_settings,
    answer_capability_profile_for_settings,
    build_analysis_execution_profile,
)
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
    ExecutionLane,
    require_execution_lane,
)
from openkb.desktop_model_provider_adapter import model_protocol_for
from openkb.desktop_model_settings import DesktopModelSettings, DesktopModelSettingsError
from openkb.desktop_model_terminal import (
    DesktopTerminalModelGateway,
)
from openkb.desktop_prompt_contracts import prompt_contract_for

if TYPE_CHECKING:
    from openkb.desktop_model_usage import DesktopModelUsageStore

AnalysisCapabilityVerifier = Callable[[DesktopModelExecutionProfile], bool]
AnswerCapabilityVerifier = Callable[[DesktopAnswerCapabilityProfile], bool]
AnalysisCapabilityInvalidator = Callable[
    [DesktopModelExecutionProfile, str, str],
    None,
]

ANALYSIS_MODEL_OPERATIONS = frozenset(
    {
        "knowledge_fact_harvest",
        "document_entity_inventory",
        "entity_dossier_planning",
        "knowledge_analysis",
        "knowledge_analysis_batch",
        "knowledge_analysis_merge",
        "page_tree_enrichment",
        "page_tree_selection",
        "knowledge_navigation_step",
        "knowledge_graph_extraction",
        "knowledge_relation_analysis",
        "retrieval_plan",
        "structured_output_repair",
        "model_capability_analysis",
        "model_capability_analysis_streaming",
    }
)
ANSWER_MODEL_OPERATIONS = frozenset({"grounded_answer", "model_capability_answer"})


def model_role_for_operation(operation: str) -> str:
    """Return the stable capability/usage role for an operation."""
    if operation in ANALYSIS_MODEL_OPERATIONS:
        return "analysis"
    if operation in ANSWER_MODEL_OPERATIONS:
        return "answer"
    return "default"


class DesktopRoleModelGateway(DesktopModelGateway):
    """One provider connection with independently selected model roles."""

    def __init__(
        self,
        *,
        settings: DesktopModelSettings,
        default_gateway: DesktopTerminalModelGateway,
        analysis_gateway: DesktopTerminalModelGateway,
        answer_gateway: DesktopTerminalModelGateway,
        gateway_factory: Callable[[str], DesktopTerminalModelGateway] | None = None,
        usage_store: DesktopModelUsageStore | None = None,
        analysis_capability_verifier: AnalysisCapabilityVerifier | None = None,
        analysis_capability_invalidator: AnalysisCapabilityInvalidator | None = None,
        analysis_capability_corroborated_invalidator: AnalysisCapabilityInvalidator | None = None,
        answer_capability_verifier: AnswerCapabilityVerifier | None = None,
        execution_lane: ExecutionLane = "background",
    ) -> None:
        self._settings = settings
        self._default_gateway = default_gateway
        self._analysis_gateway = analysis_gateway
        self._answer_gateway = answer_gateway
        self._gateway_factory = gateway_factory
        self._usage_store = usage_store
        self._analysis_capability_verifier = analysis_capability_verifier
        self._analysis_capability_invalidator = analysis_capability_invalidator
        self._analysis_capability_corroborated_invalidator = (
            analysis_capability_corroborated_invalidator
        )
        self._answer_capability_verifier = answer_capability_verifier
        self._execution_lane = require_execution_lane(execution_lane)

    @property
    def provider_name(self) -> str:
        return self._settings.provider

    @property
    def model_name(self) -> str:
        """Return the Analysis model for analysis checkpoint compatibility."""
        return self._settings.analysis_model_name

    @property
    def analysis_concurrency(self) -> int:
        return self._settings.max_concurrent_model_calls

    @property
    def requires_analysis_capability_check(self) -> bool:
        return True

    def for_lane(self, lane: ExecutionLane) -> DesktopRoleModelGateway:
        lane = require_execution_lane(lane)
        gateway_factory = self._gateway_factory
        lane_gateway_factory = (
            (lambda model: gateway_factory(model).for_lane(lane))
            if gateway_factory is not None
            else None
        )
        return DesktopRoleModelGateway(
            settings=self._settings,
            default_gateway=self._default_gateway.for_lane(lane),
            analysis_gateway=self._analysis_gateway.for_lane(lane),
            answer_gateway=self._answer_gateway.for_lane(lane),
            gateway_factory=lane_gateway_factory,
            usage_store=self._usage_store,
            analysis_capability_verifier=self._analysis_capability_verifier,
            analysis_capability_invalidator=self._analysis_capability_invalidator,
            analysis_capability_corroborated_invalidator=(
                self._analysis_capability_corroborated_invalidator
            ),
            answer_capability_verifier=self._answer_capability_verifier,
            execution_lane=lane,
        )

    def analysis_capability_verified(self) -> bool:
        verifier = self._analysis_capability_verifier
        if verifier is None:
            return True
        try:
            profile = self.execution_profile_for_operation("knowledge_analysis")
        except (DesktopModelCapacityError, DesktopModelSettingsError):
            return False
        return verifier(profile)

    def answer_capability_verified(self) -> bool:
        verifier = self._answer_capability_verifier
        if verifier is None:
            return True
        try:
            profile = self.answer_capability_profile()
        except (DesktopModelCapacityError, DesktopModelSettingsError):
            return False
        return verifier(profile)

    def answer_capability_profile(self) -> DesktopAnswerCapabilityProfile:
        """Return the same Answer reasoning budget proven by the capability check."""
        return answer_capability_profile_for_settings(self._settings)

    def invalidate_analysis_capability(self, failure_code: str, reason: str) -> None:
        invalidator = self._analysis_capability_invalidator
        if invalidator is not None:
            invalidator(
                self.execution_profile_for_operation("knowledge_analysis"),
                failure_code,
                reason,
            )

    def invalidate_corroborated_analysis_capability(self, failure_code: str, reason: str) -> None:
        invalidator = self._analysis_capability_corroborated_invalidator
        if invalidator is not None:
            invalidator(
                self.execution_profile_for_operation("knowledge_analysis"),
                failure_code,
                reason,
            )

    def record_model_result_failure(self, call_id: str, failure_code: str) -> None:
        usage_store = self._usage_store
        if usage_store is not None:
            usage_store.mark_result_failure(call_id, failure_code)

    def capability_for_operation(self, operation: str):
        role, _gateway = self._gateway_for(operation)
        return self._settings.capability_for_role(role)

    def execution_profile_for_operation(self, operation: str) -> DesktopModelExecutionProfile:
        """Resolve the immutable structured-Analysis profile without provider I/O."""
        role = model_role_for_operation(operation)
        if role != "analysis":
            raise DesktopModelSettingsError(
                "Only structured Analysis operations use an Analysis execution profile."
            )
        return analysis_execution_profile_for_settings(self._settings, operation=operation)

    def analyze(self, request: DesktopModelRequest, **kwargs) -> DesktopModelResult:
        role, gateway = self._gateway_for(request.operation, request.model_name)
        decorated = self._decorate(request, role)
        return self._invoke(gateway.analyze, decorated, role, kwargs)

    def analyze_once(self, request: DesktopModelRequest, **kwargs) -> DesktopModelResult:
        role, gateway = self._gateway_for(request.operation, request.model_name)
        decorated = self._decorate(request, role)
        return self._invoke(gateway.analyze_once, decorated, role, kwargs)

    def stream(self, request: DesktopModelRequest, **kwargs) -> DesktopModelResult:
        role, gateway = self._gateway_for(request.operation, request.model_name)
        decorated = self._decorate(request, role)
        return self._invoke(gateway.stream, decorated, role, kwargs)

    def _invoke(self, call, request: DesktopModelRequest, role: str, kwargs) -> DesktopModelResult:
        usage_store = self._usage_store
        if usage_store is None:
            return call(request, **kwargs)
        original_on_event = kwargs["on_event"]
        model = request.model_name or self._settings.model_for_role(role)

        def record_event(event: object) -> None:
            usage_store.record_event(
                request=request,
                event=event,
                provider=self._settings.provider,
                model=model,
            )
            original_on_event(event)

        result = call(request, **{**kwargs, "on_event": record_event})
        input_price, output_price = self._settings.pricing_for_role(role)
        usage_store.record_result(
            request=request,
            call_id=result.call_id,
            attempt=result.attempt_count,
            content=result.content,
            usage=result.usage,
            provider_request_id=result.provider_request_id,
            input_price_per_million=input_price,
            output_price_per_million=output_price,
        )
        return result

    def _gateway_for(
        self,
        operation: str,
        requested_model: str | None = None,
    ) -> tuple[str, DesktopTerminalModelGateway]:
        if model_role_for_operation(operation) == "analysis":
            if (
                requested_model is not None
                and requested_model != self._settings.analysis_model_name
                and self._gateway_factory is not None
            ):
                return "analysis", self._gateway_factory(requested_model)
            return "analysis", self._analysis_gateway
        if model_role_for_operation(operation) == "answer":
            return "answer", self._answer_gateway
        return "default", self._default_gateway

    def _decorate(self, request: DesktopModelRequest, role: str) -> DesktopModelRequest:
        selected_model = request.model_name or self._settings.model_for_role(role)
        capability = (
            model_capability_profile(
                selected_model,
                context_capacity=request.context_capacity,
            )
            if selected_model != self._settings.model_for_role(role)
            else self._settings.capability_for_role(role)
        )
        contract = prompt_contract_for(request.operation)
        configured_adapter = model_protocol_for(self._settings.provider)
        if (
            request.provider_adapter is not None
            and request.provider_adapter != configured_adapter.identity
        ):
            raise DesktopModelSettingsError(
                "The pinned Model Execution Profile no longer matches Model Configuration."
            )
        adapter = model_protocol_for(request.provider_adapter or self._settings.provider)
        reasoning = request.reasoning_effort or self._settings.reasoning_for_role(role)
        if reasoning is not None and reasoning not in adapter.supported_reasoning:
            reasoning = None
        response_schema = request.response_schema or contract.output_schema
        if response_schema is not None and not adapter.supports_structured_analysis:
            raise DesktopModelSettingsError(
                "Custom model providers cannot run structured Analysis. "
                "Choose the named DeepSeek provider for the Analysis role."
            )
        generation_parameters = (
            dict(request.generation_parameters)
            if request.generation_parameters
            else dict(contract.generation_parameters)
        )
        if (
            response_schema is not None
            and role == "analysis"
            and "max_tokens" not in generation_parameters
        ):
            budget_operation = (
                request.parent_operation
                if request.operation == "structured_output_repair"
                and request.parent_operation is not None
                else request.operation
            )
            generation_parameters["max_tokens"] = build_analysis_execution_profile(
                provider=self._settings.provider,
                model=selected_model,
                capability=capability,
                reasoning_effort=reasoning or "off",
                api_base_url=self._settings.api_base_url,
                operation=budget_operation,
            ).provider_output_ceiling_tokens
        capability_identity = request.capability_identity
        if role == "analysis" and capability_identity is None:
            capability_identity = build_analysis_execution_profile(
                provider=self._settings.provider,
                model=selected_model,
                capability=capability,
                reasoning_effort=reasoning or "off",
                api_base_url=self._settings.api_base_url,
                operation=request.operation,
            ).capability_evidence_profile.identity
        return replace(
            request,
            model_role=role,
            model_name=selected_model,
            context_capacity=request.context_capacity or capability.context_capacity,
            document_input_capacity=(
                request.document_input_capacity or capability.document_input_capacity
            ),
            reasoning_effort=reasoning,
            provider_adapter=request.provider_adapter or adapter.identity,
            provider_adapter_version=request.provider_adapter_version or adapter.version,
            structured_output_mode=(
                request.structured_output_mode
                if response_schema is not None and request.structured_output_mode is not None
                else adapter.structured_output_mode
                if response_schema is not None
                else None
            ),
            response_schema=response_schema,
            response_schema_name=request.response_schema_name
            or contract.version.replace(".", "_").replace("-", "_")[:64],
            generation_parameters=generation_parameters,
            capability_identity=capability_identity,
            prompt_contract_digest=request.prompt_contract_digest or contract.digest,
            prompt_contract_version=request.prompt_contract_version or contract.version,
            prompt_contract_snapshot=request.prompt_contract_snapshot,
            supports_streaming=(
                request.supports_streaming
                if request.supports_streaming is not None
                else capability.supports_streaming
            ),
            execution_lane=self._execution_lane,
        )
