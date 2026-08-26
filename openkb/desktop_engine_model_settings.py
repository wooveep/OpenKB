"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_knowledge_graph as graph_engine
from openkb import desktop_engine_page_tree_enrichment as enrichment_engine
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine_model_lifecycle import emit_model_lifecycle
from openkb.desktop_model_capability_check import (
    capability_check_request,
    selected_model_checks,
    validate_capability_result,
)
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_execution_profile import (
    DesktopModelCapacityError,
    analysis_execution_profile_for_settings,
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
            return _settings_payload(kb_dir, read_desktop_model_settings(kb_dir))
        if request.method == "workbench.save_model_settings":
            server._begin_workspace_mutation(request, cancel_event)
            previous = read_desktop_model_settings(kb_dir)
            enrichment_engine.retire_page_tree_enrichment_gateway(server, kb_dir)
            graph_engine.retire_knowledge_graph_gateway(server, kb_dir)
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
            _invalidate_changed_profile(kb_dir, previous, settings)
            return _settings_payload(kb_dir, settings)
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
            capability_store = DesktopModelCapabilityStore(kb_dir)

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
                profile = None
                try:
                    profile = analysis_execution_profile_for_settings(settings)
                except DesktopModelCapacityError:
                    pass
                if profile is not None:
                    capability_store.begin(profile)
                    gateway = desktop_model_gateway_for_settings(
                        kb_dir,
                        replace(settings, model=profile.model),
                    )
                    result = gateway.analyze(
                        capability_check_request(settings, profile=profile),
                        on_event=emit_lifecycle,
                        is_cancelled=(
                            cancel_event.is_set if cancel_event is not None else None
                        ),
                    )
                    validate_capability_result("model_capability_analysis", result.content)
                    attempts = result.attempt_count
                    checked_models.append(profile.model)
                    capability_store.mark_verified(profile)
                else:
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
                if profile is not None:
                    capability_store.mark_failed(
                        profile,
                        failure_code=error.failure.code,
                        reason=error.failure.reason,
                    )
                raise DesktopRequestError(error.failure.code, error.failure.reason) from error
            except DesktopModelCancelledError as error:
                if profile is not None:
                    capability_store.mark_cancelled(profile)
                raise DesktopRequestError(
                    "request_cancelled", "Connection test cancelled."
                ) from error
            except (ValueError, json.JSONDecodeError) as error:
                if profile is not None:
                    capability_store.mark_failed(
                        profile,
                        failure_code="model_capability_check_failed",
                        reason=str(error),
                    )
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
                "profile_identity": profile.identity if profile is not None else None,
                "capability_status": "verified" if profile is not None else "answer_verified",
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


def _settings_payload(kb_dir: Path, settings) -> dict[str, object]:
    payload = settings.as_dict()
    payload["usage_aggregate"] = DesktopModelUsageStore(kb_dir).aggregate()
    try:
        profile = analysis_execution_profile_for_settings(settings)
    except DesktopModelCapacityError as error:
        payload["analysis_capability"] = {
            "profile_identity": None,
            "status": "unchecked",
            "failure_code": "analysis_profile_unavailable",
            "reason": str(error),
            "checked_at": None,
        }
    else:
        payload["analysis_capability"] = DesktopModelCapabilityStore(kb_dir).state(
            profile
        ).as_dict()
    return payload


def _invalidate_changed_profile(kb_dir: Path, previous, current) -> None:
    try:
        previous_profile = analysis_execution_profile_for_settings(previous)
    except DesktopModelCapacityError:
        return
    try:
        current_profile = analysis_execution_profile_for_settings(current)
    except DesktopModelCapacityError:
        current_profile = None
    if current_profile is not None and previous_profile.identity == current_profile.identity:
        return
    DesktopModelCapabilityStore(kb_dir).invalidate(
        previous_profile,
        failure_code="model_execution_profile_changed",
        reason="Model Configuration changed; verify the replacement Analysis profile.",
    )
