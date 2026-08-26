"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb import desktop_engine_knowledge_graph as graph_engine
from openkb import desktop_engine_page_tree_enrichment as enrichment_engine
from openkb.desktop_diagnostic_bundle import DesktopDiagnosticBundleService
from openkb.desktop_engine_model_lifecycle import emit_model_lifecycle
from openkb.desktop_model_capability_check import (
    model_capability_check_plans,
)
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_capability_verifier import (
    DesktopModelCapabilityVerificationError,
    verify_model_capability,
)
from openkb.desktop_model_execution_profile import (
    DesktopModelCapacityError,
    analysis_execution_profile_for_settings,
    answer_capability_profile_for_settings,
)
from openkb.desktop_model_provider_adapter import model_protocol_for
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

            def emit_lifecycle(event: DesktopTerminalModelEvent) -> None:
                emit_model_lifecycle(
                    server,
                    kb_dir=kb_dir,
                    request_id=request.request_id,
                    event=event,
                )

            adapter = model_protocol_for(settings.provider)
            analysis_profile = None
            try:
                analysis_profile = analysis_execution_profile_for_settings(settings)
            except DesktopModelCapacityError as error:
                if adapter.supports_structured_analysis:
                    raise DesktopRequestError("analysis_profile_unavailable", str(error)) from error
            try:
                answer_profile = answer_capability_profile_for_settings(settings)
            except DesktopModelCapacityError as error:
                raise DesktopRequestError("answer_profile_unavailable", str(error)) from error
            checks = model_capability_check_plans(
                settings,
                analysis_profile=analysis_profile,
                answer_profile=answer_profile,
            )

            attempts = 0
            checked_models: list[str] = []
            role_results: dict[str, dict[str, object]] = {}
            if analysis_profile is None:
                role_results["analysis"] = {
                    "role": "analysis",
                    "status": "unavailable",
                    "reason": adapter.analysis_unavailable_reason,
                    "attempt_count": 0,
                    "profile_identity": None,
                    "cached": False,
                }
            try:
                for check in checks:
                    verification = verify_model_capability(
                        kb_dir,
                        role=check.role,
                        model=check.model,
                        profile=check.evidence_profile,
                        gateway=desktop_model_gateway_for_settings(kb_dir, check.settings),
                        request=check.request,
                        on_event=emit_lifecycle,
                        is_cancelled=(cancel_event.is_set if cancel_event is not None else None),
                    )
                    attempts += verification.attempt_count
                    checked_models.append(check.model)
                    role_results[check.role] = verification.as_dict()
                    if check.role == "answer" and settings.model == settings.answer_model_name:
                        role_results["default"] = {
                            **verification.as_dict(),
                            "role": "default",
                            "covered_by": "answer",
                        }
            except DesktopModelCapabilityVerificationError as error:
                raise DesktopRequestError(
                    error.code,
                    f"{error.role.title()} Model Capability Check failed: {error.reason}",
                ) from error
            return {
                "ok": True,
                "model": settings.model,
                "models": checked_models,
                "latency_ms": round((time.monotonic() - started_at) * 1000),
                "attempt_count": attempts,
                "profile_identity": (
                    analysis_profile.identity
                    if analysis_profile is not None
                    else answer_profile.identity
                ),
                "capability_status": (
                    "verified" if analysis_profile is not None else "answer_verified"
                ),
                "role_results": role_results,
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
    capability_store = DesktopModelCapabilityStore(kb_dir)
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
        payload["analysis_capability"] = capability_store.state(profile).as_dict()
    try:
        answer_profile = answer_capability_profile_for_settings(settings)
    except DesktopModelCapacityError as error:
        payload["answer_capability"] = {
            "profile_identity": None,
            "status": "unchecked",
            "failure_code": "answer_profile_unavailable",
            "reason": str(error),
            "checked_at": None,
        }
    else:
        payload["answer_capability"] = capability_store.state(answer_profile).as_dict()
    return payload


def _invalidate_changed_profile(kb_dir: Path, previous, current) -> None:
    capability_store = DesktopModelCapabilityStore(kb_dir)
    try:
        previous_analysis = analysis_execution_profile_for_settings(previous)
    except DesktopModelCapacityError:
        previous_analysis = None
    try:
        current_analysis = analysis_execution_profile_for_settings(current)
    except DesktopModelCapacityError:
        current_analysis = None
    if previous_analysis is not None and (
        current_analysis is None or previous_analysis.identity != current_analysis.identity
    ):
        capability_store.invalidate(
            previous_analysis,
            failure_code="model_execution_profile_changed",
            reason="Model Configuration changed; verify the replacement Analysis profile.",
        )
    try:
        previous_answer = answer_capability_profile_for_settings(previous)
    except DesktopModelCapacityError:
        previous_answer = None
    try:
        current_answer = answer_capability_profile_for_settings(current)
    except DesktopModelCapacityError:
        current_answer = None
    if previous_answer is not None and (
        current_answer is None or previous_answer.identity != current_answer.identity
    ):
        capability_store.invalidate(
            previous_answer,
            failure_code="model_execution_profile_changed",
            reason="Model Configuration changed; verify the replacement Answer profile.",
        )
