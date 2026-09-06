"""Behavior checks for the support-safe Desktop Engine application log."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openkb.diagnostics.failure_context import failure_kind_for_code
from openkb.diagnostics.handler import DiagnosticJsonFormatter
from openkb.diagnostics.imports import ImportStageDiagnostics
from openkb.diagnostics.logging import (
    TRACE_LEVEL,
    configure_desktop_engine_logging,
    flush_desktop_engine_logging,
    log_event,
    shutdown_desktop_engine_logging_for_tests,
)
from openkb.diagnostics.settings import (
    DiagnosticLoggingSettings,
    component_for_logger,
    settings_from_environment,
)


@pytest.fixture(autouse=True)
def _reset_desktop_logging() -> None:
    shutdown_desktop_engine_logging_for_tests()
    yield
    shutdown_desktop_engine_logging_for_tests()


def _future_expiry() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")


def _records(path: Path) -> list[dict[str, object]]:
    flush_desktop_engine_logging()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_logging_settings_default_to_warn_and_support_component_overrides() -> None:
    default = settings_from_environment({"OPENKB_RUNTIME_SESSION_ID": "session-1"})
    overridden = settings_from_environment(
        {
            "OPENKB_RUNTIME_SESSION_ID": "session-2",
            "OPENKB_LOG_LEVEL": "info",
            "OPENKB_LOG_COMPONENT_LEVELS": json.dumps({"import": "debug", "model": "warning"}),
        }
    )

    assert default.level_name == "WARN"
    assert default.effective_level("runtime") == logging.WARNING
    assert overridden.level_name == "INFO"
    assert overridden.effective_level("import") == logging.DEBUG
    assert overridden.effective_level("model") == logging.WARNING
    assert overridden.effective_level("retrieval") == logging.INFO


@pytest.mark.parametrize(
    ("error_code", "failure_kind"),
    [
        ("model_network_transient", "network_failure"),
        ("model_authentication_failed", "provider_failure"),
        ("model_response_invalid", "model_result_failure"),
        ("reasoning_only_result", "model_result_failure"),
        ("document_ir_invalid", "parser_failure"),
        ("invalid_pdf_document", "parser_failure"),
        ("empty_text_document", "parser_failure"),
    ],
)
def test_failure_codes_preserve_actionable_failure_kinds(
    error_code: str, failure_kind: str
) -> None:
    assert failure_kind_for_code(error_code) == failure_kind


@pytest.mark.parametrize(
    "logger_name",
    [
        "openkb.parsers.document",
        "openkb.parsers.legacy_office",
        "openkb.parsers.runtime",
        "openkb.parsers.pdf",
        "openkb.parsers.presentation",
        "openkb.parsers.spreadsheet",
    ],
)
def test_parser_modules_share_the_parser_component(logger_name: str) -> None:
    assert component_for_logger(logger_name) == "parser"


@pytest.mark.parametrize(
    ("logger_name", "component"),
    [
        ("openkb.engine.page_tree_enrichment", "page_tree"),
        ("openkb.engine.knowledge_graph", "knowledge"),
        ("openkb.engine.knowledge_reanalysis", "knowledge"),
        ("openkb.documents.missing_sources", "projection"),
        ("openkb.knowledge.pages.okf_projection", "projection"),
    ],
)
def test_background_workers_use_their_domain_component(logger_name: str, component: str) -> None:
    assert component_for_logger(logger_name) == component


def test_trace_requires_bounded_sensitive_authorization() -> None:
    rejected = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "TRACE",
            "OPENKB_ALLOW_SENSITIVE_TRACE": "false",
            "OPENKB_RUNTIME_SESSION_ID": "session-rejected",
        }
    )
    accepted = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_LOG_COMPONENT_LEVELS": '{"model":"trace"}',
            "OPENKB_ALLOW_SENSITIVE_TRACE": "true",
            "OPENKB_SENSITIVE_TRACE_EXPIRES_AT": _future_expiry(),
            "OPENKB_SENSITIVE_TRACE_CAPTURE_ID": "capture-1",
            "OPENKB_RUNTIME_SESSION_ID": "session-accepted",
        }
    )

    assert rejected.level_name == "WARN"
    assert rejected.component_levels == {}
    assert rejected.effective_level("model") == logging.WARNING
    assert rejected.warnings == ("sensitive_trace_authorization_invalid",)
    assert accepted.effective_level("model") == TRACE_LEVEL
    assert accepted.trace_components == ("model",)
    assert accepted.sensitive_trace_enabled


def test_trace_expiry_requires_an_explicit_utc_timestamp() -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "TRACE",
            "OPENKB_ALLOW_SENSITIVE_TRACE": "true",
            "OPENKB_SENSITIVE_TRACE_EXPIRES_AT": "2026-08-27T09:00:00+08:00",
            "OPENKB_SENSITIVE_TRACE_CAPTURE_ID": "capture-offset",
        }
    )

    assert settings.level_name == "WARN"
    assert settings.warnings == ("sensitive_trace_authorization_invalid",)


def test_invalid_normalized_contract_fails_closed_to_all_warn() -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "DEBUG",
            "OPENKB_LOG_COMPONENT_LEVELS": '{"import":"DEBUG","model":"verbose"}',
            "OPENKB_RUNTIME_SESSION_ID": "session-invalid",
        }
    )

    assert settings.level_name == "WARN"
    assert settings.component_levels == {}
    assert settings.effective_level("import") == logging.WARNING
    assert "logging_component_override_invalid" in settings.warnings


def test_trace_expiry_or_stop_file_immediately_falls_back_to_warn(tmp_path: Path) -> None:
    stop_file = tmp_path / "stop"
    now = datetime.now(UTC)
    settings = DiagnosticLoggingSettings(
        level_name="TRACE",
        component_levels={},
        runtime_session_id="session",
        allow_sensitive_trace=True,
        sensitive_trace_expires_at=now + timedelta(minutes=10),
        sensitive_trace_capture_id="capture",
        sensitive_trace_root=tmp_path,
        sensitive_trace_stop_file=stop_file,
    )

    assert settings.effective_level("model", now=now) == TRACE_LEVEL
    stop_file.touch()
    assert settings.effective_level("model", now=now) == logging.WARNING
    assert not settings.sensitive_trace_enabled_at(now)
    assert settings.effective_level("model", now=now + timedelta(minutes=11)) == logging.WARNING


def test_json_log_filters_levels_and_never_formats_legacy_arguments(tmp_path: Path) -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_RUNTIME_SESSION_ID": "session-safe",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )
    path = configure_desktop_engine_logging(settings)
    assert path is not None
    logger = logging.getLogger("openkb.test_logging")

    logger.info("legacy_info secret=%s", "SOURCE-SENTINEL")
    logger.warning("legacy_failure secret=%s", "CREDENTIAL-SENTINEL")
    log_event(
        logger,
        logging.WARNING,
        "model_call_failed",
        "Model provider request failed.",
        component="model",
        terminal=True,
        fields={
            "job_id": "job-1",
            "error_code": "provider_http_error",
            "path_kind": "absolute",
            "prompt": "PROMPT-SENTINEL",
            "raw_path": "PATH-SENTINEL",
        },
    )

    records = _records(path)
    serialized = json.dumps(records)
    assert [record["event"] for record in records] == [
        "legacy_failure",
        "model_call_failed",
    ]
    assert "SOURCE-SENTINEL" not in serialized
    assert "CREDENTIAL-SENTINEL" not in serialized
    assert "PROMPT-SENTINEL" not in serialized
    assert "PATH-SENTINEL" not in serialized
    assert records[-1]["job_id"] == "job-1"
    assert records[-1]["error_code"] == "provider_http_error"
    assert records[-1]["path_kind"] == "absolute"
    assert records[-1]["schema_version"] == 1
    assert records[-1]["runtime_session_id"] == "session-safe"
    assert records[-1]["component"] == "model"


def test_json_log_includes_only_sanitized_traceback_frames(tmp_path: Path) -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_RUNTIME_SESSION_ID": "session-stack",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )
    path = configure_desktop_engine_logging(settings)
    assert path is not None
    logger = logging.getLogger("openkb.test_logging")

    try:
        raise RuntimeError("EXCEPTION-MESSAGE-SENTINEL")
    except RuntimeError:
        log_event(
            logger,
            logging.ERROR,
            "runtime_failed",
            "Desktop Engine stopped unexpectedly.",
            component="runtime",
            terminal=True,
            exc_info=True,
        )

    [record] = _records(path)
    serialized = json.dumps(record)
    assert "EXCEPTION-MESSAGE-SENTINEL" not in serialized
    assert record["error_type"] == "RuntimeError"
    assert record["stack"][-1]["function"] == (
        "test_json_log_includes_only_sanitized_traceback_frames"
    )
    assert "filename" not in serialized


def test_json_formatter_normalizes_boolean_exc_info_from_frozen_runtime() -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_RUNTIME_SESSION_ID": "session-frozen-stack",
        }
    )
    formatter = DiagnosticJsonFormatter(settings)

    try:
        raise RuntimeError("FROZEN-EXCEPTION-MESSAGE-SENTINEL")
    except RuntimeError:
        record = logging.LogRecord(
            name="openkb.test_logging",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="runtime_failed",
            args=(),
            exc_info=True,  # type: ignore[arg-type]
        )
        payload = json.loads(formatter.format(record))

    serialized = json.dumps(payload)
    assert payload["error_type"] == "RuntimeError"
    assert payload["stack"][-1]["function"] == (
        "test_json_formatter_normalizes_boolean_exc_info_from_frozen_runtime"
    )
    assert "FROZEN-EXCEPTION-MESSAGE-SENTINEL" not in serialized


def test_configure_migrates_plaintext_log_before_writing_json(tmp_path: Path) -> None:
    old_path = tmp_path / "openkb-engine.log"
    old_path.write_text("old plaintext with SECRET\n", encoding="utf-8")
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_RUNTIME_SESSION_ID": "session-migration",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )

    path = configure_desktop_engine_logging(settings)
    assert path == old_path
    log_event(
        logging.getLogger("openkb.engine.server"),
        logging.WARNING,
        "logging_ready",
        "Structured logging is ready.",
        component="runtime",
    )

    records = _records(path)
    migrated = list(tmp_path.glob("openkb-engine.legacy-*.log"))
    assert len(migrated) == 1
    assert migrated[0].read_text(encoding="utf-8") == "old plaintext with SECRET\n"
    assert records[0]["event"] == "logging_ready"


def test_component_level_enables_debug_without_enabling_other_components(tmp_path: Path) -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_LOG_COMPONENT_LEVELS": '{"import":"DEBUG"}',
            "OPENKB_RUNTIME_SESSION_ID": "session-components",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )
    path = configure_desktop_engine_logging(settings)
    assert path is not None
    logger = logging.getLogger("openkb.test_logging")

    log_event(
        logger,
        logging.DEBUG,
        "import_decision",
        "Import decision recorded.",
        component="import",
    )
    log_event(
        logger,
        logging.DEBUG,
        "model_decision",
        "Model decision recorded.",
        component="model",
    )

    assert [record["event"] for record in _records(path)] == ["import_decision"]


def test_import_stage_debug_records_bounded_lifecycle_and_timing(tmp_path: Path) -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "WARN",
            "OPENKB_LOG_COMPONENT_LEVELS": '{"import":"DEBUG","parser":"DEBUG"}',
            "OPENKB_RUNTIME_SESSION_ID": "session-stage-lifecycle",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )
    path = configure_desktop_engine_logging(settings)
    assert path is not None
    ticks = iter((10.0, 10.125, 20.0, 20.4))
    diagnostics = ImportStageDiagnostics(clock=lambda: next(ticks))

    diagnostics(
        {
            "job_id": "job-1",
            "stage_run_id": "stage-parser",
            "stage": "document_ir",
            "status": "running",
        }
    )
    diagnostics(
        {
            "job_id": "job-1",
            "stage_run_id": "stage-parser",
            "stage": "document_ir",
            "status": "completed",
        }
    )
    diagnostics(
        {
            "job_id": "job-1",
            "stage_run_id": "stage-import",
            "stage": "evidence",
            "status": "running",
        }
    )
    diagnostics(
        {
            "job_id": "job-1",
            "stage_run_id": "stage-import",
            "stage": "evidence",
            "status": "completed",
        }
    )

    records = _records(path)
    assert [record["event"] for record in records] == [
        "import_stage_started",
        "import_stage_finished",
        "import_stage_started",
        "import_stage_finished",
    ]
    assert [record["component"] for record in records] == [
        "parser",
        "parser",
        "import",
        "import",
    ]
    assert records[1]["elapsed_ms"] == 125
    assert records[3]["elapsed_ms"] == 400


def test_deduplication_ignores_unique_poll_request_ids(tmp_path: Path) -> None:
    settings = settings_from_environment(
        {
            "OPENKB_LOG_LEVEL": "DEBUG",
            "OPENKB_RUNTIME_SESSION_ID": "session-poll",
            "OPENKB_LOG_DIR": str(tmp_path),
        }
    )
    path = configure_desktop_engine_logging(settings)
    assert path is not None
    logger = logging.getLogger("openkb.diagnostics.engine")

    for request_id in ("poll-1", "poll-2"):
        log_event(
            logger,
            logging.DEBUG,
            "engine_poll_observed",
            "A successful poll request was observed.",
            component="bridge",
            fields={
                "request_id": request_id,
                "method": "workbench.import_jobs",
            },
            dedupe=True,
        )

    records = _records(path)
    assert [record["event"] for record in records] == ["engine_poll_observed"]
    assert records[0]["request_id"] == "poll-1"
