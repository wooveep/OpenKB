"""Compatibility checks for the sole explicit-terminal Desktop model gateway."""

from __future__ import annotations

import threading

import pytest

from openkb import desktop_model_transport
from openkb.config import LlmCredentialBundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelTransportError,
    classify_model_error,
)
from openkb.desktop_model_terminal import (
    MODEL_CONNECT_TIMEOUT_SECONDS,
    DesktopTerminalModelGateway,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


class HttpFailure(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_gateway_factory_has_no_response_timeout_constructor() -> None:
    gateway = DesktopModelGateway(lambda _request, _connect_timeout: "complete")

    assert isinstance(gateway, DesktopTerminalModelGateway)
    with pytest.raises(TypeError):
        DesktopModelGateway(
            lambda _request, _connect_timeout: "complete",
            initial_timeout_seconds=20,
        )


@pytest.mark.parametrize("timeout", (0.0, -1.0, float("nan"), float("inf")))
def test_request_scoped_response_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout must be finite and positive"):
        DesktopModelRequest(
            "query_planning",
            "Question",
            "{}",
            response_timeout_seconds=timeout,
        )


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (TimeoutError(), "model_network_transient"),
        (ConnectionError(), "model_network_transient"),
        (DesktopModelTransportError("network_timeout"), "model_network_transient"),
        (DesktopModelTransportError("provider_timeout"), "model_provider_failure"),
        (HttpFailure(408), "model_provider_failure"),
    ),
)
def test_explicit_timeout_failures_are_classified_by_origin(error: Exception, code: str) -> None:
    """Provider/network timeout errors stay failures; elapsed thinking never creates one."""
    assert classify_model_error(error).code == code


def test_gateway_analyze_once_accepts_only_terminal_controls() -> None:
    gateway = DesktopModelGateway(lambda _request, timeout: str(timeout))

    result = gateway.analyze_once(
        DesktopModelRequest("page_tree_selection", "Knowledge Base", "{}"),
        on_event=lambda _event: None,
    )

    assert result.content == str(MODEL_CONNECT_TIMEOUT_SECONDS)
    with pytest.raises(TypeError):
        gateway.analyze_once(
            DesktopModelRequest("page_tree_selection", "Knowledge Base", "{}"),
            on_event=lambda _event: None,
            timeout_seconds=20,
        )


def test_gateway_stops_while_waiting_for_provider_retry_after() -> None:
    retry_waiting = threading.Event()
    stop = threading.Event()
    stopped = threading.Event()

    def rate_limited_transport(_request, _connect_timeout):
        raise DesktopModelTransportError("rate_limited", retry_after_seconds=10)

    def run() -> None:
        try:
            DesktopModelGateway(rate_limited_transport).analyze(
                DesktopModelRequest("grounded_answer", "answer", "source"),
                on_event=lambda event: retry_waiting.set() if event.status == "retrying" else None,
                is_cancelled=stop.is_set,
            )
        except DesktopModelCancelledError:
            stopped.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert retry_waiting.wait(timeout=1)
    stop.set()
    worker.join(timeout=0.5)

    assert stopped.is_set()
    assert not worker.is_alive()


def test_configured_desktop_gateway_disables_malformed_model_config(tmp_path, monkeypatch):
    kb_dir = tmp_path / "desktop-kb"
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("- not-a-config-mapping\n", encoding="utf-8")
    monkeypatch.setattr(
        desktop_model_transport,
        "resolve_credential_bundle",
        lambda _kb_dir: LlmCredentialBundle(),
    )

    assert desktop_model_transport.desktop_model_gateway_for(kb_dir) is None


def test_recovery_override_is_ephemeral_and_carries_model_context(tmp_path, monkeypatch):
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Recovery KB")
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.write_text(
        "model: default/model\n"
        "desktop:\n"
        "  provider: deepseek\n"
        "  api_base_url: https://api.deepseek.com\n",
        encoding="utf-8",
    )
    models: list[object] = []
    requests: list[DesktopModelRequest] = []

    class Transport:
        def __init__(self, *, model, bundle):
            del bundle
            models.append(model)

        def __call__(self, request, connect_timeout_seconds):
            assert connect_timeout_seconds == MODEL_CONNECT_TIMEOUT_SECONDS
            requests.append(request)
            return "Recovered"

    monkeypatch.setattr(
        desktop_model_transport,
        "resolve_credential_bundle",
        lambda _kb_dir: LlmCredentialBundle(api_key="test-key"),
    )
    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", Transport)

    gateway = desktop_model_transport.desktop_model_gateway_for(
        kb_dir,
        DesktopRecoveryOverride(model="recovery/model", context_capacity=32_768),
    )

    assert gateway is not None
    result = gateway.analyze(
        DesktopModelRequest("knowledge_analysis", "guide.txt", "source"),
        on_event=lambda _event: None,
    )

    assert result.content == "Recovered"
    assert "deepseek/recovery/model" in models
    assert requests[0].model_name == "recovery/model"
    assert requests[0].context_capacity == 32_768
    assert config_path.read_text(encoding="utf-8") == (
        "model: default/model\n"
        "desktop:\n"
        "  provider: deepseek\n"
        "  api_base_url: https://api.deepseek.com\n"
    )


def test_non_retryable_errors_end_after_one_explicit_attempt() -> None:
    calls = 0

    def authentication_transport(_request, _connect_timeout):
        nonlocal calls
        calls += 1
        raise DesktopModelTransportError("authentication")

    with pytest.raises(DesktopModelCallError) as captured:
        DesktopModelGateway(authentication_transport).analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )

    assert captured.value.failure.code == "model_authentication_failed"
    assert calls == 1
