"""Route model-backed operations through configured capability roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from openkb.desktop_model_capabilities import model_capability_profile
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.desktop_model_settings import DesktopModelSettings
from openkb.desktop_model_terminal import (
    DesktopTerminalModelGateway,
)
from openkb.desktop_prompt_contracts import prompt_contract_for

if TYPE_CHECKING:
    from openkb.desktop_model_usage import DesktopModelUsageStore

ANALYSIS_MODEL_OPERATIONS = frozenset(
    {
        "knowledge_analysis",
        "knowledge_analysis_batch",
        "knowledge_analysis_merge",
        "page_tree_enrichment",
        "page_tree_selection",
        "knowledge_graph_extraction",
        "retrieval_plan",
        "structured_output_repair",
        "model_capability_analysis",
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
        execution_lane: str = "background",
    ) -> None:
        self._settings = settings
        self._default_gateway = default_gateway
        self._analysis_gateway = analysis_gateway
        self._answer_gateway = answer_gateway
        self._gateway_factory = gateway_factory
        self._usage_store = usage_store
        self._execution_lane = execution_lane

    @property
    def provider_name(self) -> str:
        return self._settings.provider

    @property
    def model_name(self) -> str:
        """Return the Analysis model for analysis checkpoint compatibility."""
        return self._settings.analysis_model_name

    def for_lane(self, lane: str) -> DesktopRoleModelGateway:
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
            execution_lane=lane,
        )

    def capability_for_operation(self, operation: str):
        role, _gateway = self._gateway_for(operation)
        return self._settings.capability_for_role(role)

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
        reasoning = self._settings.reasoning_for_role(role)
        if not capability.supports_reasoning:
            reasoning = None
        return replace(
            request,
            model_role=role,
            model_name=selected_model,
            context_capacity=request.context_capacity or capability.context_capacity,
            document_input_capacity=(
                request.document_input_capacity or capability.document_input_capacity
            ),
            reasoning_effort="none" if reasoning == "off" else reasoning,
            response_schema=(
                request.response_schema or contract.output_schema
                if capability.supports_native_json_schema
                else None
            ),
            response_schema_name=request.response_schema_name
            or contract.version.replace(".", "_").replace("-", "_")[:64],
            generation_parameters=request.generation_parameters
            or dict(contract.generation_parameters),
            prompt_contract_digest=request.prompt_contract_digest or contract.digest,
            prompt_contract_version=request.prompt_contract_version or contract.version,
            prompt_contract_snapshot=request.prompt_contract_snapshot,
            supports_streaming=capability.supports_streaming,
            execution_lane=self._execution_lane,
        )
