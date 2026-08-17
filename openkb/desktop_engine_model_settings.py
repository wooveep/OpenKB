"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_model_settings import (
    read_desktop_model_settings,
    save_desktop_model_settings,
)

if TYPE_CHECKING:
    from openkb.desktop_engine import DesktopEngineServer, DesktopRequest


def dispatch_model_settings_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Keep model config local to the active KB and pass it only over the private Bridge."""
    from openkb.desktop_engine import DesktopRequestError, _required_path_param

    with server._workspace_requests_lock:
        active = server._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base",
                "Open a Desktop Knowledge Base before changing model settings.",
            )
        kb_dir = Path(active.kb_dir)
        if request.method == "workbench.model_settings":
            return read_desktop_model_settings(kb_dir).as_dict()
        if request.method == "workbench.save_model_settings":
            server._begin_workspace_mutation(request, cancel_event)
            return save_desktop_model_settings(
                kb_dir,
                provider=request.params.get("provider"),
                model=request.params.get("model"),
                api_base_url=request.params.get("api_base_url"),
                api_key=request.params.get("api_key"),
                max_concurrent_model_calls=request.params.get("max_concurrent_model_calls"),
                initial_timeout_seconds=request.params.get("initial_timeout_seconds"),
            ).as_dict()
        if request.method == "workbench.export_diagnostic_bundle":
            return (
                DesktopDiagnosticBundleService(kb_dir)
                .export(Path(_required_path_param(request, "destination")))
                .as_dict()
            )
    raise DesktopRequestError(
        "method_not_found", f"Unknown model-settings method: {request.method}"
    )
