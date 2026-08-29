"""Configured LiteLLM adapter for Desktop document analysis."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from openkb.config import LlmCredentialBundle, resolve_credential_bundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_logging import log_event, trace_event
from openkb.desktop_model_active_streams import DesktopActiveModelStreams
from openkb.desktop_model_active_streams import once as _once
from openkb.desktop_model_analysis_gate import DesktopAnalysisCapabilityGate
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_contract_renderer import render_provider_visible_contract
from openkb.desktop_model_dispatch import (
    _concurrency_gate_for,
    _ConcurrentDesktopModelTransport,
    _DesktopModelConcurrencyGate,
    _DesktopModelRateLimiter,
    _rate_limiter_for,
)
from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelRequest,
    DesktopModelTransportError,
    DesktopProviderStreamEvent,
    DesktopProviderTokenUsage,
    ExecutionLane,
)
from openkb.desktop_model_http_lifecycle import terminal_completion_client
from openkb.desktop_model_provider_adapter import model_protocol_for
from openkb.desktop_model_roles import DesktopRoleModelGateway
from openkb.desktop_model_settings import (
    DEFAULT_MAX_CONCURRENT_MODEL_CALLS,
    DesktopModelSettings,
    DesktopModelSettingsError,
    litellm_model_identifier,
    read_desktop_model_settings,
)
from openkb.desktop_model_terminal import DesktopTerminalModelGateway
from openkb.desktop_model_usage import DesktopModelUsageStore
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_sensitive_trace import sensitive_trace_component_enabled

logger = logging.getLogger(__name__)


def desktop_model_gateway_for(
    kb_dir: Path, override: DesktopRecoveryOverride | None = None
) -> DesktopModelGateway | None:
    """Build a live gateway only when this Desktop KB has valid model settings."""
    resolved = kb_dir.expanduser().resolve()
    config_path = resolved / ".openkb" / "config.yaml"
    try:
        bundle = resolve_credential_bundle(resolved)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        bundle = LlmCredentialBundle()
    try:
        settings = read_desktop_model_settings(resolved)
    except (DesktopModelSettingsError, OSError, TypeError, ValueError, yaml.YAMLError):
        logger.warning("Disabling the model gateway because KB-local settings are invalid.")
        return None
    model = (
        override.model if override is not None and override.model is not None else settings.model
    )
    if bundle.api_key is None and not config_path.exists() and override is None:
        return None
    return _gateway_for(
        litellm_model_identifier(settings.provider, model),
        bundle,
        override,
        kb_dir=resolved,
        settings=settings,
    )


def desktop_model_gateway_for_settings(
    kb_dir: Path, settings: DesktopModelSettings
) -> DesktopTerminalModelGateway:
    """Build a gateway from an unsaved Settings draft for connection testing."""
    return DesktopTerminalModelGateway(
        _ConcurrentDesktopModelTransport(
            DesktopLiteLLMTransport(
                model=litellm_model_identifier(settings.provider, settings.model),
                bundle=LlmCredentialBundle(
                    api_key=settings.api_key,
                    base_url=settings.api_base_url,
                ),
            ),
            _DesktopModelConcurrencyGate(settings.max_concurrent_model_calls),
            rate_limiter=_DesktopModelRateLimiter(
                settings.requests_per_minute, settings.tokens_per_minute
            ),
        )
    )


def _gateway_for(
    model: object,
    bundle: LlmCredentialBundle,
    override: DesktopRecoveryOverride | None,
    *,
    kb_dir: Path,
    settings: DesktopModelSettings | None,
) -> DesktopModelGateway:
    concurrency = (
        settings.max_concurrent_model_calls
        if settings is not None
        else DEFAULT_MAX_CONCURRENT_MODEL_CALLS
    )

    def lane_gate(lane: ExecutionLane) -> _DesktopModelConcurrencyGate:
        maximum = 1 if lane == "interactive" else concurrency
        return _concurrency_gate_for(kb_dir, maximum, lane=lane)

    provider = settings.provider if settings is not None else "custom"
    default_model = settings.model if settings is not None else str(model or "")
    analysis_model = (
        override.model
        if override is not None and override.model is not None
        else settings.analysis_model_name
        if settings is not None
        else default_model
    )
    answer_model = settings.answer_model_name if settings is not None else default_model
    if settings is None:
        settings = DesktopModelSettings(
            provider=provider,
            model=default_model,
            api_base_url=bundle.base_url or "",
            api_key=bundle.api_key or "",
            max_concurrent_model_calls=concurrency,
            analysis_model=analysis_model if analysis_model != default_model else None,
            answer_model=answer_model if answer_model != default_model else None,
            analysis_reasoning=override.reasoning if override is not None else None,
        )
    elif (
        analysis_model != settings.analysis_model_name
        or override is not None
        and override.context_capacity is not None
        or override is not None
        and override.reasoning is not None
    ):
        from dataclasses import replace

        settings = replace(
            settings,
            analysis_model=analysis_model,
            analysis_context_capacity=(
                override.context_capacity
                if override is not None and override.context_capacity is not None
                else settings.analysis_context_capacity
            ),
            analysis_reasoning=(
                override.reasoning
                if override is not None and override.reasoning is not None
                else settings.analysis_reasoning
            ),
        )

    gateways: dict[str, DesktopTerminalModelGateway] = {}
    rate_limiter = _rate_limiter_for(
        kb_dir, settings.requests_per_minute, settings.tokens_per_minute
    )

    def terminal_gateway(selected_model: str) -> DesktopTerminalModelGateway:
        existing = gateways.get(selected_model)
        if existing is not None:
            return existing
        transport = DesktopLiteLLMTransport(
            model=litellm_model_identifier(provider, selected_model),
            bundle=bundle,
        )
        gateway = DesktopTerminalModelGateway(
            _ConcurrentDesktopModelTransport(
                transport,
                lane_gate("background"),
                rate_limiter=rate_limiter,
                lane_factory=lane_gate,
            ),
            provider_name=provider,
            model_name=selected_model,
        )
        gateways[selected_model] = gateway
        return gateway

    capability_store = DesktopModelCapabilityStore(kb_dir)

    def invalidate_analysis_capability(
        profile: DesktopModelExecutionProfile,
        failure_code: str,
        reason: str,
    ) -> None:
        DesktopAnalysisCapabilityGate(kb_dir, profile, True).invalidate_failure(
            failure_code,
            reason=reason,
        )

    return DesktopRoleModelGateway(
        settings=settings,
        default_gateway=terminal_gateway(default_model),
        analysis_gateway=terminal_gateway(analysis_model),
        answer_gateway=terminal_gateway(answer_model),
        gateway_factory=terminal_gateway,
        usage_store=DesktopModelUsageStore(kb_dir),
        analysis_capability_verifier=capability_store.is_verified,
        analysis_capability_invalidator=invalidate_analysis_capability,
        analysis_capability_corroborated_invalidator=(
            lambda profile, failure_code, reason: capability_store.invalidate(
                profile,
                failure_code=failure_code,
                reason=reason,
            )
        ),
        answer_capability_verifier=capability_store.is_verified,
    )


class DesktopLiteLLMTransport:
    """One synchronous LiteLLM request; errors remain classified by the gateway."""

    def __init__(self, *, model: object, bundle: LlmCredentialBundle) -> None:
        self._model = model
        self._bundle = bundle
        self._active_streams = DesktopActiveModelStreams()

    def __call__(self, request: DesktopModelRequest, connect_timeout_seconds: float) -> object:
        return self.call_until_terminal(request, connect_timeout_seconds)

    def sensitive_request_payload(self, request: DesktopModelRequest) -> str:
        """Return the exact provider body minus separately supplied credentials/headers."""
        payload: dict[str, object] = {
            "model": self._model,
            "messages": _messages_for(request),
            "base_url": self._bundle.base_url,
            "max_retries": 0,
            **(request.generation_parameters or {}),
            **_provider_request_parameters(request),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr)

    def call_until_terminal(
        self, request: DesktopModelRequest, connect_timeout_seconds: float
    ) -> object:
        """Call LiteLLM with a bound connect phase and unbounded response phases."""
        return self.call_until_terminal_with_lifecycle(
            request,
            connect_timeout_seconds,
            lambda: None,
        )

    def call_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_request_sent: Callable[[], None],
    ) -> object:
        response, close = self._terminal_completion(
            request,
            connect_timeout_seconds,
            stream=False,
            on_request_sent=on_request_sent,
        )
        try:
            return _response_content(response)
        finally:
            close()

    def stream_until_terminal(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_delta: Callable[[object], None],
    ) -> object:
        """Stream with no first-byte, read, reasoning, generation, or total deadline."""
        return self.stream_until_terminal_with_lifecycle(
            request,
            connect_timeout_seconds,
            on_delta,
            lambda: None,
        )

    def prepare_active_stream(self, request: DesktopModelRequest) -> None:
        self._active_streams.prepare(id(request))

    def stream_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_delta: Callable[[object], None],
        on_request_sent: Callable[[], None],
    ) -> object:
        response, close = self._terminal_completion(
            request,
            connect_timeout_seconds,
            stream=True,
            on_request_sent=on_request_sent,
        )
        try:
            return self._consume_stream(
                request,
                response,
                on_delta,
            )
        finally:
            close()

    def cancel_active_stream(self, request: DesktopModelRequest) -> bool:
        """Close this request's live HTTP resources when cancellation races the stream."""
        try:
            return self._active_streams.close(id(request))
        except Exception:
            logger.warning(
                "Could not close the active model stream for operation=%s.",
                request.operation,
            )
            return False

    def _consume_stream(
        self,
        request: DesktopModelRequest,
        response: object,
        on_delta: Callable[[object], None],
    ) -> str:
        if not hasattr(response, "__iter__"):
            raise DesktopModelTransportError("response_format", sensitive_detail=repr(response))
        parts: list[str] = []
        reasoning_parts: list[str] = []
        capture_reasoning = sensitive_trace_component_enabled("model")
        usage: DesktopProviderTokenUsage | None = None
        provider_request_id: str | None = None
        finish_reason: str | None = None
        reasoning_chunk_count = 0
        final_chunk_count = 0
        reasoning_character_count = 0
        final_character_count = 0
        output_limit_reached = False
        adapter = (
            model_protocol_for(request.provider_adapter)
            if request.provider_adapter is not None
            else None
        )
        try:
            for chunk in response:
                usage = _provider_token_usage(chunk) or usage
                provider_request_id = _provider_request_id(chunk) or provider_request_id
                event = (
                    adapter.stream_event(chunk)
                    if adapter is not None
                    else _legacy_stream_event(chunk)
                )
                if event.finish_reason is not None:
                    finish_reason = event.finish_reason
                output_limit_reached = output_limit_reached or event.output_limit_reached
                if event.reasoning_character_count:
                    reasoning_chunk_count += 1
                    reasoning_character_count += event.reasoning_character_count
                    if capture_reasoning and event.sensitive_reasoning_content:
                        reasoning_parts.append(event.sensitive_reasoning_content)
                if event.final_content:
                    final_chunk_count += 1
                    final_character_count += len(event.final_content)
                    parts.append(event.final_content)
                if (
                    event.final_content
                    or event.reasoning_character_count
                    or event.finish_reason is not None
                ):
                    on_delta(event)
        except DesktopModelTransportError:
            raise
        except Exception as error:
            translated = self._provider_transport_error(
                error,
                request,
            )
            if translated is not None:
                raise translated from error
            raise
        return DesktopModelProviderResponse(
            "".join(parts),
            usage=usage,
            provider_request_id=provider_request_id,
            observations=DesktopModelOutputObservations(
                finish_reason=finish_reason,
                reasoning_observed=reasoning_chunk_count > 0,
                final_content_observed=bool("".join(parts).strip()),
                reasoning_chunk_count=reasoning_chunk_count,
                final_chunk_count=final_chunk_count,
                reasoning_character_count=reasoning_character_count,
                final_character_count=final_character_count,
                output_limit_reached=output_limit_reached,
            ),
            sensitive_reasoning_content="".join(reasoning_parts),
        )

    def _terminal_completion(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        *,
        stream: bool,
        on_request_sent: Callable[[], None],
    ) -> tuple[object, Callable[[], None]]:
        from openai import Timeout

        try:
            model = self._validated_model()
            timeout = Timeout(
                connect=connect_timeout_seconds,
                read=None,
                write=None,
                pool=None,
            )
            completion_client, raw_close = terminal_completion_client(
                model=model,
                bundle=self._bundle,
                timeout=timeout,
                on_request_sent=on_request_sent,
            )
            close = _once(raw_close)
            release = self._active_streams.register(id(request), close) if stream else close
        except BaseException:
            if stream:
                self._active_streams.abandon(id(request))
            raise

        try:
            response = self._request_completion(
                request,
                timeout,
                stream=stream,
                completion_client=completion_client,
            )
        except BaseException:
            release()
            raise
        return response, release

    def _request_completion(
        self,
        request: DesktopModelRequest,
        timeout: object,
        *,
        stream: bool,
        completion_client: object | None = None,
    ) -> object:
        self._validated_model()

        log_event(
            logger,
            logging.DEBUG,
            "model_provider_request",
            "A model provider request was dispatched.",
            component="model",
            fields={
                "operation": request.operation,
                "model": self._model if isinstance(self._model, str) else "invalid",
                "adapter": request.provider_adapter,
                "endpoint": _diagnostic_endpoint(self._bundle.base_url),
                "streaming": stream,
                "job_id": request.job_id,
                "stage_run_id": request.stage_run_id,
                "batch_id": request.batch_id,
            },
        )
        try:
            from litellm import completion

            response = completion(
                model=self._model,
                messages=_messages_for(request),
                timeout=timeout,
                api_key=self._bundle.api_key,
                base_url=self._bundle.base_url,
                **(request.generation_parameters or {}),
                **({"stream": True} if stream else {}),
                **({"stream_options": {"include_usage": True}} if stream else {}),
                max_retries=0,
                **_provider_request_parameters(request),
                **({"client": completion_client} if completion_client is not None else {}),
                **(
                    {"extra_headers": self._bundle.extra_headers}
                    if self._bundle.extra_headers
                    else {}
                ),
            )
        except Exception as error:
            translated = self._provider_transport_error(
                error,
                request,
            )
            if translated is not None:
                raise translated from error
            raise
        return response

    def _validated_model(self) -> str:
        if not isinstance(self._model, str) or not self._model.strip():
            raise DesktopModelTransportError("configuration")
        if not self._bundle.api_key:
            raise DesktopModelTransportError("configuration")
        return self._model

    def _provider_transport_error(
        self,
        error: Exception,
        request: DesktopModelRequest,
    ) -> DesktopModelTransportError | None:
        category = _terminal_provider_error_category(error)
        if category is None:
            return None
        diagnostic_detail = _provider_error_detail(error, self._bundle.api_key)
        trace_event(
            logger,
            "model_transport_failure_classified",
            "A provider transport failure was classified for its Failure Owner.",
            component="model",
            fields={
                "operation": request.operation,
                "model": self._model if isinstance(self._model, str) else "invalid",
                "adapter": request.provider_adapter,
                "endpoint": _diagnostic_endpoint(self._bundle.base_url),
                "provider_error_code": category,
                "error_type": type(error).__name__,
                "job_id": request.job_id,
                "stage_run_id": request.stage_run_id,
                "batch_id": request.batch_id,
            },
        )
        return DesktopModelTransportError(
            category,
            retry_after_seconds=_provider_retry_after_seconds(error),
            diagnostic_type=type(error).__name__,
            diagnostic_detail=diagnostic_detail,
            sensitive_detail=str(error),
        )


def _messages_for(request: DesktopModelRequest) -> list[dict[str, str]]:
    contract = prompt_contract_for(request.operation)
    snapshot_instructions = (
        request.prompt_contract_snapshot.get("instructions")
        if request.prompt_contract_snapshot is not None
        else None
    )
    instructions = (
        snapshot_instructions if isinstance(snapshot_instructions, str) else contract.instructions
    )
    return [
        {
            "role": "system",
            "content": render_provider_visible_contract(request, instructions),
        },
        {"role": "user", "content": request.content},
    ]


def _provider_request_parameters(request: DesktopModelRequest) -> dict[str, object]:
    """Translate pinned request semantics through the selected provider adapter."""
    if request.provider_adapter is not None:
        adapter = model_protocol_for(request.provider_adapter)
        if (
            request.provider_adapter_version is not None
            and request.provider_adapter_version != adapter.version
        ):
            raise DesktopModelTransportError("configuration")
        return adapter.request_parameters(
            structured_output_mode=request.structured_output_mode,
            response_schema=request.response_schema,
            response_schema_name=request.response_schema_name,
            reasoning=request.reasoning_effort,
        )

    # Compatibility for direct callers created before provider adapters were pinned.
    parameters: dict[str, object] = {}
    if request.reasoning_effort is not None:
        parameters["reasoning_effort"] = request.reasoning_effort
    if request.response_schema is not None:
        parameters["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.response_schema_name or "openkb_structured_output",
                "strict": True,
                "schema": request.response_schema,
            },
        }
    return parameters


def _response_content(response: object) -> str:
    choices = _value(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise DesktopModelTransportError("response_format", sensitive_detail=repr(response))
    choice = choices[0]
    message = _value(choice, "message")
    content = _value(message, "content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise DesktopModelTransportError("response_format", sensitive_detail=repr(response))
    reasoning = _value(message, "reasoning_content") or _value(message, "reasoning")
    reasoning_characters = len(reasoning) if isinstance(reasoning, str) else 0
    finish_reason = _value(choice, "finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        finish_reason = None
    return DesktopModelProviderResponse(
        content,
        usage=_provider_token_usage(response),
        provider_request_id=_provider_request_id(response),
        observations=DesktopModelOutputObservations(
            finish_reason=finish_reason,
            reasoning_observed=reasoning_characters > 0,
            final_content_observed=bool(content.strip()),
            reasoning_chunk_count=1 if reasoning_characters else 0,
            final_chunk_count=1 if content else 0,
            reasoning_character_count=reasoning_characters,
            final_character_count=len(content),
            output_limit_reached=finish_reason in {"length", "max_tokens", "max_output_tokens"},
        ),
        sensitive_reasoning_content=(
            reasoning
            if isinstance(reasoning, str) and sensitive_trace_component_enabled("model")
            else ""
        ),
    )


def _provider_token_usage(response: object) -> DesktopProviderTokenUsage | None:
    usage = _value(response, "usage")
    if usage is None:
        return None
    input_tokens = _non_negative_int(
        _value(usage, "prompt_tokens") or _value(usage, "input_tokens")
    )
    output_tokens = _non_negative_int(
        _value(usage, "completion_tokens") or _value(usage, "output_tokens")
    )
    total_tokens = _non_negative_int(_value(usage, "total_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return DesktopProviderTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens if total_tokens is not None else input_tokens + output_tokens,
    )


def _provider_request_id(response: object) -> str | None:
    value = _value(response, "id") or _value(response, "request_id")
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _stream_delta(chunk: object) -> str:
    choices = _value(chunk, "choices")
    if not isinstance(choices, list) or not choices:
        return ""
    content = _value(_value(choices[0], "delta"), "content")
    return content if isinstance(content, str) else ""


def _legacy_stream_event(chunk: object) -> DesktopProviderStreamEvent:
    choices = _value(chunk, "choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    finish_reason = _value(choice, "finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        finish_reason = None
    return DesktopProviderStreamEvent(
        final_content=_stream_delta(chunk),
        finish_reason=finish_reason,
        output_limit_reached=finish_reason in {"length", "max_tokens", "max_output_tokens"},
    )


def _terminal_provider_error_category(error: Exception) -> str | None:
    """Preserve explicit terminal causes; elapsed model work is never a timeout."""
    return _provider_error_category_for_policy(error)


def _provider_error_category_for_policy(
    error: Exception,
) -> str | None:
    status_code = _value(error, "status_code")
    if not isinstance(status_code, int):
        status_code = _value(_value(error, "response"), "status_code")
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return "authentication"
        if status_code == 408:
            return "provider_timeout"
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "server"
        if 400 <= status_code <= 499:
            return "input"

    name = type(error).__name__.lower()
    if "timeout" in name:
        return "network_timeout"
    if "rate" in name and "limit" in name:
        return "rate_limited"
    if any(fragment in name for fragment in ("authentication", "permission", "unauthorized")):
        return "authentication"
    if isinstance(error, (ConnectionError, OSError)) or any(
        fragment in name
        for fragment in (
            "connection",
            "network",
            "protocol",
            "disconnect",
            "readerror",
            "writeerror",
        )
    ):
        return "network"
    if any(fragment in name for fragment in ("internalserver", "serviceunavailable")):
        return "server"
    return None


def _diagnostic_endpoint(base_url: str | None) -> str | None:
    """Keep a useful provider address in local logs without query credentials."""
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    if not parsed.scheme or parsed.hostname is None:
        return "<invalid-api-base-url>"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _provider_error_detail(error: Exception, api_key: str | None) -> str:
    """Bound provider diagnostics and remove the directly configured API Key."""
    detail = str(error).strip() or type(error).__name__
    if api_key:
        detail = detail.replace(api_key, "<REDACTED>")
    return detail[:500]


def _provider_retry_after_seconds(error: Exception) -> float | None:
    response = _value(error, "response")
    headers = _value(response, "headers")
    raw_value: object = None
    if isinstance(headers, dict):
        raw_value = headers.get("retry-after", headers.get("Retry-After"))
    else:
        get = getattr(headers, "get", None)
        if callable(get):
            raw_value = get("retry-after") or get("Retry-After")
    if isinstance(raw_value, (int, float)):
        return float(raw_value) if raw_value > 0 else None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        seconds = float(raw_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return seconds if seconds > 0 else None


def _value(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
