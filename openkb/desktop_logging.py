"""Public Desktop Engine diagnostics API.

Application Logs are support-safe JSON Lines. Raw failure evidence belongs only
to :mod:`openkb.desktop_sensitive_trace`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from openkb.desktop_log_handler import HybridDiagnosticHandler, migrate_plaintext_logs
from openkb.desktop_logging_settings import (
    TRACE_LEVEL,
    DiagnosticLoggingSettings,
    settings_from_environment,
)

_ENGINE_LOG_FILE = "openkb-engine.log"
_ACTIVE_HANDLER: HybridDiagnosticHandler | None = None

logging.addLevelName(TRACE_LEVEL, "TRACE")


def desktop_application_log_directory() -> Path:
    """Return the application log location outside every knowledge base."""
    configured = os.environ.get("OPENKB_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "OpenKB" / "logs"
    return Path.home() / ".local" / "share" / "OpenKB" / "logs"


def configure_desktop_engine_logging(
    settings: DiagnosticLoggingSettings | None = None,
) -> Path | None:
    """Configure one idempotent, rotating, support-safe Engine log."""
    global _ACTIVE_HANDLER
    if _ACTIVE_HANDLER is not None:
        return _ACTIVE_HANDLER.path
    normalized = settings or settings_from_environment()
    configured_directory = normalized.log_directory or os.environ.get("OPENKB_LOG_DIR")
    if configured_directory:
        directory = Path(configured_directory).expanduser()
    else:
        directory = desktop_application_log_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _ENGINE_LOG_FILE
        migrate_plaintext_logs(path)
        handler = HybridDiagnosticHandler(path, normalized)
    except OSError:
        return None

    logger = logging.getLogger("openkb")
    logger.addHandler(handler)
    logger.setLevel(TRACE_LEVEL)
    logger.propagate = False
    _ACTIVE_HANDLER = handler
    for warning_code in normalized.warnings:
        effective_level = logging.getLevelName(normalized.effective_level("runtime"))
        if effective_level == "WARNING":
            effective_level = "WARN"
        log_event(
            logging.getLogger("openkb.desktop_logging"),
            logging.WARNING,
            "logging_configuration_warning",
            "Desktop logging configuration was normalized with a warning.",
            component="runtime",
            fields={"warning_code": warning_code, "effective_level": effective_level},
        )
    return path


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    summary: str,
    *,
    component: str,
    fields: Mapping[str, object] | None = None,
    terminal: bool = False,
    dedupe: bool = False,
    exc_info: Any = None,
) -> None:
    """Emit one structured event through the fail-closed formatter."""
    logger.log(
        level,
        event,
        extra={
            "openkb_event": event,
            "openkb_summary": summary,
            "openkb_component": component,
            "openkb_fields": dict(fields or {}),
            "openkb_terminal": terminal,
            "openkb_dedupe": dedupe,
        },
        exc_info=exc_info,
    )


def trace_event(
    logger: logging.Logger,
    event: str,
    summary: str,
    *,
    component: str,
    fields: Mapping[str, object] | None = None,
    dedupe: bool = False,
) -> None:
    log_event(
        logger,
        TRACE_LEVEL,
        event,
        summary,
        component=component,
        fields=fields,
        dedupe=dedupe,
    )


def current_logging_settings() -> DiagnosticLoggingSettings:
    if _ACTIVE_HANDLER is not None:
        return _ACTIVE_HANDLER.settings
    return settings_from_environment()


def flush_desktop_engine_logging() -> None:
    if _ACTIVE_HANDLER is not None:
        _ACTIVE_HANDLER.flush()


def shutdown_desktop_engine_logging_for_tests() -> None:
    """Detach the process singleton; intentionally public only for isolated tests."""
    global _ACTIVE_HANDLER
    if _ACTIVE_HANDLER is None:
        return
    logger = logging.getLogger("openkb")
    logger.removeHandler(_ACTIVE_HANDLER)
    _ACTIVE_HANDLER.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    _ACTIVE_HANDLER = None
