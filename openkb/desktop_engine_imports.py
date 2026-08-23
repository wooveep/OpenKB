"""Desktop Engine Import Job routes and runtime-restoration workers."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_page_tree_enrichment as page_tree_enrichment_engine
from openkb.desktop_import import DesktopImportControl, DesktopImportError, DesktopTextImportService
from openkb.desktop_import_types import DesktopRecoveryOverride

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest

logger = logging.getLogger(__name__)


def dispatch_import_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Run one Import Job against a coordinator-owned Active-KB lease."""
    from openkb.desktop_engine import (
        DesktopRequestError,
        _recovery_override_param,
        _required_path_param,
        _required_string_param,
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
            if request.method == "workbench.import_text_document":
                return run_import(
                    server,
                    lease.kb_dir,
                    request_id=str(request.request_id),
                    source_path=Path(_required_path_param(request, "source_path")),
                    control=lease.control,
                    parser_mode=parser_mode,
                )
            if request.method == "workbench.resume_import_job":
                return run_import(
                    server,
                    lease.kb_dir,
                    request_id=str(request.request_id),
                    job_id=_required_string_param(request, "job_id"),
                    control=lease.control,
                    parser_mode=parser_mode,
                )
            return run_import(
                server,
                lease.kb_dir,
                request_id=str(request.request_id),
                job_id=_required_string_param(request, "job_id"),
                recovery_override=_recovery_override_param(request),
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
    from openkb.desktop_engine import DesktopRequestError

    control = control or DesktopImportControl()
    importer = DesktopTextImportService(
        kb_dir,
        control=control,
        on_stage_progress=lambda data: _record_import_stage(server, request_id, control, data),
        model_gateway=server._model_gateway_factory(kb_dir, recovery_override),
        require_model_analysis=True,
        parser_mode=parser_mode,
    )
    try:
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
        page_tree_enrichment_engine.start_page_tree_enrichments(
            server, kb_dir, server._model_gateway_factory(kb_dir, None)
        )
        return result.as_dict()
    finally:
        _release_import_control(server, control)


def pause_import_job(server: DesktopEngineServer, job_id: str) -> dict[str, object]:
    from openkb.desktop_engine import DesktopRequestError

    with server._active_lock:
        control = server._import_controls.get(job_id)
    if control is None:
        raise DesktopRequestError(
            "import_job_not_running", "Import job is not running in this Desktop Runtime."
        )
    control.request_pause()
    return {"job_id": job_id, "accepted": True}


def cancel_import_job(server: DesktopEngineServer, job_id: str) -> dict[str, object]:
    from openkb.desktop_engine import DesktopRequestError

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


def start_recoverable_imports(server: DesktopEngineServer, kb_dir: Path) -> None:
    job_ids = DesktopTextImportService(kb_dir).recoverable_job_ids()
    if not job_ids:
        return
    threading.Thread(
        target=_resume_recoverable_imports,
        args=(server, kb_dir, job_ids),
        daemon=True,
        name="openkb-engine-import-recovery",
    ).start()


def _resume_recoverable_imports(
    server: DesktopEngineServer, kb_dir: Path, job_ids: tuple[str, ...]
) -> None:
    from openkb.desktop_engine import DesktopRequestError

    for job_id in job_ids:
        if server._shutdown.is_set():
            return
        with server._workspace_transition.import_job(expected_kb_dir=kb_dir) as lease:
            if lease is None:
                return
            try:
                run_import(
                    server,
                    kb_dir,
                    request_id=None,
                    job_id=job_id,
                    control=lease.control,
                )
            except (DesktopImportError, DesktopRequestError) as error:
                logger.warning("Could not resume Desktop import job %s: %s", job_id, error)


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
    from openkb.desktop_engine import DesktopRequestError

    raise DesktopRequestError(
        "invalid_params", "parser_mode must be auto, fast, or enhanced."
    )
