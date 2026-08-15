"""Focused behavior checks for Desktop model defaults and diagnostic exports."""

from __future__ import annotations

import io
import sqlite3
import zipfile

from openkb import desktop_model_transport
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine import DesktopEngineServer, DesktopRequest
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_model_gateway import DesktopModelGateway, DesktopModelRequest
from openkb.desktop_model_settings import (
    read_desktop_model_settings,
    save_desktop_model_settings,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _create_desktop_kb(kb_dir):
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Desktop KB")
    return kb_dir


def test_model_defaults_store_only_an_environment_reference_and_drive_the_gateway(
    tmp_path, monkeypatch
):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    monkeypatch.setenv("OPENKB_DESKTOP_TEST_KEY", "do-not-persist-this-key")
    saved = save_desktop_model_settings(
        kb_dir,
        model="test/model",
        credential_reference="env:OPENKB_DESKTOP_TEST_KEY",
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

    assert saved.credential_available
    assert read_desktop_model_settings(kb_dir) == saved
    assert gateway is not None
    assert (
        gateway.analyze(
            DesktopModelRequest("document_analysis", "source.txt", "source"),
            on_event=lambda _event: None,
        ).content
        == "complete"
    )
    assert calls == [("test/model", "do-not-persist-this-key", 25.0)]
    assert "do-not-persist-this-key" not in (kb_dir / ".openkb" / "config.yaml").read_text()
    assert "do-not-persist-this-key" not in (
        kb_dir / ".openkb" / "state.sqlite3"
    ).read_bytes().decode("latin-1")


def test_diagnostic_bundle_is_explicit_and_redacts_source_model_and_credential_content(
    tmp_path, monkeypatch
):
    kb_dir = _create_desktop_kb(tmp_path / "desktop-kb")
    monkeypatch.setenv("OPENKB_DIAGNOSTIC_TEST_KEY", "diagnostic-credential-secret")
    save_desktop_model_settings(
        kb_dir,
        model="test/model",
        credential_reference="env:OPENKB_DIAGNOSTIC_TEST_KEY",
        max_concurrent_model_calls=1,
        initial_timeout_seconds=20,
    )
    source = tmp_path / "private-source.txt"
    source.write_text("private-source-content", encoding="utf-8")
    DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(lambda _request, _timeout: "private-model-response"),
    ).import_text(source)

    destination = tmp_path / "desktop-diagnostics.zip"
    bundle = DesktopDiagnosticBundleService(kb_dir).export(destination)

    assert bundle.path == str(destination)
    assert set(bundle.files) == {
        "manifest.json",
        "model-settings.json",
        "import-jobs.json",
        "model-calls.json",
        "graph-diagnostics.json",
        "integrity.json",
    }
    with zipfile.ZipFile(destination) as archive:
        content = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "private-source-content" not in content
    assert "private-model-response" not in content
    assert "diagnostic-credential-secret" not in content
    assert "OPENKB_DIAGNOSTIC_TEST_KEY" in content


def test_engine_settings_routes_do_not_accept_a_credential_value(tmp_path):
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
                "model": "test/model",
                "credential_reference": "env:OPENKB_DESKTOP_TEST_KEY",
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

    assert saved["credential_reference"] == "env:OPENKB_DESKTOP_TEST_KEY"
    assert saved["credential_available"] is False
    assert exported["path"] == str(tmp_path / "engine-diagnostics.zip")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        values = connection.execute("SELECT value FROM metadata").fetchall()
    assert all("OPENKB_DESKTOP_TEST_KEY" not in value[0] for value in values)
