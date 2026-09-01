"""Provider-owned protocol behavior for Desktop model roles."""

from __future__ import annotations

import json

import pytest
from litellm.utils import get_optional_params

from openkb import desktop_model_transport
from openkb.config import LlmCredentialBundle
from openkb.desktop_model_execution_profile import analysis_execution_profile_for_settings
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.desktop_model_provider_adapter import (
    model_protocol_for,
    named_provider_adapter_for,
)
from openkb.desktop_model_roles import DesktopRoleModelGateway
from openkb.desktop_model_settings import DesktopModelSettings, DesktopModelSettingsError
from openkb.desktop_model_terminal import DesktopTerminalModelGateway
from openkb.desktop_prompt_contracts import prompt_contract_for


def test_deepseek_resolves_role_reasoning_and_structured_protocol_explicitly() -> None:
    settings = DesktopModelSettings(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )

    adapter = named_provider_adapter_for(settings.provider)

    assert adapter.identity == "deepseek"
    assert adapter.version == "deepseek.v1"
    assert adapter.structured_output_mode == "json_object"
    assert settings.reasoning_for_role("analysis") == "off"
    assert settings.reasoning_for_role("answer") is None


def test_custom_uses_compatibility_protocol_without_a_named_provider_adapter() -> None:
    with pytest.raises(ValueError, match="Unknown Desktop model provider adapter"):
        named_provider_adapter_for("custom")

    protocol = model_protocol_for("custom")

    assert protocol.identity == "custom"
    assert protocol.version == "custom.compatibility.v1"
    assert protocol.supports_structured_analysis is False


def test_role_gateway_pins_deepseek_adapter_mode_and_effective_analysis_reasoning() -> None:
    requests: list[DesktopModelRequest] = []

    class CapturingGateway:
        def analyze(self, request: DesktopModelRequest, **_kwargs) -> DesktopModelResult:
            requests.append(request)
            return DesktopModelResult("call-1", '{"terms":["OpenKB"]}', 1)

    terminal = CapturingGateway()
    settings = DesktopModelSettings(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    gateway = DesktopRoleModelGateway(
        settings=settings,
        default_gateway=terminal,  # type: ignore[arg-type]
        analysis_gateway=terminal,  # type: ignore[arg-type]
        answer_gateway=terminal,  # type: ignore[arg-type]
    )

    gateway.analyze(
        DesktopModelRequest("retrieval_plan", "question", "Build a retrieval plan."),
        on_event=lambda _event: None,
    )

    assert requests[0].model_role == "analysis"
    assert requests[0].provider_adapter == "deepseek"
    assert requests[0].provider_adapter_version == "deepseek.v1"
    assert requests[0].structured_output_mode == "json_object"
    assert requests[0].reasoning_effort == "off"
    assert requests[0].generation_parameters == {
        "temperature": 0,
        "max_tokens": analysis_execution_profile_for_settings(
            settings
        ).provider_output_ceiling_tokens,
    }


def test_deepseek_structured_request_uses_json_object_and_disables_thinking(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": '{"terms":["OpenKB"]}'}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )

    transport(
        DesktopModelRequest(
            "retrieval_plan",
            "question",
            "Build a retrieval plan.",
            reasoning_effort="off",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema={"type": "object"},
        ),
        30,
    )

    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "thinking" not in captured[0]
    assert "reasoning_effort" not in captured[0]
    assert get_optional_params(
        model="deepseek-v4-pro",
        custom_llm_provider="deepseek",
        extra_body=captured[0]["extra_body"],
    )["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_initial_graph_request_renders_its_complete_output_contract(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": '{"nodes":[],"edges":[]}'}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )
    contract = prompt_contract_for("knowledge_graph_extraction")

    transport(
        DesktopModelRequest(
            "knowledge_graph_extraction",
            "guide.md",
            json.dumps(
                {
                    "evidence": [
                        {
                            "evidence_id": "evidence-1",
                            "text": "Atlas uses Gateway.",
                        }
                    ]
                }
            ),
            reasoning_effort="off",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema=contract.output_schema,
            response_example=contract.output_example,
            prompt_contract_version=contract.version,
            prompt_contract_snapshot=contract.snapshot(),
        ),
        30,
    )

    messages = captured[0]["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]["content"]
    assert "STRUCTURED OUTPUT CONTRACT" in system_message
    assert '"output_schema"' in system_message
    assert '"RELATED_TO"' in system_message
    assert '"support_quote"' in system_message
    assert '"output_example"' in system_message
    assert "evidence-1" in system_message
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_deepseek_page_tree_selection_renders_its_complete_output_contract(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": '{"selections":[]}'}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )
    contract = prompt_contract_for("page_tree_selection")

    transport(
        DesktopModelRequest(
            "page_tree_selection",
            "Current Knowledge Base",
            '{"question":"Compare Alpha and Beta","trees":[]}',
            reasoning_effort="off",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema=contract.output_schema,
            response_example=contract.output_example,
            prompt_contract_version=contract.version,
            prompt_contract_snapshot=contract.snapshot(),
        ),
        30,
    )

    messages = captured[0]["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]["content"]
    assert "STRUCTURED OUTPUT CONTRACT" in system_message
    assert '"selections"' in system_message
    assert '"node_ids"' in system_message
    assert '"maxItems":3' in system_message
    assert '"maxItems":12' in system_message
    assert '"maximum_twelve_nodes_per_document"' in system_message
    assert '"known_document_and_node_ids_only"' in system_message
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_deepseek_graph_repair_request_keeps_the_parent_contract_visible(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": '{"nodes":[],"edges":[]}'}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )
    graph_contract = prompt_contract_for("knowledge_graph_extraction")
    repair_contract = prompt_contract_for("structured_output_repair")

    transport(
        DesktopModelRequest(
            "structured_output_repair",
            "guide.md",
            "content-free repair input",
            reasoning_effort="off",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema=graph_contract.output_schema,
            response_example=graph_contract.output_example,
            parent_operation="knowledge_graph_extraction",
            parent_prompt_contract_digest=graph_contract.digest,
            prompt_contract_version=repair_contract.version,
            prompt_contract_snapshot=repair_contract.snapshot(),
        ),
        30,
    )

    messages = captured[0]["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]["content"]
    assert "STRUCTURED OUTPUT CONTRACT" in system_message
    assert '"parent_contract_version"' in system_message
    assert '"parent_instructions"' in system_message
    assert '"parent_validation_rules"' in system_message
    assert '"exact_support_quote_required"' in system_message
    assert '"same_evidence_edge_endpoints"' in system_message
    assert '"RELATED_TO"' in system_message
    assert '"support_quote"' in system_message
    assert '"output_example"' in system_message
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_deepseek_knowledge_analysis_renders_its_complete_output_contract(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )
    contract = prompt_contract_for("knowledge_analysis_batch")

    transport(
        DesktopModelRequest(
            "knowledge_analysis_batch",
            "guide.md",
            "content-free evidence batch",
            reasoning_effort="off",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema=contract.output_schema,
            response_example=contract.output_example,
            prompt_contract_version=contract.version,
            prompt_contract_snapshot=contract.snapshot(),
        ),
        30,
    )

    messages = captured[0]["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]["content"]
    assert "STRUCTURED OUTPUT CONTRACT" in system_message
    assert '"document_summary"' in system_message
    assert '"applicability"' in system_message
    assert '"troubleshooting"' in system_message
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_deepseek_grounded_answer_omits_structured_format_and_provider_default_thinking(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": "A cited answer [1]."}}]}

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="deepseek/deepseek-v4-pro",
        bundle=LlmCredentialBundle(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        ),
    )

    transport(
        DesktopModelRequest(
            "grounded_answer",
            "question",
            "Evidence",
            model_role="answer",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
        ),
        30,
    )

    assert "response_format" not in captured[0]
    assert "thinking" not in captured[0]
    assert "reasoning_effort" not in captured[0]


def test_custom_provider_model_name_cannot_impersonate_a_structured_analysis_adapter() -> None:
    class UnexpectedGateway:
        def analyze(self, _request: DesktopModelRequest, **_kwargs) -> DesktopModelResult:
            raise AssertionError("Unsupported Custom Analysis must stop before provider dispatch.")

    terminal = UnexpectedGateway()
    gateway = DesktopRoleModelGateway(
        settings=DesktopModelSettings(
            provider="custom",
            model="deepseek-v4-pro",
            api_base_url="https://custom.example.test/v1",
            api_key="test-key",
            max_concurrent_model_calls=1,
        ),
        default_gateway=terminal,  # type: ignore[arg-type]
        analysis_gateway=terminal,  # type: ignore[arg-type]
        answer_gateway=terminal,  # type: ignore[arg-type]
    )

    with pytest.raises(DesktopModelSettingsError, match="Custom.*structured Analysis"):
        gateway.analyze(
            DesktopModelRequest("retrieval_plan", "question", "Build a plan."),
            on_event=lambda _event: None,
        )


def test_deepseek_stream_separates_private_reasoning_from_final_output(monkeypatch) -> None:
    private_reasoning = "private chain of thought"
    chunks = iter(
        [
            {
                "choices": [
                    {"delta": {"reasoning_content": private_reasoning}, "finish_reason": None}
                ]
            },
            {"choices": [{"delta": {"content": '{"terms":["OpenKB"]}'}, "finish_reason": None}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            },
        ]
    )
    monkeypatch.setattr("litellm.completion", lambda **_kwargs: chunks)
    events = []
    visible: list[str] = []
    gateway = DesktopTerminalModelGateway(
        desktop_model_transport.DesktopLiteLLMTransport(
            model="deepseek/deepseek-v4-pro",
            bundle=LlmCredentialBundle(
                api_key="test-key",
                base_url="https://api.deepseek.com",
            ),
        ),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
    )

    result = gateway.stream(
        DesktopModelRequest(
            "retrieval_plan",
            "question",
            "Build a plan.",
            provider_adapter="deepseek",
            provider_adapter_version="deepseek.v1",
            structured_output_mode="json_object",
            response_schema={"type": "object"},
        ),
        on_event=events.append,
        on_delta=lambda _attempt, delta: visible.append(delta),
    )

    assert result.content == '{"terms":["OpenKB"]}'
    assert visible == ['{"terms":["OpenKB"]}']
    assert "reasoning_output_activity" in [event.status for event in events]
    assert "model_output_activity" in [event.status for event in events]
    assert result.observations is not None
    assert result.observations.finish_reason == "stop"
    assert result.observations.reasoning_chunk_count == 1
    assert result.observations.final_chunk_count == 1
    assert result.observations.reasoning_character_count == len(private_reasoning)
    assert result.sensitive_reasoning_content == ""
    assert private_reasoning not in repr(events)
    assert private_reasoning not in repr(result.observations)


@pytest.mark.parametrize(
    ("chunks", "expected_code", "reasoning_chunks", "final_chunks"),
    (
        (
            [
                {"choices": [{"delta": {"reasoning_content": "private"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
            ],
            "reasoning_output_exhausted",
            1,
            0,
        ),
        (
            [
                {"choices": [{"delta": {"reasoning_content": "private"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ],
            "reasoning_only_result",
            1,
            0,
        ),
        (
            [{"choices": [{"delta": {}, "finish_reason": "stop"}]}],
            "empty_final_result",
            0,
            0,
        ),
        (
            [
                {"choices": [{"delta": {"content": "   "}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ],
            "empty_final_result",
            0,
            1,
        ),
    ),
)
def test_deepseek_empty_final_results_are_specific_and_never_retried(
    monkeypatch,
    chunks,
    expected_code: str,
    reasoning_chunks: int,
    final_chunks: int,
) -> None:
    calls = 0

    def completion(**_kwargs):
        nonlocal calls
        calls += 1
        return iter(chunks)

    monkeypatch.setattr("litellm.completion", completion)
    events = []
    gateway = DesktopTerminalModelGateway(
        desktop_model_transport.DesktopLiteLLMTransport(
            model="deepseek/deepseek-v4-pro",
            bundle=LlmCredentialBundle(
                api_key="test-key",
                base_url="https://api.deepseek.com",
            ),
        ),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
    )

    with pytest.raises(DesktopModelCallError) as captured:
        gateway.stream(
            DesktopModelRequest(
                "retrieval_plan",
                "question",
                "Build a plan.",
                provider_adapter="deepseek",
                provider_adapter_version="deepseek.v1",
                structured_output_mode="json_object",
                response_schema={"type": "object"},
            ),
            on_event=events.append,
            on_delta=lambda _attempt, _delta: None,
        )

    assert calls == 1
    assert captured.value.attempt_count == 1
    assert captured.value.failure.code == expected_code
    assert captured.value.observations is not None
    assert captured.value.observations.reasoning_chunk_count == reasoning_chunks
    assert captured.value.observations.final_chunk_count == final_chunks
    assert events[-1].status == "model_result_failure"
    assert events[-1].failure_code == expected_code
