"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_page_tree_enrichment as enrichment_engine
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelRequest,
)
from openkb.desktop_model_settings import (
    read_desktop_model_settings,
    save_desktop_model_settings,
    validate_desktop_model_settings,
)
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_model_transport import desktop_model_gateway_for_settings
from openkb.desktop_page_tree_enrichment import DesktopPageTreeEnrichmentService

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
            enrichment_engine.invalidate_page_tree_enrichment_workers(server)
            DesktopPageTreeEnrichmentService(kb_dir).recover_interrupted()
            settings = save_desktop_model_settings(
                kb_dir,
                provider=request.params.get("provider"),
                model=request.params.get("model"),
                api_base_url=request.params.get("api_base_url"),
                api_key=request.params.get("api_key"),
                max_concurrent_model_calls=request.params.get("max_concurrent_model_calls"),
                initial_timeout_seconds=request.params.get("initial_timeout_seconds"),
            )
            enrichment_engine.start_page_tree_enrichments(
                server,
                kb_dir,
                server._model_gateway_factory(kb_dir, None),
                retry_failed=True,
            )
            return settings.as_dict()
        if request.method == "workbench.test_model_connection":
            settings = validate_desktop_model_settings(
                provider=request.params.get("provider"),
                model=request.params.get("model"),
                api_base_url=request.params.get("api_base_url"),
                api_key=request.params.get("api_key"),
                max_concurrent_model_calls=request.params.get("max_concurrent_model_calls"),
                initial_timeout_seconds=request.params.get("initial_timeout_seconds"),
            )
            started_at = time.monotonic()

            def emit_lifecycle(event: DesktopTerminalModelEvent) -> None:
                payload = event.as_dict()
                payload["request_id"] = request.request_id
                server._emit_event("model.call_lifecycle", payload)

            try:
                result = desktop_model_gateway_for_settings(kb_dir, settings).stream(
                    DesktopModelRequest(
                        operation="connection_test",
                        document_name="OpenKB connection test",
                        content="Reply with the single word OK.",
                    ),
                    on_event=emit_lifecycle,
                    on_delta=lambda _attempt, _delta: None,
                    is_cancelled=cancel_event.is_set if cancel_event is not None else None,
                )
            except DesktopModelCallError as error:
                raise DesktopRequestError(error.failure.code, error.failure.reason) from error
            except DesktopModelCancelledError as error:
                raise DesktopRequestError(
                    "request_cancelled", "Connection test cancelled."
                ) from error
            return {
                "ok": True,
                "model": settings.model,
                "latency_ms": round((time.monotonic() - started_at) * 1000),
                "attempt_count": result.attempt_count,
            }
        if request.method == "workbench.export_diagnostic_bundle":
            return (
                DesktopDiagnosticBundleService(kb_dir)
                .export(Path(_required_path_param(request, "destination")))
                .as_dict()
            )
    raise DesktopRequestError(
        "method_not_found", f"Unknown model-settings method: {request.method}"
    )
