"""Configured LiteLLM adapter for Desktop document analysis."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from openkb.config import LlmCredentialBundle, resolve_credential_bundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_model_active_streams import DesktopActiveModelStreams
from openkb.desktop_model_active_streams import once as _once
from openkb.desktop_model_gateway import (
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelProviderResponse,
    DesktopModelRequest,
    DesktopModelTransportError,
    DesktopProviderTokenUsage,
)
from openkb.desktop_model_http_lifecycle import terminal_completion_client
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

_concurrency_gates: dict[tuple[Path, str], _DesktopModelConcurrencyGate] = {}
_concurrency_gates_lock = threading.Lock()

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

    def lane_gate(lane: str) -> _DesktopModelConcurrencyGate:
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
        )
    elif (
        analysis_model != settings.analysis_model_name
        or override is not None
        and override.context_capacity is not None
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
        )

    gateways: dict[str, DesktopTerminalModelGateway] = {}

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
                lane_factory=lane_gate,
            ),
            provider_name=provider,
            model_name=selected_model,
        )
        gateways[selected_model] = gateway
        return gateway

    return DesktopRoleModelGateway(
        settings=settings,
        default_gateway=terminal_gateway(default_model),
        analysis_gateway=terminal_gateway(analysis_model),
        answer_gateway=terminal_gateway(answer_model),
        gateway_factory=terminal_gateway,
        usage_store=DesktopModelUsageStore(kb_dir),
    )


class _DesktopModelConcurrencyGate:
    """A small KB-local limiter; it holds no model config or credentials."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._active = 0
        self._condition = threading.Condition()
        self._waiters: deque[object] = deque()

    def configure(self, maximum: int) -> None:
        with self._condition:
            self._maximum = maximum
            self._condition.notify_all()

    def acquire_until_cancelled(self, is_cancelled: Callable[[], bool] | None) -> None:
        """Wait for capacity without converting queue time into a Model Call deadline."""
        ticket = object()
        with self._condition:
            self._waiters.append(ticket)
            while self._active >= self._maximum or self._waiters[0] is not ticket:
                if is_cancelled is not None and is_cancelled():
                    self._remove_waiter(ticket)
                    raise DesktopModelCancelledError()
                self._condition.wait(0.05)
            self._waiters.popleft()
            self._active += 1

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def _remove_waiter(self, ticket: object) -> None:
        self._waiters.remove(ticket)
        self._condition.notify_all()


def _concurrency_gate_for(
    kb_dir: Path,
    maximum: int,
    *,
    lane: str = "background",
) -> _DesktopModelConcurrencyGate:
    key = (kb_dir, lane)
    with _concurrency_gates_lock:
        gate = _concurrency_gates.get(key)
        if gate is None:
            gate = _DesktopModelConcurrencyGate(maximum)
            _concurrency_gates[key] = gate
        else:
            gate.configure(maximum)
        return gate


class _ConcurrentDesktopModelTransport:
    """Apply the configured limit around both ordinary and streaming requests."""

    def __init__(
        self,
        transport: Callable[[DesktopModelRequest, float], object],
        gate: _DesktopModelConcurrencyGate,
        *,
        lane_factory: Callable[[str], _DesktopModelConcurrencyGate] | None = None,
    ) -> None:
        self._transport = transport
        self._gate = gate
        self._lane_factory = lane_factory

    def for_lane(self, lane: str) -> _ConcurrentDesktopModelTransport:
        """Share one provider transport while selecting an independently limited lane."""
        if lane not in {"background", "interactive"}:
            raise ValueError(f"Unknown model execution lane: {lane}")
        if self._lane_factory is None:
            return self
        return _ConcurrentDesktopModelTransport(
            self._transport,
            self._lane_factory(lane),
            lane_factory=self._lane_factory,
        )

    def __call__(self, request: DesktopModelRequest, connect_timeout_seconds: float) -> object:
        return self._delegate_call("call_until_terminal", request, connect_timeout_seconds)

    def release_prepared_model_attempt(self) -> None:
        self._gate.release()

    def cancel_active_stream(self, request: DesktopModelRequest) -> bool:
        cancel = getattr(self._transport, "cancel_active_stream", None)
        return bool(cancel(request)) if callable(cancel) else False

    def prepare_active_stream(self, request: DesktopModelRequest) -> None:
        prepare = getattr(self._transport, "prepare_active_stream", None)
        if callable(prepare):
            prepare(request)

    def prepare_terminal_model_attempt(
        self, is_cancelled: Callable[[], bool] | None
    ) -> Callable[[], None]:
        self._gate.acquire_until_cancelled(is_cancelled)
        return _once(self._gate.release)

    def call_until_terminal(
        self, request: DesktopModelRequest, connect_timeout_seconds: float
    ) -> object:
        return self._delegate_call("call_until_terminal", request, connect_timeout_seconds)

    def call_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_request_sent: Callable[[], None],
    ) -> object:
        call = getattr(self._transport, "call_until_terminal_with_lifecycle", None)
        if callable(call):
            return call(request, connect_timeout_seconds, on_request_sent)
        response = self._delegate_call("call_until_terminal", request, connect_timeout_seconds)
        on_request_sent()
        return response

    def stream_until_terminal(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> object:
        return self._delegate_stream(
            "stream_until_terminal", request, connect_timeout_seconds, on_delta
        )

    def stream_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_delta: Callable[[str], None],
        on_request_sent: Callable[[], None],
    ) -> object:
        stream = getattr(self._transport, "stream_until_terminal_with_lifecycle", None)
        if callable(stream):
            return stream(
                request,
                connect_timeout_seconds,
                on_delta,
                on_request_sent,
            )
        on_request_sent()
        return self._delegate_stream(
            "stream_until_terminal", request, connect_timeout_seconds, on_delta
        )

    def _delegate_call(
        self,
        method_name: str,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
    ) -> object:
        call = getattr(self._transport, method_name, None)
        if callable(call):
            return call(request, connect_timeout_seconds)
        return self._transport(request, connect_timeout_seconds)

    def _delegate_stream(
        self,
        method_name: str,
        request: DesktopModelRequest,
        connect_timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> object:
        stream = getattr(self._transport, method_name, None)
        if callable(stream):
            return stream(request, connect_timeout_seconds, on_delta)
        response = self._transport(request, connect_timeout_seconds)
        if isinstance(response, str):
            on_delta(response)
        return response


class DesktopLiteLLMTransport:
    """One synchronous LiteLLM request; errors remain classified by the gateway."""

    def __init__(self, *, model: object, bundle: LlmCredentialBundle) -> None:
        self._model = model
        self._bundle = bundle
        self._active_streams = DesktopActiveModelStreams()

    def __call__(self, request: DesktopModelRequest, connect_timeout_seconds: float) -> object:
        return self.call_until_terminal(request, connect_timeout_seconds)

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
        on_delta: Callable[[str], None],
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
        on_delta: Callable[[str], None],
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
        on_delta: Callable[[str], None],
    ) -> str:
        if not hasattr(response, "__iter__"):
            raise DesktopModelTransportError("response_format")
        parts: list[str] = []
        usage: DesktopProviderTokenUsage | None = None
        provider_request_id: str | None = None
        try:
            for chunk in response:
                usage = _provider_token_usage(chunk) or usage
                provider_request_id = _provider_request_id(chunk) or provider_request_id
                delta = _stream_delta(chunk)
                if delta:
                    parts.append(delta)
                    on_delta(delta)
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
                timeout_description=f"{connect_timeout_seconds:.1f}s connect-only",
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
        timeout_description: str,
        stream: bool,
        completion_client: object | None = None,
    ) -> object:
        self._validated_model()

        logger.info(
            "model_provider_request operation=%s document=%r model=%r endpoint=%r "
            "timeout=%r stream=%s",
            request.operation,
            request.document_name,
            self._model,
            _diagnostic_endpoint(self._bundle.base_url),
            timeout_description,
            stream,
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
                **(
                    {"reasoning_effort": request.reasoning_effort}
                    if request.reasoning_effort is not None
                    else {}
                ),
                **(
                    {
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": request.response_schema_name or "openkb_structured_output",
                                "strict": True,
                                "schema": request.response_schema,
                            },
                        }
                    }
                    if request.response_schema is not None
                    else {}
                ),
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
        logger.warning(
            "model_provider_request_failed operation=%s document=%r model=%r "
            "endpoint=%r category=%s exception_type=%s detail=%r",
            request.operation,
            request.document_name,
            self._model,
            _diagnostic_endpoint(self._bundle.base_url),
            category,
            type(error).__name__,
            diagnostic_detail,
        )
        return DesktopModelTransportError(
            category,
            retry_after_seconds=_provider_retry_after_seconds(error),
            diagnostic_type=type(error).__name__,
            diagnostic_detail=diagnostic_detail,
        )


def _messages_for(request: DesktopModelRequest) -> list[dict[str, str]]:
    contract = prompt_contract_for(request.operation)
    snapshot_instructions = (
        request.prompt_contract_snapshot.get("instructions")
        if request.prompt_contract_snapshot is not None
        else None
    )
    return [
        {
            "role": "system",
            "content": (
                snapshot_instructions
                if isinstance(snapshot_instructions, str)
                else contract.instructions
            ),
        },
        {"role": "user", "content": request.content},
    ]


def _response_content(response: object) -> str:
    choices = _value(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise DesktopModelTransportError("response_format")
    content = _value(_value(choices[0], "message"), "content")
    if not isinstance(content, str) or not content.strip():
        raise DesktopModelTransportError("response_format")
    return DesktopModelProviderResponse(
        content,
        usage=_provider_token_usage(response),
        provider_request_id=_provider_request_id(response),
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
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-api-base-url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


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
