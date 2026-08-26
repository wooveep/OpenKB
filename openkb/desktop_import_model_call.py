"""Import-owned Model Call execution and durable lifecycle recording."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from openkb.desktop_import_model_ledger import DesktopImportModelLedger
from openkb.desktop_import_store import DesktopImportStore, ImportJobState
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
)

logger = logging.getLogger(__name__)


def run_import_model_call(
    *,
    gateway: DesktopModelGateway,
    ledger: DesktopImportModelLedger,
    store: DesktopImportStore,
    state: ImportJobState,
    stage: str,
    request: DesktopModelRequest,
    is_cancelled: Callable[[], bool],
) -> DesktopModelResult:
    """Run one call while persisting only sanitized lifecycle metadata."""
    request = replace(
        request,
        job_id=state.job_id,
        stage_run_id=state.stage_ids[stage],
    )

    def record_attempt(event: object) -> None:
        lifecycle = normalize_model_event(event)
        logger.info(
            "import_model_attempt job_id=%s document=%r stage=%s call_id=%s "
            "attempt=%s status=%s elapsed_seconds=%.1f error_code=%s",
            state.job_id,
            state.source.name,
            stage,
            lifecycle.call_id,
            lifecycle.attempt,
            lifecycle.lifecycle_status,
            lifecycle.elapsed_seconds,
            lifecycle.error_code,
        )
        ledger.record_attempt(
            job_id=state.job_id,
            stage_run_id=state.stage_ids[stage],
            operation=request.operation,
            event=event,
        )
        store.emit_stage(
            state,
            stage,
            "running",
            80,
            error_code=lifecycle.error_code,
        )

    return gateway.analyze(
        request,
        on_event=record_attempt,
        is_cancelled=is_cancelled,
    )


def quarantine_import_model_call(
    *,
    ledger: DesktopImportModelLedger,
    store: DesktopImportStore,
    state: ImportJobState,
    stage: str,
    error: DesktopModelCallError,
) -> str:
    """Persist one terminal call failure and return its stable public Import code."""
    ledger.quarantine(
        job_id=state.job_id,
        stage_run_id=state.stage_ids[stage],
        stage=stage,
        call_id=error.call_id,
        failure=error.failure,
        attempt_count=error.attempt_count,
    )
    return (
        "model_response_invalid"
        if error.failure.code == "model_response_invalid"
        else "document_quarantined"
    )
