"""Structured diagnostics at the Desktop Engine process and Bridge boundaries."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from openkb.desktop_failure_context import failure_context_fields
from openkb.desktop_logging import (
    configure_desktop_engine_logging,
    current_logging_settings,
    flush_desktop_engine_logging,
    log_event,
    trace_event,
)
from openkb.desktop_logging_settings import settings_from_environment
from openkb.desktop_sensitive_trace import close_active_sensitive_trace, configure_sensitive_trace

logger = logging.getLogger(__name__)
_FAILURE_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
QUIET_REQUEST_METHODS = frozenset({"workbench.import_jobs", "workbench.knowledge_reanalysis"})


@dataclass
class EngineRequestDiagnostics:
    request_id: str | int
    method: str
    quiet: bool
    started_at: float = field(default_factory=time.monotonic)

    @classmethod
    def begin(cls, request_id: str | int, method: str) -> EngineRequestDiagnostics:
        diagnostics = cls(request_id, method, method in QUIET_REQUEST_METHODS)
        if not diagnostics.quiet:
            log_event(
                logger,
                logging.DEBUG,
                "engine_request_started",
                "A Desktop Bridge request started.",
                component="bridge",
                fields={"request_id": request_id, "method": method},
            )
        return diagnostics

    def _elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started_at) * 1000)

    def completed(self) -> None:
        if self.quiet:
            trace_event(
                logger,
                "engine_poll_observed",
                "A successful poll request was observed.",
                component="bridge",
                fields={
                    "request_id": self.request_id,
                    "method": self.method,
                    "elapsed_ms": self._elapsed_ms(),
                },
                dedupe=True,
            )
            return
        log_event(
            logger,
            logging.INFO,
            "engine_request_completed",
            "A Desktop Bridge request completed.",
            component="bridge",
            fields={
                "request_id": self.request_id,
                "method": self.method,
                "outcome": "succeeded",
                "elapsed_ms": self._elapsed_ms(),
            },
        )

    def typed_failure(self, error: BaseException) -> None:
        error_code = str(getattr(error, "code", "engine_request_failed"))
        if self._log_propagated_failure(error, error_code):
            return
        fields = self._failure_fields(error, error_code)
        log_event(
            logger,
            logging.WARNING,
            "engine_request_failed",
            "A Desktop Bridge request failed.",
            component="bridge",
            fields=fields,
            terminal=True,
        )

    def _log_propagated_failure(self, error: BaseException, error_code: str) -> bool:
        failure_event_id = _failure_event_id_in_chain(error)
        if failure_event_id is not None:
            trace_event(
                logger,
                "failure_propagated",
                "A domain failure propagated through the Desktop Bridge.",
                component="bridge",
                fields={
                    "failure_event_id": failure_event_id,
                    "request_id": self.request_id,
                    "method": self.method,
                    "error_code": error_code,
                    "elapsed_ms": self._elapsed_ms(),
                },
            )
            return True
        return False

    def unexpected_failure(self, error: BaseException) -> None:
        if self._log_propagated_failure(error, "engine_request_failed"):
            return
        log_event(
            logger,
            logging.ERROR,
            "engine_request_failed",
            "An unexpected Desktop Engine request failure occurred.",
            component="bridge",
            fields=self._failure_fields(error, "engine_request_failed"),
            terminal=True,
            exc_info=True,
        )

    def _failure_fields(self, error: BaseException, error_code: str) -> dict[str, object]:
        return failure_context_fields(
            error=error,
            error_code=error_code,
            component="bridge",
            stage=self.method,
            phase="request_dispatch",
            outcome="failed",
            retryable=False,
            elapsed_ms=self._elapsed_ms(),
            correlations={"request_id": self.request_id, "method": self.method},
        )


def _failure_event_id_in_chain(error: BaseException) -> str | None:
    """Find a canonical Failure Owner through sanitized domain wrappers."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(visited) < 16:
        visited.add(id(current))
        candidate = getattr(current, "failure_event_id", None)
        if isinstance(candidate, str) and _FAILURE_EVENT_ID_PATTERN.fullmatch(candidate):
            return candidate
        current = current.__cause__ or current.__context__
    return None


def initialize_engine_diagnostics(*, app_version: str) -> bool:
    settings = settings_from_environment()
    log_path = configure_desktop_engine_logging(settings)
    capture = configure_sensitive_trace(
        current_logging_settings(),
        app_version=app_version,
        build=os.environ.get("OPENKB_BUILD_ID", "unknown"),
    )
    if settings.trace_components and capture is None:
        stop_file = settings.sensitive_trace_stop_file
        try:
            if stop_file is not None:
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.touch(exist_ok=True)
        except OSError:
            pass
        log_event(
            logger,
            logging.WARNING,
            "sensitive_trace_unavailable",
            "Sensitive Trace could not start; logging fell back to WARN.",
            component="runtime",
            fields={"warning_code": "sensitive_trace_unavailable", "effective_level": "WARN"},
            terminal=True,
        )
    log_event(
        logger,
        logging.INFO,
        "engine_started",
        "OpenKB Desktop Engine started.",
        component="runtime",
    )
    return log_path is not None


def log_engine_runtime_failure(error: BaseException) -> str:
    fields = failure_context_fields(
        error=error,
        error_code="engine_runtime_failed",
        component="runtime",
        stage="engine_runtime",
        phase="serve",
        outcome="failed",
        retryable=False,
    )
    log_event(
        logger,
        logging.ERROR,
        "engine_runtime_failed",
        "OpenKB Desktop Engine stopped unexpectedly.",
        component="runtime",
        fields=fields,
        terminal=True,
        exc_info=True,
    )
    flush_desktop_engine_logging()
    close_active_sensitive_trace("runtime_failed")
    return str(fields["failure_event_id"])


def log_engine_stopped() -> None:
    log_event(
        logger,
        logging.INFO,
        "engine_stopped",
        "OpenKB Desktop Engine stopped.",
        component="runtime",
    )
    flush_desktop_engine_logging()
    close_active_sensitive_trace("runtime_stopped")
