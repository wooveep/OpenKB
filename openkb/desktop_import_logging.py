"""Failure Owner diagnostics for Desktop Import Jobs."""

from __future__ import annotations

import logging
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from typing import Literal

from openkb.desktop_failure_context import failure_context_fields
from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_store import IMPORT_STAGES, ImportJobState
from openkb.desktop_logging import log_event, trace_event
from openkb.desktop_model_gateway import DesktopModelCallError
from openkb.desktop_sensitive_trace import (
    record_sensitive_trace_failure,
    sensitive_trace_component_enabled,
)

logger = logging.getLogger(__name__)

StageProgressCallback = Callable[[dict[str, object]], None]
_PARSER_STAGES = frozenset({"raw_asset", "document_ir"})
_TERMINAL_STAGE_STATUSES = frozenset({"completed", "failed", "skipped", "paused", "cancelled"})


class ImportStageDiagnostics:
    """Turn noisy progress callbacks into one bounded DEBUG lifecycle per stage."""

    def __init__(
        self,
        downstream: StageProgressCallback | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._downstream = downstream
        self._clock = clock
        self._started_at: dict[str, float] = {}

    def __call__(self, data: dict[str, object]) -> None:
        try:
            self._record(data)
        except Exception:
            # Diagnostics never replace a durable stage transition or callback.
            pass
        if self._downstream is not None:
            self._downstream(data)

    def _record(self, data: Mapping[str, object]) -> None:
        job_id = data.get("job_id")
        stage_run_id = data.get("stage_run_id")
        stage = data.get("stage")
        status = data.get("status")
        if not isinstance(job_id, str) or not job_id:
            return
        if not isinstance(stage_run_id, str) or not stage_run_id:
            return
        if not isinstance(stage, str) or not stage:
            return
        if not isinstance(status, str):
            return
        component = "parser" if stage in _PARSER_STAGES else "import"
        fields: dict[str, object] = {
            "job_id": job_id,
            "stage_run_id": stage_run_id,
            "stage": stage,
            "status": status,
        }
        if status == "running":
            if stage_run_id in self._started_at:
                return
            self._started_at[stage_run_id] = self._clock()
            log_event(
                logger,
                logging.DEBUG,
                "import_stage_started",
                "A document import stage started.",
                component=component,
                fields=fields,
            )
            return
        if status not in _TERMINAL_STAGE_STATUSES:
            return
        started_at = self._started_at.pop(stage_run_id, None)
        if started_at is not None:
            fields["elapsed_ms"] = round(max(0.0, self._clock() - started_at) * 1000)
        error_code = data.get("error_code")
        if isinstance(error_code, str) and error_code:
            fields["error_code"] = error_code
        log_event(
            logger,
            logging.DEBUG,
            "import_stage_finished",
            "A document import stage finished.",
            component=component,
            fields=fields,
        )


def _source_observations(state: ImportJobState) -> dict[str, object]:
    try:
        source_size = state.source.stat().st_size
    except OSError:
        source_size = None
    return {
        "source_extension": state.source.suffix.lower()[:32] or "none",
        "source_size_bytes": source_size,
        "path_kind": "absolute" if state.source.is_absolute() else "relative",
    }


def _last_completed_stage(stage: str) -> str:
    try:
        index = IMPORT_STAGES.index(stage)
    except ValueError:
        return "unknown"
    return IMPORT_STAGES[index - 1] if index else "none"


def log_model_analysis_quarantine(
    state: ImportJobState,
    stage: str,
    error: DesktopModelCallError,
) -> None:
    """Reference the Model Call Failure Owner without a duplicate warning."""
    trace_event(
        logger,
        "failure_propagated",
        "A terminal model failure propagated into Import quarantine.",
        component="import",
        fields={
            **error.diagnostic_context,
            "failure_event_id": error.failure_event_id,
            "job_id": state.job_id,
            "stage": stage,
            "outcome": "quarantined",
        },
    )


def log_import_failure(
    state: ImportJobState,
    stage: str,
    error: DesktopImportError,
    *,
    outcome: Literal["failed", "quarantined"],
    include_traceback: bool = False,
) -> None:
    """Own one self-contained, support-safe terminal Import Job failure."""
    component = "parser" if stage in {"raw_asset", "document_ir"} else "import"
    fields = failure_context_fields(
        error=error,
        error_code=error.code,
        component=component,
        stage=stage,
        phase="stage_execution",
        outcome=outcome,
        retryable=False,
        attempt=error.attempt_count,
        correlations={
            "job_id": state.job_id,
            "stage_run_id": state.stage_ids.get(stage),
        },
        observations={
            **_source_observations(state),
            "last_completed_stage": _last_completed_stage(stage),
            **error.diagnostic_context,
        },
    )
    log_event(
        logger,
        logging.WARNING,
        f"import_{outcome}",
        f"A document import was {outcome}.",
        component=component,
        fields=fields,
        terminal=True,
        exc_info=include_traceback,
    )
    if sensitive_trace_component_enabled(component):
        try:
            exception_type, exception, exception_traceback = sys.exc_info()
            stack = (
                "".join(traceback.format_exception(exception_type, exception, exception_traceback))
                if exception is not None
                else repr(error)
            )
            record_sensitive_trace_failure(
                f"import_{outcome}",
                metadata={
                    "failure_event_id": fields["failure_event_id"],
                    "job_id": state.job_id,
                    "stage": stage,
                    "source_path": str(state.source),
                    "error_code": error.code,
                    "error": str(error),
                },
                payloads={"exception-stack": stack, "source-path": str(state.source)},
            )
        except Exception:
            pass
