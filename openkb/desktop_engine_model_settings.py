"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_knowledge_graph as graph_engine
from openkb import desktop_engine_page_tree_enrichment as enrichment_engine
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine_model_lifecycle import emit_model_lifecycle
from openkb.desktop_knowledge_graph_tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.desktop_model_capability_check import (
    capability_check_request,
    selected_model_checks,
    validate_capability_result,
)
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
)
from openkb.desktop_model_settings import (
    read_desktop_model_settings,
    save_desktop_model_settings,
    validate_desktop_model_settings,
)
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_model_transport import desktop_model_gateway_for_settings
from openkb.desktop_model_usage import DesktopModelUsageStore
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
            payload = read_desktop_model_settings(kb_dir).as_dict()
            payload["usage_aggregate"] = DesktopModelUsageStore(kb_dir).aggregate()
            return payload
        if request.method == "workbench.save_model_settings":
            server._begin_workspace_mutation(request, cancel_event)
            enrichment_engine.invalidate_page_tree_enrichment_workers(server)
            graph_engine.invalidate_knowledge_graph_workers(server)
            DesktopPageTreeEnrichmentService(kb_dir).recover_interrupted()
            DesktopKnowledgeGraphExtractionTasks(kb_dir).recover_interrupted()
            settings = save_desktop_model_settings(
                kb_dir,
                provider=request.params.get("provider"),
                model=request.params.get("model"),
                api_base_url=request.params.get("api_base_url"),
                api_key=request.params.get("api_key"),
                max_concurrent_model_calls=request.params.get("max_concurrent_model_calls"),
                requests_per_minute=request.params.get("requests_per_minute"),
                tokens_per_minute=request.params.get("tokens_per_minute"),
                **_role_settings_params(request.params),
            )
            enrichment_engine.start_page_tree_enrichments(
                server,
                kb_dir,
                server._model_gateway_factory(kb_dir, None),
                retry_failed=True,
            )
            payload = settings.as_dict()
            payload["usage_aggregate"] = DesktopModelUsageStore(kb_dir).aggregate()
            return payload
        if request.method == "workbench.test_model_connection":
            settings = validate_desktop_model_settings(
                provider=request.params.get("provider"),
                model=request.params.get("model"),
                api_base_url=request.params.get("api_base_url"),
                api_key=request.params.get("api_key"),
                max_concurrent_model_calls=request.params.get("max_concurrent_model_calls"),
                requests_per_minute=request.params.get("requests_per_minute"),
                tokens_per_minute=request.params.get("tokens_per_minute"),
                **_role_settings_params(request.params),
            )
            started_at = time.monotonic()

            def emit_lifecycle(event: DesktopTerminalModelEvent) -> None:
                emit_model_lifecycle(
                    server,
                    kb_dir=kb_dir,
                    request_id=request.request_id,
                    event=event,
                )

            try:
                attempts = 0
                checked_models: list[str] = []
                for model, operation, check_settings in selected_model_checks(settings):
                    gateway = desktop_model_gateway_for_settings(kb_dir, check_settings)
                    check_request = capability_check_request(
                        check_settings,
                        model=model,
                        operation=operation,
                    )
                    is_cancelled = cancel_event.is_set if cancel_event is not None else None
                    result = (
                        gateway.stream(
                            check_request,
                            on_event=emit_lifecycle,
                            on_delta=lambda _attempt, _delta: None,
                            is_cancelled=is_cancelled,
                        )
                        if operation == "model_capability_answer"
                        else gateway.analyze(
                            check_request,
                            on_event=emit_lifecycle,
                            is_cancelled=is_cancelled,
                        )
                    )
                    validate_capability_result(operation, result.content)
                    attempts += result.attempt_count
                    checked_models.append(model)
            except DesktopModelCallError as error:
                raise DesktopRequestError(error.failure.code, error.failure.reason) from error
            except DesktopModelCancelledError as error:
                raise DesktopRequestError(
                    "request_cancelled", "Connection test cancelled."
                ) from error
            except (ValueError, json.JSONDecodeError) as error:
                raise DesktopRequestError(
                    "model_capability_check_failed",
                    str(error),
                ) from error
            return {
                "ok": True,
                "model": settings.model,
                "models": checked_models,
                "latency_ms": round((time.monotonic() - started_at) * 1000),
                "attempt_count": attempts,
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


def _role_settings_params(params: dict[str, object]) -> dict[str, object]:
    names = (
        "analysis_model",
        "answer_model",
        "default_context_capacity",
        "analysis_context_capacity",
        "answer_context_capacity",
        "default_reasoning",
        "analysis_reasoning",
        "answer_reasoning",
        "default_input_price_per_million",
        "default_output_price_per_million",
        "analysis_input_price_per_million",
        "analysis_output_price_per_million",
        "answer_input_price_per_million",
        "answer_output_price_per_million",
    )
    return {name: params.get(name) for name in names}
