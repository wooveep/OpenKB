"""Focused behavior checks for Desktop model defaults and diagnostic exports."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from threading import Event

import pytest

from openkb import desktop_engine_model_settings, desktop_model_transport
from openkb.config import DEFAULT_API_BASE_URL, DEFAULT_CONFIG, save_config
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.desktop_model_settings import (
    DEFAULT_MAX_CONCURRENT_MODEL_CALLS,
    DesktopModelSettings,
    DesktopModelSettingsError,
    model_capability_profile,
    read_desktop_model_settings,
    save_desktop_model_settings,
)
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _create_desktop_kb(kb_dir):
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Desktop KB")
    return kb_dir


@pytest.mark.parametrize("config", ({}, {"desktop": {}}, {"desktop": {"api_key": ""}}))
def test_absent_model_settings_keep_compatibility_defaults(tmp_path, config):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_config(kb_dir / ".openkb" / "config.yaml", config)

    settings = read_desktop_model_settings(kb_dir)

    assert settings.provider == "custom"
    assert settings.model == DEFAULT_CONFIG["model"]
    assert settings.api_base_url == DEFAULT_API_BASE_URL
    assert settings.api_key == ""
    assert settings.max_concurrent_model_calls == DEFAULT_MAX_CONCURRENT_MODEL_CALLS
    assert settings.requests_per_minute is None
    assert settings.tokens_per_minute is None


@pytest.mark.parametrize(
    "config",
    (
        {"desktop": None},
        {"desktop": []},
        {"model": None},
        {"model": "  "},
        {"desktop": {"provider": None}},
        {"desktop": {"provider": "unsupported"}},
        {"desktop": {"api_base_url": None}},
        {"desktop": {"api_base_url": ""}},
        {"desktop": {"api_base_url": "http://["}},
        {"desktop": {"api_key": 7}},
        {"desktop": {"max_concurrent_model_calls": True}},
        {"desktop": {"max_concurrent_model_calls": 0}},
        {"desktop": {"max_concurrent_model_calls": 9}},
        {"desktop": {"requests_per_minute": True}},
        {"desktop": {"requests_per_minute": 0}},
        {"desktop": {"tokens_per_minute": -1}},
    ),
)
def test_present_malformed_model_settings_are_never_defaulted(tmp_path, config):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_config(kb_dir / ".openkb" / "config.yaml", config)

    with pytest.raises(DesktopModelSettingsError) as captured:
        read_desktop_model_settings(kb_dir)

    assert captured.value.code == "desktop_model_settings_invalid"


def test_legacy_response_timeout_is_ignored_and_omitted_on_next_save(tmp_path):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_config(
        kb_dir / ".openkb" / "config.yaml",
        {
            "model": "legacy-model",
            "desktop": {
                "api_key": "key",
                "initial_timeout_seconds": "not-even-a-number",
            },
        },
    )

    settings = read_desktop_model_settings(kb_dir)
    assert "initial_timeout_seconds" not in settings.as_dict()
    assert "model_call_deadline_seconds" not in settings.as_dict()
    save_desktop_model_settings(
        kb_dir,
        provider=settings.provider,
        model=settings.model,
        api_base_url=settings.api_base_url,
        api_key=settings.api_key,
        max_concurrent_model_calls=settings.max_concurrent_model_calls,
    )
    assert "initial_timeout_seconds" not in (kb_dir / ".openkb" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_invalid_model_settings_are_explicit_but_optional_gateways_stay_disabled(tmp_path):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_config(
        kb_dir / ".openkb" / "config.yaml",
        {"model": False, "desktop": {"api_key": "plain-key"}},
    )
    workspace = DesktopKnowledgeBaseRuntime()
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    opened = server._dispatch(
        DesktopRequest(
            request_id="open-invalid-settings",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(kb_dir)},
        ),
        cancel_event=None,
    )
    assert opened["knowledge_base"]["kb_dir"] == str(kb_dir)
    assert desktop_model_transport.desktop_model_gateway_for(kb_dir) is None

    with pytest.raises(DesktopModelSettingsError) as captured:
        server._dispatch(
            DesktopRequest(
                request_id="read-invalid-settings",
                method="workbench.model_settings",
                params={},
            ),
            cancel_event=None,
        )
    assert captured.value.code == "desktop_model_settings_invalid"


def test_model_defaults_store_a_direct_connection_and_drive_the_gateway(tmp_path, monkeypatch):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    saved = save_desktop_model_settings(
        kb_dir,
        model="test/model",
        api_base_url="https://models.example.test/v1",
        api_key="persisted-test-key",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=25,
    )
    calls: list[tuple[object, str | None, float]] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            self._model = model
            self._bundle = bundle

        def __call__(self, _request, timeout_seconds):
            calls.append((self._model, self._bundle.api_key, timeout_seconds))
            return "complete"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)

    assert saved.provider == "custom"
    assert saved.api_key == "persisted-test-key"
    assert read_desktop_model_settings(kb_dir) == saved
    assert gateway is not None
    assert (
        gateway.analyze(
            DesktopModelRequest("document_analysis", "source.txt", "source"),
            on_event=lambda _event: None,
        ).content
        == "complete"
    )
    assert calls == [("openai/test/model", "persisted-test-key", 30.0)]
    config = (kb_dir / ".openkb" / "config.yaml").read_text()
    assert "persisted-test-key" in config
    assert "https://models.example.test/v1" in config
    assert "persisted-test-key" not in (kb_dir / ".openkb" / "state.sqlite3").read_bytes().decode(
        "latin-1"
    )


def test_model_roles_round_trip_and_fall_back_without_duplicating_connection(tmp_path):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")

    saved = save_desktop_model_settings(
        kb_dir,
        provider="custom",
        model="default-model",
        analysis_model="analysis-model",
        answer_model=None,
        api_base_url="https://models.example.test/v1",
        api_key="one-shared-key",
        max_concurrent_model_calls=4,
        requests_per_minute=120,
        tokens_per_minute=240_000,
        default_context_capacity=32_000,
        analysis_context_capacity=64_000,
        answer_context_capacity=None,
        default_reasoning=None,
        analysis_reasoning="low",
        answer_reasoning="off",
        default_input_price_per_million=1.25,
        default_output_price_per_million=2.5,
        analysis_input_price_per_million=0.5,
        analysis_output_price_per_million=1.0,
        answer_input_price_per_million=None,
        answer_output_price_per_million=None,
        initial_timeout_seconds=1,
    )

    assert saved.analysis_model_name == "analysis-model"
    assert saved.answer_model_name == "default-model"
    assert saved.max_concurrent_model_calls == 4
    assert saved.requests_per_minute == 120
    assert saved.tokens_per_minute == 240_000
    assert saved.analysis_reasoning == "low"
    assert saved.answer_reasoning == "off"
    assert read_desktop_model_settings(kb_dir) == saved
    config = (kb_dir / ".openkb" / "config.yaml").read_text(encoding="utf-8")
    assert config.count("one-shared-key") == 1
    assert config.count("https://models.example.test/v1") == 1


def test_role_settings_centralize_default_fallbacks() -> None:
    settings = DesktopModelSettings(
        provider="custom",
        model="default-model",
        api_base_url="https://models.example.test/v1",
        api_key="secret",
        max_concurrent_model_calls=2,
        analysis_model="analysis-model",
        default_context_capacity=32_000,
        default_reasoning="medium",
        default_input_price_per_million=1.0,
        default_output_price_per_million=2.0,
    )

    analysis = settings.role_settings("analysis")

    assert analysis.model == "analysis-model"
    assert analysis.context_capacity == 32_000
    assert analysis.reasoning == "medium"
    assert analysis.input_price_per_million == 1.0
    assert analysis.output_price_per_million == 2.0


def test_unknown_model_capability_is_conservative_and_overridable():
    unknown = model_capability_profile("private-model")
    overridden = model_capability_profile("private-model", context_capacity=48_000)
    known_custom = DesktopModelSettings(
        provider="custom",
        model="gpt-5-compatible",
        api_base_url="https://models.example.test/v1",
        api_key="key",
        max_concurrent_model_calls=1,
    ).capability_for_role("default")

    assert unknown.context_capacity == 16_000
    assert unknown.document_input_capacity == 8_000
    assert unknown.supports_reasoning is False
    assert unknown.supports_streaming is False
    assert overridden.context_capacity == 48_000
    assert overridden.document_input_capacity == 24_000
    assert known_custom.supports_streaming is True


def test_role_gateway_routes_analysis_and_answer_to_distinct_models(tmp_path, monkeypatch):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_desktop_model_settings(
        kb_dir,
        provider="custom",
        model="default-model",
        analysis_model="analysis-model",
        answer_model="answer-model",
        api_base_url="https://models.example.test/v1",
        api_key="shared-key",
        max_concurrent_model_calls=2,
        initial_timeout_seconds=1,
    )
    calls: list[tuple[object, str, str | None]] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            self._model = model

        def __call__(self, request, _timeout_seconds):
            calls.append((self._model, request.operation, request.reasoning_effort))
            return "complete"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)
    assert gateway is not None

    for operation in (
        "knowledge_analysis",
        "knowledge_analysis_batch",
        "knowledge_analysis_merge",
        "page_tree_enrichment",
        "knowledge_graph_extraction",
        "retrieval_plan",
    ):
        gateway.analyze(
            DesktopModelRequest(operation, "source", "content"),
            on_event=lambda _event: None,
        )
    gateway.stream(
        DesktopModelRequest("grounded_answer", "question", "content"),
        on_event=lambda _event: None,
        on_delta=lambda _attempt, _delta: None,
    )
    gateway.analyze(
        DesktopModelRequest("connection_test", "settings", "content"),
        on_event=lambda _event: None,
    )

    assert [str(model) for model, _operation, _reasoning in calls[:6]] == [
        "openai/analysis-model"
    ] * 6
    assert str(calls[6][0]) == "openai/answer-model"
    assert str(calls[7][0]) == "openai/default-model"


def test_deepseek_endpoint_routes_an_unprefixed_model_through_litellm(tmp_path, monkeypatch):
    """Existing DeepSeek settings must use LiteLLM's explicit provider route."""
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_desktop_model_settings(
        kb_dir,
        model="deepseek-v4-flash",
        api_base_url="https://api.deepseek.com/",
        api_key="persisted-test-key",
        max_concurrent_model_calls=1,
        initial_timeout_seconds=20,
    )
    calls: list[object] = []

    class FakeTransport:
        def __init__(self, *, model, bundle):
            self._model = model

        def __call__(self, _request, _timeout_seconds):
            calls.append(self._model)
            return "complete"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)

    assert gateway is not None
    assert read_desktop_model_settings(kb_dir).provider == "deepseek"
    assert (
        gateway.analyze(
            DesktopModelRequest("document_analysis", "source.txt", "source"),
            on_event=lambda _event: None,
        ).content
        == "complete"
    )
    assert calls == ["deepseek/deepseek-v4-flash"]


def test_diagnostic_bundle_is_explicit_and_redacts_source_model_and_credential_content(tmp_path):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    save_desktop_model_settings(
        kb_dir,
        model="test/model",
        api_base_url="https://models.example.test/v1",
        api_key="diagnostic-credential-secret",
        max_concurrent_model_calls=1,
        initial_timeout_seconds=20,
    )
    source = tmp_path / "private-source.txt"
    source.write_text("private-source-content", encoding="utf-8")
    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            lambda _request, _timeout: json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "document",
                    "document_description": "private-model-response",
                    "concepts": [],
                    "entities": [],
                }
            )
        ),
    ).import_text(source)

    destination = tmp_path / "desktop-diagnostics.zip"
    bundle = DesktopDiagnosticBundleService(kb_dir).export(destination)

    assert bundle.path == str(destination)
    assert set(bundle.files) == {
        "manifest.json",
        "model-settings.json",
        "import-jobs.json",
        "model-calls.json",
        "model-usage.json",
        "graph-diagnostics.json",
        "page-tree-enrichment.json",
        "integrity.json",
    }
    with zipfile.ZipFile(destination) as archive:
        content = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "private-source-content" not in content
    assert "private-model-response" not in content
    assert "diagnostic-credential-secret" not in content
    assert '"api_key_configured": true' in content


def test_engine_settings_routes_accept_a_direct_api_key_without_persisting_it_in_sqlite(tmp_path):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.open(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    saved = server._dispatch(
        DesktopRequest(
            request_id="settings-save",
            method="workbench.save_model_settings",
            params={
                "provider": "deepseek",
                "model": "test/model",
                "api_base_url": "https://models.example.test/v1",
                "api_key": "engine-settings-key",
                "max_concurrent_model_calls": 2,
                "initial_timeout_seconds": 30,
            },
        ),
        cancel_event=None,
    )
    exported = server._dispatch(
        DesktopRequest(
            request_id="diagnostic-export",
            method="workbench.export_diagnostic_bundle",
            params={"destination": str(tmp_path / "engine-diagnostics.zip")},
        ),
        cancel_event=None,
    )

    assert saved["api_key"] == "engine-settings-key"
    assert saved["provider"] == "deepseek"
    assert saved["api_base_url"] == "https://models.example.test/v1"
    assert exported["path"] == str(tmp_path / "engine-diagnostics.zip")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        values = connection.execute("SELECT value FROM metadata").fetchall()
    assert all("engine-settings-key" not in value[0] for value in values)


def test_engine_connection_check_emits_terminal_lifecycle_events(tmp_path, monkeypatch):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.open(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    emitted: list[tuple[str, dict[str, object]]] = []

    class FakeTerminalGateway:
        def analyze(self, request, *, on_event, is_cancelled):
            assert is_cancelled is not None
            assert request.operation == "model_capability_analysis_streaming"
            assert request.supports_streaming is True
            on_event(
                DesktopTerminalModelEvent(
                    call_id="call-1",
                    attempt=1,
                    status="awaiting_model_result",
                    elapsed_seconds=180,
                )
            )
            return DesktopModelResult("call-1", '{"status":"ok"}', 1)

    monkeypatch.setattr(
        desktop_engine_model_settings,
        "desktop_model_gateway_for_settings",
        lambda _kb_dir, _settings: FakeTerminalGateway(),
    )
    monkeypatch.setattr(
        server,
        "_emit_event",
        lambda kind, data: emitted.append((kind, data)),
    )

    result = server._dispatch(
        DesktopRequest(
            request_id="connection-check-1",
            method="workbench.test_model_connection",
            params={
                "provider": "custom",
                "model": "test/model",
                "api_base_url": "https://model.test/v1",
                "api_key": "test-key",
                "max_concurrent_model_calls": 1,
                "initial_timeout_seconds": 1,
            },
        ),
        cancel_event=Event(),
    )

    assert result["ok"] is True
    assert emitted == [
        (
            "model.call_lifecycle",
            {
                "call_id": "call-1",
                "attempt": 1,
                "status": "awaiting_model_result",
                "elapsed_seconds": 180,
                "failure_code": None,
                "reason": None,
                "retry_after_seconds": None,
                "operation": "unknown",
                "model_role": "default",
                "provider": "scripted",
                "model_name": "unknown",
                "execution_lane": "background",
                "attempt_id": "call-1:1",
                "request_id": "connection-check-1",
                "long_wait_threshold_seconds": 300.0,
            },
        )
    ]
    assert "private provider output" not in repr(emitted)


def test_engine_capability_check_runs_once_for_each_distinct_selected_model(tmp_path, monkeypatch):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.open(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    checked: list[tuple[str, str, bool]] = []

    class FakeTerminalGateway:
        def __init__(self, model: str) -> None:
            self.model = model

        def analyze(self, request, *, on_event, is_cancelled):
            assert is_cancelled is not None
            checked.append((self.model, request.operation, request.response_schema is not None))
            content = '{"status":"ok"}' if "analysis" in request.operation else "OK"
            return DesktopModelResult(f"call-{self.model}", content, 1)

        def stream(self, request, *, on_event, on_delta, is_cancelled):
            assert is_cancelled is not None
            checked.append((self.model, request.operation, request.response_schema is not None))
            content = "OK"
            on_delta(1, content)
            return DesktopModelResult(f"call-{self.model}", content, 1)

    monkeypatch.setattr(
        desktop_engine_model_settings,
        "desktop_model_gateway_for_settings",
        lambda _kb_dir, settings: FakeTerminalGateway(settings.model),
    )

    result = server._dispatch(
        DesktopRequest(
            request_id="capability-check-roles",
            method="workbench.test_model_connection",
            params={
                "provider": "custom",
                "model": "gpt-5-default",
                "analysis_model": "gpt-5-analysis",
                "answer_model": "answer-only-model",
                "api_base_url": "https://model.test/v1",
                "api_key": "test-key",
                "max_concurrent_model_calls": 2,
                "initial_timeout_seconds": 1,
            },
        ),
        cancel_event=Event(),
    )

    assert result["ok"] is True
    assert result["models"] == [
        "gpt-5-default",
        "gpt-5-analysis",
        "answer-only-model",
    ]
    assert checked == [
        ("gpt-5-default", "model_capability_default", False),
        ("gpt-5-analysis", "model_capability_analysis", True),
        ("answer-only-model", "model_capability_answer", False),
    ]
