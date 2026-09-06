"""Desktop Engine Import Job routes owned by explicit user requests."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.engine import knowledge_graph as knowledge_graph_engine
from openkb.engine import page_tree_enrichment as page_tree_enrichment_engine
from openkb.engine.model_lifecycle import emit_model_lifecycle
from openkb.importing.service import DesktopImportControl, DesktopTextImportService
from openkb.importing.types import DesktopRecoveryOverride
from openkb.models.capability_check import capability_check_request
from openkb.models.capability_verifier import (
    DesktopModelCapabilityVerificationError,
    verify_model_capability,
)
from openkb.models.settings import read_desktop_model_settings

if TYPE_CHECKING:
    from openkb.engine.protocol import DesktopRequest
    from openkb.engine.server import DesktopEngineServer


def dispatch_import_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Run one Import Job against a coordinator-owned Active-KB lease."""
    from openkb.engine.protocol import (
        DesktopRequestError,
        recovery_override_param,
        required_string_param,
    )

    with server._workspace_transition.import_job() as lease:
        if lease is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before importing a document.",
            )
        with server._workspace_requests_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            active = server._workspace.active()
            if active is None or Path(active.kb_dir).expanduser().resolve() != lease.kb_dir:
                raise DesktopRequestError(
                    "knowledge_base_switched",
                    "The Active Knowledge Base changed before the import began.",
                )
            server._begin_workspace_mutation(request, cancel_event)
            parser_mode = _parser_mode(request.params.get("parser_mode"))
            source_path: Path | None = None
            job_id: str | None = None
            recovery_override: DesktopRecoveryOverride | None = None
            if request.method == "workbench.import_text_document":
                source_path = Path(required_string_param(request, "source_path"))
            else:
                job_id = required_string_param(request, "job_id")
                if request.method != "workbench.resume_import_job":
                    recovery_override = recovery_override_param(request)
        return run_import(
            server,
            lease.kb_dir,
            request_id=str(request.request_id),
            source_path=source_path,
            job_id=job_id,
            recovery_override=recovery_override,
            control=lease.control,
            parser_mode=parser_mode,
        )


def run_import(
    server: DesktopEngineServer,
    kb_dir: Path,
    *,
    request_id: str | None,
    source_path: Path | None = None,
    job_id: str | None = None,
    recovery_override: DesktopRecoveryOverride | None = None,
    control: DesktopImportControl | None = None,
    parser_mode: str = "auto",
) -> dict[str, object]:
    """Run one job while its durable state, not this worker, remains authoritative."""
    from openkb.engine.protocol import DesktopRequestError

    control = control or DesktopImportControl()
    try:
        model_gateway = server._model_gateway_factory(kb_dir, recovery_override)
        if recovery_override is not None and recovery_override.check_and_recover:
            _check_recovery_profile(
                server,
                kb_dir,
                request_id=request_id,
                gateway=model_gateway,
                is_cancelled=lambda: control.action == "cancelled",
            )
        importer = DesktopTextImportService(
            kb_dir,
            control=control,
            on_stage_progress=lambda data: _record_import_stage(server, request_id, control, data),
            model_gateway=model_gateway,
            require_model_analysis=True,
            parser_mode=parser_mode,
        )
        if source_path is not None:
            result = importer.import_text(source_path)
        elif job_id is not None:
            result = (
                importer.recover_text(job_id, recovery_override)
                if recovery_override is not None
                else importer.resume_text(job_id)
            )
        else:
            raise DesktopRequestError("invalid_params", "An import source or job is required.")
        server._corpus_synthesis_workers.start(kb_dir, server._model_gateway_factory(kb_dir, None))
        page_tree_enrichment_engine.start_page_tree_enrichments(
            server, kb_dir, server._model_gateway_factory(kb_dir, None)
        )
        knowledge_graph_engine.start_knowledge_graph_extractions(
            server, kb_dir, server._model_gateway_factory(kb_dir, None)
        )
        return result.as_dict()
    finally:
        _release_import_control(server, control)


def pause_import_job(server: DesktopEngineServer, job_id: str) -> dict[str, object]:
    from openkb.engine.protocol import DesktopRequestError

    with server._active_lock:
        control = server._import_controls.get(job_id)
    if control is None:
        raise DesktopRequestError(
            "import_job_not_running", "Import job is not running in this Desktop Runtime."
        )
    control.request_pause()
    return {"job_id": job_id, "accepted": True}


def cancel_import_job(server: DesktopEngineServer, job_id: str) -> dict[str, object]:
    from openkb.engine.protocol import DesktopRequestError

    with server._active_lock:
        control = server._import_controls.get(job_id)
    if control is not None:
        control.request_cancel()
        return {"job_id": job_id, "accepted": True}
    active = server._workspace.active()
    if active is None:
        raise DesktopRequestError(
            "no_active_knowledge_base", "Open a Desktop Knowledge Base before cancelling."
        )
    DesktopTextImportService(Path(active.kb_dir)).cancel_paused_job(job_id)
    return {"job_id": job_id, "accepted": True}


def _record_import_stage(
    server: DesktopEngineServer,
    request_id: str | None,
    control: DesktopImportControl,
    data: dict[str, object],
) -> None:
    job_id = data.get("job_id")
    if isinstance(job_id, str):
        with server._active_lock:
            server._import_controls[job_id] = control
    server._emit_event("import.stage_progress", {"request_id": request_id, **data})


def _release_import_control(server: DesktopEngineServer, control: DesktopImportControl) -> None:
    with server._active_lock:
        for job_id, active_control in tuple(server._import_controls.items()):
            if active_control is control:
                del server._import_controls[job_id]


def _parser_mode(value: object) -> str:
    if value is None:
        return "auto"
    if isinstance(value, str) and value in {"auto", "fast", "enhanced"}:
        return value
    from openkb.engine.protocol import DesktopRequestError

    raise DesktopRequestError("invalid_params", "parser_mode must be auto, fast, or enhanced.")


def _check_recovery_profile(
    server: DesktopEngineServer,
    kb_dir: Path,
    *,
    request_id: str | None,
    gateway,
    is_cancelled,
) -> None:
    """Verify the one-time recovery profile before mutating any recovery checkpoint."""
    from openkb.engine.protocol import DesktopRequestError

    if gateway is None:
        raise DesktopRequestError(
            "recovery_model_not_configured",
            "A configured DeepSeek Analysis model is required for Check and Recover.",
        )
    profile_factory = getattr(gateway, "execution_profile_for_operation", None)
    if not callable(profile_factory):
        raise DesktopRequestError(
            "model_capability_check_failed",
            "The selected recovery model has no explicit Analysis execution profile.",
        )
    try:
        profile = profile_factory("knowledge_analysis")
    except ValueError as error:
        raise DesktopRequestError("model_capability_check_failed", str(error)) from error
    try:
        verify_model_capability(
            kb_dir,
            role="analysis",
            model=profile.model,
            profile=profile,
            gateway=gateway,
            request=capability_check_request(read_desktop_model_settings(kb_dir), profile=profile),
            on_event=lambda event: emit_model_lifecycle(
                server,
                kb_dir=kb_dir,
                request_id=request_id or "check-and-recover",
                event=event,
            ),
            is_cancelled=is_cancelled,
            reuse_verified=True,
        )
    except DesktopModelCapabilityVerificationError as error:
        message = (
            "Check and Recover was cancelled before Replan."
            if error.code == "request_cancelled"
            else error.reason
        )
        raise DesktopRequestError(error.code, message) from error
