"""Containment tests for explicitly authorized Sensitive Trace Captures."""

from __future__ import annotations

import hashlib
import json
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import openkb.diagnostics.sensitive_trace as sensitive_trace_runtime
from openkb.diagnostics.engine import log_engine_stopped
from openkb.diagnostics.logging import (
    configure_desktop_engine_logging,
    flush_desktop_engine_logging,
    shutdown_desktop_engine_logging_for_tests,
)
from openkb.diagnostics.sensitive_trace import (
    MAX_EVENT_METADATA_BYTES,
    MAX_PAYLOAD_BYTES,
    SensitiveTraceCapture,
    configure_sensitive_trace,
    reset_sensitive_trace_for_tests,
)
from openkb.diagnostics.settings import DiagnosticLoggingSettings
from openkb.models.gateway import (
    DesktopModelCallError,
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelRequest,
    DesktopModelTransportError,
)


def _settings(tmp_path: Path, *, capture_id: str = "capture-1") -> DiagnosticLoggingSettings:
    return DiagnosticLoggingSettings(
        level_name="WARN",
        component_levels={"model": "TRACE"},
        runtime_session_id="runtime-1",
        allow_sensitive_trace=True,
        sensitive_trace_expires_at=datetime.now(UTC) + timedelta(hours=1),
        sensitive_trace_capture_id=capture_id,
        sensitive_trace_root=tmp_path / "sensitive-traces",
        sensitive_trace_stop_file=tmp_path / "sensitive-traces" / capture_id / ".stop",
    )


def test_capture_is_not_created_without_effective_trace(tmp_path: Path) -> None:
    settings = DiagnosticLoggingSettings(
        level_name="WARN",
        runtime_session_id="runtime",
        sensitive_trace_root=tmp_path,
    )

    assert SensitiveTraceCapture.open(settings, app_version="test", build="test") is None
    assert list(tmp_path.iterdir()) == []


def test_failure_capture_writes_manifest_event_and_content_addressed_payload(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    capture = SensitiveTraceCapture.open(settings, app_version="0.1.0", build="dev")
    assert capture is not None

    result = capture.record_failure(
        "model_call_failed",
        metadata={"call_id": "call-1", "raw_path": r"C:\private\document.pdf"},
        payloads={"assembled-response": "RAW MODEL RESPONSE"},
    )

    assert result is not None
    capture_dir = settings.sensitive_trace_root / "capture-1"  # type: ignore[operator]
    manifest = json.loads((capture_dir / "capture.json").read_text(encoding="utf-8"))
    [event] = [
        json.loads(line)
        for line in (capture_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    [payload] = manifest["payloads"]
    payload_path = capture_dir / payload["relative_path"]

    assert stat.S_IMODE(capture_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600
    assert payload_path.read_bytes() == b"RAW MODEL RESPONSE"
    assert payload["sha256"] == hashlib.sha256(b"RAW MODEL RESPONSE").hexdigest()
    assert event["metadata"]["raw_path"] == r"C:\private\document.pdf"
    assert event["payloads"][0]["sha256"] == payload["sha256"]
    assert manifest["capture_id"] == "capture-1"
    assert manifest["app_version"] == "0.1.0"
    assert manifest["configured_level"] == "WARN"
    assert manifest["component_levels"] == {"model": "TRACE"}
    assert manifest["trace_components"] == ["model"]
    assert manifest["effective_levels"]["model"] == "TRACE"
    assert manifest["effective_levels"]["import"] == "WARN"
    assert manifest["complete"] is False
    assert "username" not in json.dumps(manifest).lower()
    assert "machine" not in json.dumps(manifest).lower()


def test_large_payload_keeps_first_and_last_mebibyte_and_full_digest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    capture = SensitiveTraceCapture.open(settings, app_version="test", build="test")
    assert capture is not None
    raw = b"A" * (1024 * 1024) + b"MIDDLE" + b"Z" * (1024 * 1024)

    result = capture.record_failure(
        "provider_failure",
        metadata={"call_id": "call-large"},
        payloads={"response": raw},
    )

    assert result is not None
    [payload] = result.payloads
    stored = (capture.directory / payload.relative_path).read_bytes()
    assert len(stored) == MAX_PAYLOAD_BYTES
    assert stored == b"A" * (1024 * 1024) + b"Z" * (1024 * 1024)
    assert payload.full_bytes == len(raw)
    assert payload.truncated
    assert payload.sha256 == hashlib.sha256(raw).hexdigest()


def test_identical_payload_is_deduplicated_within_capture(tmp_path: Path) -> None:
    capture = SensitiveTraceCapture.open(_settings(tmp_path), app_version="test", build="test")
    assert capture is not None

    first = capture.record_failure("first_failure", payloads={"response": "same"})
    second = capture.record_failure("second_failure", payloads={"response": "same"})

    assert first is not None and second is not None
    assert first.payloads[0].relative_path == second.payloads[0].relative_path
    assert len(list((capture.directory / "payloads").iterdir())) == 1
    manifest = json.loads((capture.directory / "capture.json").read_text(encoding="utf-8"))
    assert manifest["event_count"] == 2
    assert manifest["payload_count"] == 1


def test_stop_marks_manifest_and_prevents_more_payloads(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    capture = SensitiveTraceCapture.open(settings, app_version="test", build="test")
    assert capture is not None
    capture.stop("user_requested")

    assert capture.record_failure("late_failure", payloads={"response": "late"}) is None
    manifest = json.loads((capture.directory / "capture.json").read_text(encoding="utf-8"))
    assert manifest["stop_reason"] == "user_requested"
    assert manifest["ended_at"] is not None
    assert manifest["complete"] is True
    assert settings.sensitive_trace_stop_file is not None
    assert settings.sensitive_trace_stop_file.exists()


def test_expiry_cleanup_removes_active_plaintext_capture(tmp_path: Path) -> None:
    reset_sensitive_trace_for_tests()
    capture = configure_sensitive_trace(_settings(tmp_path), app_version="test", build="test")
    assert capture is not None and capture.directory.is_dir()

    sensitive_trace_runtime._expire_active_capture(capture)

    assert not capture.directory.exists()
    assert not sensitive_trace_runtime.sensitive_trace_is_active()
    reset_sensitive_trace_for_tests()


def test_stopped_capture_is_still_deleted_at_authorization_expiry(tmp_path: Path) -> None:
    reset_sensitive_trace_for_tests()
    settings = replace(
        _settings(tmp_path), sensitive_trace_expires_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    capture = configure_sensitive_trace(settings, app_version="test", build="test")
    assert capture is not None
    capture.stop()

    deadline = time.monotonic() + 3
    while capture.directory.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not capture.directory.exists()
    reset_sensitive_trace_for_tests()


def test_ordinary_startup_prunes_expired_capture_directories(tmp_path: Path) -> None:
    root = tmp_path / "sensitive-traces"
    expired = root / "expired-capture"
    expired.mkdir(parents=True)
    started_at = datetime.now(UTC) - timedelta(hours=2)
    expires_at = datetime.now(UTC) - timedelta(hours=1)
    (expired / "capture.json").write_text(
        json.dumps(
            {
                "capture_id": "expired-capture",
                "started_at": started_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    settings = DiagnosticLoggingSettings(
        level_name="WARN",
        runtime_session_id="runtime",
        sensitive_trace_root=root,
    )
    reset_sensitive_trace_for_tests()

    assert configure_sensitive_trace(settings, app_version="test", build="test") is None
    assert not expired.exists()
    reset_sensitive_trace_for_tests()


def test_open_prunes_to_two_capture_directories(tmp_path: Path) -> None:
    root = tmp_path / "sensitive-traces"
    for index in range(3):
        capture = SensitiveTraceCapture.open(
            _settings(tmp_path, capture_id=f"capture-{index}"),
            app_version="test",
            build="test",
        )
        assert capture is not None

    captures = sorted(path.name for path in root.iterdir() if path.is_dir())
    assert captures == ["capture-1", "capture-2"]

    reopened = SensitiveTraceCapture.open(
        _settings(tmp_path, capture_id="capture-2"),
        app_version="test",
        build="test",
    )
    assert reopened is not None
    assert len([path for path in root.iterdir() if path.is_dir()]) == 2


def test_event_metadata_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    capture = SensitiveTraceCapture.open(_settings(tmp_path), app_version="test", build="test")
    assert capture is not None

    result = capture.record_failure(
        "large_metadata_failure",
        metadata={"provider_detail": "X" * (MAX_EVENT_METADATA_BYTES * 2)},
    )

    assert result is not None
    [event] = [
        json.loads(line)
        for line in (capture.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads((capture.directory / "capture.json").read_text(encoding="utf-8"))
    assert event["metadata"]["truncated"] is True
    assert event["metadata"]["full_bytes"] > MAX_EVENT_METADATA_BYTES
    assert manifest["truncated_metadata_count"] == 1


def test_failed_model_call_keeps_raw_evidence_only_in_sensitive_capture(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings = replace(settings, log_directory=tmp_path / "logs")
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)
    configure_sensitive_trace(settings, app_version="test", build="test")

    response = DesktopModelProviderResponse(
        "",
        observations=DesktopModelOutputObservations(
            finish_reason="length",
            reasoning_observed=True,
            reasoning_chunk_count=1,
            reasoning_character_count=23,
            output_limit_reached=True,
        ),
        sensitive_reasoning_content="RAW-REASONING-SENTINEL",
    )
    gateway = DesktopModelGateway(lambda _request, _timeout: response)
    with pytest.raises(DesktopModelCallError):
        gateway.analyze(
            DesktopModelRequest(
                "knowledge_analysis",
                "PRIVATE-FILENAME.pdf",
                "PROMPT-SOURCE-SENTINEL",
                supports_streaming=False,
                job_id="job-1",
            ),
            on_event=lambda _event: None,
        )
    flush_desktop_engine_logging()

    application_log = (tmp_path / "logs" / "openkb-engine.log").read_text(encoding="utf-8")
    failure_record = next(
        json.loads(line)
        for line in application_log.splitlines()
        if json.loads(line)["event"] == "model_call_failed"
    )
    capture_dir = settings.sensitive_trace_root / "capture-1"  # type: ignore[operator]
    raw_payloads = b"".join(path.read_bytes() for path in (capture_dir / "payloads").iterdir())
    assert "PROMPT-SOURCE-SENTINEL" not in application_log
    assert "RAW-REASONING-SENTINEL" not in application_log
    assert "PRIVATE-FILENAME.pdf" not in application_log
    assert failure_record["failure_kind"] == "model_result_failure"
    assert failure_record["phase"] == "result_validation"
    assert failure_record["reasoning_observed"] is True
    assert failure_record["final_content_observed"] is False
    assert failure_record["output_limit_reached"] is True
    assert b"PROMPT-SOURCE-SENTINEL" in raw_payloads
    assert b"RAW-REASONING-SENTINEL" in raw_payloads

    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()


def test_provider_failure_log_retains_safe_provider_classification(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), log_directory=tmp_path / "logs")
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)

    def fail(_request: DesktopModelRequest, _timeout: float) -> str:
        raise DesktopModelTransportError(
            "authentication",
            diagnostic_type="APIStatusError",
            sensitive_detail="RAW-PROVIDER-RESPONSE-SENTINEL",
        )

    gateway = DesktopModelGateway(fail)
    with pytest.raises(DesktopModelCallError):
        gateway.analyze(
            DesktopModelRequest(
                "knowledge_analysis",
                "PRIVATE-FILENAME.pdf",
                "PROMPT-SOURCE-SENTINEL",
                supports_streaming=False,
            ),
            on_event=lambda _event: None,
        )
    flush_desktop_engine_logging()

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "openkb-engine.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(record for record in records if record["event"] == "model_call_failed")
    assert failure["failure_kind"] == "provider_failure"
    assert failure["phase"] == "provider_request"
    assert failure["provider_error_code"] == "authentication"
    assert failure["error_type"] == "APIStatusError"
    assert failure["next_action"] == "check_model_credentials"
    assert "RAW-PROVIDER-RESPONSE-SENTINEL" not in json.dumps(records)
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()


def test_malformed_provider_response_is_classified_and_captured_only_in_trace(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), log_directory=tmp_path / "logs")
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)
    capture = configure_sensitive_trace(settings, app_version="test", build="test")
    assert capture is not None

    def malformed(_request: DesktopModelRequest, _timeout: float) -> str:
        raise DesktopModelTransportError(
            "response_format",
            diagnostic_type="MalformedResponseError",
            sensitive_detail="RAW-MALFORMED-RESPONSE-SENTINEL",
        )

    with pytest.raises(DesktopModelCallError):
        DesktopModelGateway(malformed).analyze(
            DesktopModelRequest(
                "knowledge_analysis",
                "PRIVATE-FILENAME.pdf",
                "PROMPT-SOURCE-SENTINEL",
                supports_streaming=False,
            ),
            on_event=lambda _event: None,
        )
    flush_desktop_engine_logging()

    application_log = (tmp_path / "logs" / "openkb-engine.log").read_text(encoding="utf-8")
    failure = next(
        json.loads(line)
        for line in application_log.splitlines()
        if json.loads(line)["event"] == "model_call_failed"
    )
    raw_payloads = b"".join(
        path.read_bytes() for path in (capture.directory / "payloads").iterdir()
    )
    assert failure["error_code"] == "model_response_invalid"
    assert failure["failure_kind"] == "model_result_failure"
    assert failure["phase"] == "result_validation"
    assert "RAW-MALFORMED-RESPONSE-SENTINEL" not in application_log
    assert b"RAW-MALFORMED-RESPONSE-SENTINEL" in raw_payloads
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()


def test_trace_component_override_does_not_capture_other_components(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings(tmp_path),
        component_levels={"parser": "TRACE"},
        log_directory=tmp_path / "logs",
    )
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)
    capture = configure_sensitive_trace(settings, app_version="test", build="test")
    assert capture is not None

    gateway = DesktopModelGateway(lambda _request, _timeout: "")
    with pytest.raises(DesktopModelCallError):
        gateway.analyze(
            DesktopModelRequest(
                "knowledge_analysis",
                "PRIVATE-FILENAME.pdf",
                "PROMPT-SOURCE-SENTINEL",
                supports_streaming=False,
            ),
            on_event=lambda _event: None,
        )

    manifest = json.loads((capture.directory / "capture.json").read_text(encoding="utf-8"))
    assert manifest["event_count"] == 0
    assert list((capture.directory / "payloads").iterdir()) == []
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()


def test_engine_stop_closes_active_capture_manifest(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), log_directory=tmp_path / "logs")
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)
    capture = configure_sensitive_trace(settings, app_version="test", build="test")
    assert capture is not None

    log_engine_stopped()

    manifest = json.loads((capture.directory / "capture.json").read_text(encoding="utf-8"))
    assert manifest["ended_at"] is not None
    assert manifest["stop_reason"] == "runtime_stopped"
    assert manifest["complete"] is True
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()


def test_trace_request_payload_failure_does_not_change_model_outcomes(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), log_directory=tmp_path / "logs")
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
    configure_desktop_engine_logging(settings)
    configure_sensitive_trace(settings, app_version="test", build="test")

    class PayloadFailureTransport:
        def __init__(self, outcome: str | Exception) -> None:
            self.outcome = outcome

        def __call__(self, _request: DesktopModelRequest, _timeout: float) -> str:
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

        def sensitive_request_payload(self, _request: DesktopModelRequest) -> str:
            raise RuntimeError("trace serialization failed")

    request = DesktopModelRequest(
        "knowledge_analysis",
        "document.txt",
        "source material",
        supports_streaming=False,
    )
    successful = DesktopModelGateway(PayloadFailureTransport("usable output")).analyze(
        request,
        on_event=lambda _event: None,
    )
    assert successful.content == "usable output"

    failing = DesktopModelGateway(
        PayloadFailureTransport(DesktopModelTransportError("authentication"))
    )
    with pytest.raises(DesktopModelCallError) as captured:
        failing.analyze(request, on_event=lambda _event: None)
    assert captured.value.failure.code == "model_authentication_failed"
    reset_sensitive_trace_for_tests()
    shutdown_desktop_engine_logging_for_tests()
