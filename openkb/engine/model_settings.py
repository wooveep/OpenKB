"""Desktop Engine routes for KB-local model configuration and diagnostics."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from openkb.diagnostics.bundle import DesktopDiagnosticBundleService
from openkb.engine import knowledge_graph as graph_engine
from openkb.engine import page_tree_enrichment as enrichment_engine
from openkb.engine.model_lifecycle import emit_model_lifecycle
from openkb.models.capability_check import (
    model_capability_check_plans,
)
from openkb.models.capability_store import DesktopModelCapabilityStore
from openkb.models.capability_verifier import (
    DesktopModelCapabilityVerificationError,
    verify_model_capability,
)
from openkb.models.execution_profile import (
    DesktopModelCapacityError,
    analysis_execution_profile_for_settings,
    answer_capability_profile_for_settings,
)
from openkb.models.provider_adapter import model_protocol_for
from openkb.models.settings import (
    DesktopModelSettings,
    DesktopModelSettingsError,
    read_desktop_model_settings,
    save_desktop_model_settings,
    validate_desktop_model_settings,
)
from openkb.models.terminal import DesktopTerminalModelEvent
from openkb.models.transport import desktop_model_gateway_for_settings
from openkb.models.usage import DesktopModelUsageStore

if TYPE_CHECKING:
    from openkb.engine.protocol import DesktopRequest
    from openkb.engine.server import DesktopEngineServer


def dispatch_model_settings_request(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
) -> dict[str, object]:
    """Keep model config local to the active KB and pass it only over the private Bridge."""
    from openkb.engine.protocol import DesktopRequestError, required_string_param

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
            settings = _save_request_model_settings(server, kb_dir, request.params)
            return _settings_payload(kb_dir, settings)
        if request.method == "workbench.save_and_verify_model_settings":
            if request.params.get("verification_cost_accepted") is not True:
                raise DesktopRequestError(
                    "model_verification_cost_consent_required",
                    "Confirm that Model Capability Checks may incur provider cost "
                    "before continuing.",
                )
            settings = _save_request_model_settings(server, kb_dir, request.params)
            return _verify_saved_settings(
                server,
                request,
                cancel_event,
                kb_dir=kb_dir,
                settings=settings,
            )
        if request.method == "workbench.test_model_connection":
            try:
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
            except DesktopModelSettingsError as error:
                raise DesktopRequestError(error.code, str(error)) from error
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
                .export(Path(required_string_param(request, "destination")))
                .as_dict()
            )
    raise DesktopRequestError(
        "method_not_found", f"Unknown model-settings method: {request.method}"
    )


def _save_request_model_settings(
    server: DesktopEngineServer,
    kb_dir: Path,
    params: dict[str, object],
) -> DesktopModelSettings:
    """Persist one request and apply the shared gateway/profile transition exactly once."""
    from openkb.engine.protocol import DesktopRequestError

    previous = read_desktop_model_settings(kb_dir)
    try:
        settings = save_desktop_model_settings(
            kb_dir,
            provider=params.get("provider"),
            model=params.get("model"),
            api_base_url=params.get("api_base_url"),
            api_key=params.get("api_key"),
            max_concurrent_model_calls=params.get("max_concurrent_model_calls"),
            requests_per_minute=params.get("requests_per_minute"),
            tokens_per_minute=params.get("tokens_per_minute"),
            **_role_settings_params(params),
        )
    except DesktopModelSettingsError as error:
        raise DesktopRequestError(error.code, str(error)) from error
    _retire_optional_gateways_if_analysis_settings_changed(
        server,
        kb_dir,
        previous=previous,
        current=settings,
    )
    _invalidate_changed_profile(kb_dir, previous, settings)
    return settings


def _verify_saved_settings(
    server: DesktopEngineServer,
    request: DesktopRequest,
    cancel_event: Event | None,
    *,
    kb_dir: Path,
    settings: DesktopModelSettings,
) -> dict[str, object]:
    """Verify saved settings role by role without rolling back or hiding partial results."""
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
    analysis_error: str | None = None
    try:
        analysis_profile = analysis_execution_profile_for_settings(settings)
    except DesktopModelCapacityError as error:
        analysis_error = str(error)
    answer_profile = None
    answer_error: str | None = None
    try:
        answer_profile = answer_capability_profile_for_settings(settings)
    except DesktopModelCapacityError as error:
        answer_error = str(error)

    role_results: dict[str, dict[str, object]] = {
        "analysis": _unverified_role_result(
            "analysis",
            settings.analysis_model_name,
            (
                analysis_profile.capability_evidence_profile.identity
                if analysis_profile is not None
                else None
            ),
        ),
        "answer": _unverified_role_result(
            "answer",
            settings.answer_model_name,
            answer_profile.identity if answer_profile is not None else None,
        ),
    }
    if analysis_profile is None:
        role_results["analysis"] = {
            **role_results["analysis"],
            "status": "unavailable",
            "failure_code": "analysis_profile_unavailable",
            "reason": analysis_error or adapter.analysis_unavailable_reason,
        }
    if answer_profile is None:
        role_results["answer"] = {
            **role_results["answer"],
            "status": "unavailable",
            "failure_code": "answer_profile_unavailable",
            "reason": answer_error,
        }
    if settings.model == settings.answer_model_name:
        role_results["default"] = {
            **role_results["answer"],
            "role": "default",
            "covered_by": "answer",
        }
    else:
        role_results["default"] = {
            **_unverified_role_result("default", settings.model, None),
            "status": "not_required",
            "reason": "Default is not a required Desktop model-operation role.",
        }
    checks = model_capability_check_plans(
        settings,
        analysis_profile=analysis_profile,
        answer_profile=answer_profile,
        include_default=False,
    )
    attempts = 0
    checked_models: list[str] = []
    cancelled = False
    is_cancelled = cancel_event.is_set if cancel_event is not None else lambda: False
    for check in checks:
        if is_cancelled():
            cancelled = True
            break
        checked_models.append(check.model)
        try:
            verification = verify_model_capability(
                kb_dir,
                role=check.role,
                model=check.model,
                profile=check.evidence_profile,
                gateway=desktop_model_gateway_for_settings(kb_dir, check.settings),
                request=check.request,
                on_event=emit_lifecycle,
                is_cancelled=lambda: False,
                reuse_verified=True,
            )
        except DesktopModelCapabilityVerificationError as error:
            attempts += error.attempt_count
            cancelled = error.code == "request_cancelled"
            role_results[check.role] = {
                **_unverified_role_result(
                    check.role,
                    check.model,
                    (
                        check.evidence_profile.identity
                        if check.evidence_profile is not None
                        else None
                    ),
                ),
                "status": "cancelled" if cancelled else "failed",
                "failure_code": error.code,
                "reason": error.reason,
            }
            if check.role == "answer" and settings.model == settings.answer_model_name:
                role_results["default"] = {
                    **role_results["answer"],
                    "role": "default",
                    "covered_by": "answer",
                }
            if cancelled:
                break
            continue
        attempts += verification.attempt_count
        role_results[check.role] = verification.as_dict()
        if check.role == "answer" and settings.model == settings.answer_model_name:
            role_results["default"] = {
                **verification.as_dict(),
                "role": "default",
                "covered_by": "answer",
            }
    return {
        "saved": True,
        "verification_cost_accepted": True,
        "all_required_roles_verified": all(
            role_results[role]["status"] == "verified" for role in ("analysis", "answer")
        ),
        "cancelled": cancelled,
        "models": checked_models,
        "attempt_count": attempts,
        "latency_ms": round((time.monotonic() - started_at) * 1000),
        "role_results": role_results,
        "settings": _settings_payload(kb_dir, settings),
    }


def _unverified_role_result(
    role: str,
    model: str,
    profile_identity: str | None,
) -> dict[str, object]:
    return {
        "role": role,
        "model": model,
        "status": "unverified",
        "attempt_count": 0,
        "profile_identity": profile_identity,
        "cached": False,
        "failure_code": None,
        "reason": None,
    }


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
        current_analysis is None
        or previous_analysis.capability_evidence_profile.identity
        != current_analysis.capability_evidence_profile.identity
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


def _retire_optional_gateways_if_analysis_settings_changed(
    server: DesktopEngineServer,
    kb_dir: Path,
    *,
    previous: DesktopModelSettings,
    current: DesktopModelSettings,
) -> None:
    """Retire workers only when their captured Analysis configuration became stale."""
    if _analysis_worker_configuration(previous) == _analysis_worker_configuration(current):
        return
    enrichment_engine.retire_page_tree_enrichment_gateway(server, kb_dir)
    graph_engine.retire_knowledge_graph_gateway(server, kb_dir)


def _analysis_worker_configuration(settings: DesktopModelSettings) -> tuple[object, ...]:
    """Return only settings captured by optional Analysis worker gateways."""
    return (
        settings.provider,
        settings.api_base_url,
        settings.api_key,
        settings.max_concurrent_model_calls,
        settings.requests_per_minute,
        settings.tokens_per_minute,
        settings.role_settings("analysis"),
    )
