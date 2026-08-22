"""Configured LiteLLM adapter for Desktop document analysis."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from openkb.config import LlmCredentialBundle, resolve_credential_bundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SYSTEM_PROMPT
from openkb.desktop_knowledge_analysis_batches import (
    KNOWLEDGE_ANALYSIS_BATCH_SYSTEM_PROMPT,
    KNOWLEDGE_ANALYSIS_MERGE_SYSTEM_PROMPT,
)
from openkb.desktop_model_gateway import (
    INITIAL_RESPONSE_TIMEOUT_SECONDS,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelTransportError,
)
from openkb.desktop_model_http_lifecycle import terminal_completion_client
from openkb.desktop_model_settings import (
    DEFAULT_MAX_CONCURRENT_MODEL_CALLS,
    DesktopModelSettings,
    DesktopModelSettingsError,
    litellm_model_identifier,
    read_desktop_model_settings,
)
from openkb.desktop_model_terminal import DesktopTerminalModelGateway
from openkb.desktop_page_tree_enrichment import PAGE_TREE_ENRICHMENT_SYSTEM_PROMPT

_concurrency_gates: dict[Path, _DesktopModelConcurrencyGate] = {}
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
    timeout = (
        override.initial_timeout_seconds
        if override is not None and override.initial_timeout_seconds is not None
        else (
            settings.initial_timeout_seconds
            if settings is not None
            else INITIAL_RESPONSE_TIMEOUT_SECONDS
        )
    )
    concurrency = (
        settings.max_concurrent_model_calls
        if settings is not None
        else DEFAULT_MAX_CONCURRENT_MODEL_CALLS
    )
    return DesktopModelGateway(
        _ConcurrentDesktopModelTransport(
            DesktopLiteLLMTransport(model=model, bundle=bundle),
            _concurrency_gate_for(kb_dir, concurrency),
        ),
        initial_timeout_seconds=timeout,
        provider_name=settings.provider if settings is not None else "custom",
        model_name=(
            override.model
            if override is not None and override.model is not None
            else settings.model
            if settings is not None
            else str(model or "")
        ),
    )


class _DesktopModelConcurrencyGate:
    """A small KB-local limiter; it holds no model config or credentials."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._active = 0
        self._condition = threading.Condition()

    def configure(self, maximum: int) -> None:
        with self._condition:
            self._maximum = maximum
            self._condition.notify_all()

    def acquire(self, is_cancelled: Callable[[], bool] | None, remaining_seconds: float) -> bool:
        deadline = time.monotonic() + remaining_seconds
        with self._condition:
            while self._active >= self._maximum:
                if is_cancelled is not None and is_cancelled():
                    raise DesktopModelCancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(0.05, remaining))
            self._active += 1
            return True

    def acquire_until_cancelled(self, is_cancelled: Callable[[], bool] | None) -> None:
        """Wait for capacity without converting queue time into a Model Call deadline."""
        with self._condition:
            while self._active >= self._maximum:
                if is_cancelled is not None and is_cancelled():
                    raise DesktopModelCancelledError()
                self._condition.wait(0.05)
            self._active += 1

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify()


def _concurrency_gate_for(kb_dir: Path, maximum: int) -> _DesktopModelConcurrencyGate:
    with _concurrency_gates_lock:
        gate = _concurrency_gates.get(kb_dir)
        if gate is None:
            gate = _DesktopModelConcurrencyGate(maximum)
            _concurrency_gates[kb_dir] = gate
        else:
            gate.configure(maximum)
        return gate


def _once(call: Callable[[], None]) -> Callable[[], None]:
    """Return a thread-safe idempotent release callback for one acquired slot."""
    lock = threading.Lock()
    called = False

    def invoke() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        call()

    return invoke


class _ConcurrentDesktopModelTransport:
    """Apply the configured limit around both ordinary and streaming requests."""

    def __init__(
        self,
        transport: Callable[[DesktopModelRequest, float], object],
        gate: _DesktopModelConcurrencyGate,
    ) -> None:
        self._transport = transport
        self._gate = gate

    def __call__(self, request: DesktopModelRequest, timeout_seconds: float) -> object:
        return self._run(lambda: self._transport(request, timeout_seconds))

    def prepare_model_attempt(
        self, is_cancelled: Callable[[], bool] | None, remaining_seconds: float
    ) -> bool:
        return self._gate.acquire(is_cancelled, remaining_seconds)

    def release_prepared_model_attempt(self) -> None:
        self._gate.release()

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

    def stream(
        self,
        request: DesktopModelRequest,
        timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> object:
        return self._run(
            lambda: self._delegate_stream("stream", request, timeout_seconds, on_delta)
        )

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
        timeout_seconds: float,
    ) -> object:
        call = getattr(self._transport, method_name, None)
        if callable(call):
            return call(request, timeout_seconds)
        return self._transport(request, timeout_seconds)

    def _delegate_stream(
        self,
        method_name: str,
        request: DesktopModelRequest,
        timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> object:
        stream = getattr(self._transport, method_name, None)
        if callable(stream):
            return stream(request, timeout_seconds, on_delta)
        response = self._transport(request, timeout_seconds)
        if isinstance(response, str):
            on_delta(response)
        return response

    def _run(self, call: Callable[[], object]) -> object:
        try:
            return call()
        finally:
            self._gate.release()


class DesktopLiteLLMTransport:
    """One synchronous LiteLLM request; errors remain classified by the gateway."""

    def __init__(self, *, model: object, bundle: LlmCredentialBundle) -> None:
        self._model = model
        self._bundle = bundle

    def __call__(self, request: DesktopModelRequest, timeout_seconds: float) -> object:
        return _response_content(self._completion(request, timeout_seconds, stream=False))

    def stream(
        self,
        request: DesktopModelRequest,
        timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> object:
        """Consume LiteLLM's iterator and forward only textual answer deltas."""
        response = self._completion(request, timeout_seconds, stream=True)
        return self._consume_stream(
            request,
            response,
            on_delta,
            terminal_policy=False,
        )

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
                terminal_policy=True,
            )
        finally:
            close()

    def _consume_stream(
        self,
        request: DesktopModelRequest,
        response: object,
        on_delta: Callable[[str], None],
        *,
        terminal_policy: bool,
    ) -> str:
        if not hasattr(response, "__iter__"):
            raise DesktopModelTransportError("response_format")
        parts: list[str] = []
        try:
            for chunk in response:
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
                terminal_policy=terminal_policy,
            )
            if translated is not None:
                raise translated from error
            raise
        return "".join(parts)

    def _completion(
        self, request: DesktopModelRequest, timeout_seconds: float, *, stream: bool
    ) -> object:
        return self._request_completion(
            request,
            timeout_seconds,
            timeout_description=f"{timeout_seconds:.1f}s response",
            stream=stream,
            terminal_policy=False,
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

        model = self._validated_model()
        timeout = Timeout(
            connect=connect_timeout_seconds,
            read=None,
            write=None,
            pool=None,
        )
        completion_client, close = terminal_completion_client(
            model=model,
            bundle=self._bundle,
            timeout=timeout,
            on_request_sent=on_request_sent,
        )
        try:
            response = self._request_completion(
                request,
                timeout,
                timeout_description=f"{connect_timeout_seconds:.1f}s connect-only",
                stream=stream,
                terminal_policy=True,
                completion_client=completion_client,
            )
        except BaseException:
            close()
            raise
        return response, close

    def _request_completion(
        self,
        request: DesktopModelRequest,
        timeout: object,
        *,
        timeout_description: str,
        stream: bool,
        terminal_policy: bool,
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
                **({"stream": True} if stream else {}),
                **({"max_retries": 0} if terminal_policy else {}),
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
                terminal_policy=terminal_policy,
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
        *,
        terminal_policy: bool,
    ) -> DesktopModelTransportError | None:
        category = (
            _terminal_provider_error_category(error)
            if terminal_policy
            else _provider_error_category(error)
        )
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
    if request.operation == "page_tree_enrichment":
        return [
            {"role": "system", "content": PAGE_TREE_ENRICHMENT_SYSTEM_PROMPT},
            {"role": "user", "content": request.content},
        ]
    if request.operation == "knowledge_analysis":
        return [
            {"role": "system", "content": KNOWLEDGE_ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": request.content},
        ]
    if request.operation == "knowledge_analysis_batch":
        return [
            {"role": "system", "content": KNOWLEDGE_ANALYSIS_BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": request.content},
        ]
    if request.operation == "knowledge_analysis_merge":
        return [
            {"role": "system", "content": KNOWLEDGE_ANALYSIS_MERGE_SYSTEM_PROMPT},
            {"role": "user", "content": request.content},
        ]
    if request.operation == "retrieval_plan":
        return [
            {
                "role": "system",
                "content": (
                    "Build a bounded retrieval plan for a local knowledge base. "
                    "Return exactly one JSON object with a single `terms` array of at most 8 "
                    "short search terms. Do not write SQL, tool calls, or an answer."
                ),
            },
            {"role": "user", "content": request.content},
        ]
    if request.operation == "grounded_answer":
        return [
            {
                "role": "system",
                "content": (
                    "Answer only from the supplied source evidence. Be concise, state when "
                    "the evidence is insufficient, and cite supporting evidence numbers "
                    "such as [1]."
                ),
            },
            {"role": "user", "content": request.content},
        ]
    if request.operation == "knowledge_graph_extraction":
        return [
            {
                "role": "system",
                "content": (
                    "Extract a small evidence-bound local knowledge graph. Return exactly one "
                    "JSON object with `nodes` and `edges` arrays. Each node must have `id`, "
                    "`evidence_id`, `type` (`entity`, `concept`, or `claim`), and `label`. "
                    "Each edge must have `evidence_id`, `source_id`, `target_id`, and `type` "
                    "from IS_A, PART_OF, RELATED_TO, DEPENDS_ON, USES, PRODUCES, LOCATED_IN, "
                    "CREATED_BY, PRECEDES, REPLACES, SUPPORTS, or CONTRADICTS. Use only the "
                    "provided evidence IDs; both endpoints and every edge must cite the same "
                    "evidence ID. Do not merge same-named entities or invent facts."
                ),
            },
            {
                "role": "user",
                "content": f"Document: {request.document_name}\n\n{request.content}",
            },
        ]
    return [
        {
            "role": "system",
            "content": (
                "Analyze the document for local knowledge-base indexing. "
                "Return a concise factual summary of its main topics."
            ),
        },
        {
            "role": "user",
            "content": f"Document: {request.document_name}\n\n{request.content}",
        },
    ]


def _response_content(response: object) -> str:
    choices = _value(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise DesktopModelTransportError("response_format")
    content = _value(_value(choices[0], "message"), "content")
    if not isinstance(content, str) or not content.strip():
        raise DesktopModelTransportError("response_format")
    return content


def _stream_delta(chunk: object) -> str:
    choices = _value(chunk, "choices")
    if not isinstance(choices, list) or not choices:
        return ""
    content = _value(_value(choices[0], "delta"), "content")
    return content if isinstance(content, str) else ""


def _provider_error_category(error: Exception) -> str | None:
    return _provider_error_category_for_policy(
        error,
        http_timeout_category="timeout",
        client_timeout_category="timeout",
    )


def _terminal_provider_error_category(error: Exception) -> str | None:
    """Preserve explicit terminal causes; elapsed model work is never a timeout."""
    return _provider_error_category_for_policy(
        error,
        http_timeout_category="provider_timeout",
        client_timeout_category="network_timeout",
    )


def _provider_error_category_for_policy(
    error: Exception,
    *,
    http_timeout_category: str,
    client_timeout_category: str,
) -> str | None:
    status_code = _value(error, "status_code")
    if not isinstance(status_code, int):
        status_code = _value(_value(error, "response"), "status_code")
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return "authentication"
        if status_code == 408:
            return http_timeout_category
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "server"
        if 400 <= status_code <= 499:
            return "input"

    name = type(error).__name__.lower()
    if "timeout" in name:
        return client_timeout_category
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
