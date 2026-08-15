"""Configured LiteLLM adapter for Desktop document analysis."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from openkb.config import LlmCredentialBundle, load_config, resolve_credential_bundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_model_gateway import (
    INITIAL_RESPONSE_TIMEOUT_SECONDS,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelTransportError,
)


def desktop_model_gateway_for(
    kb_dir: Path, override: DesktopRecoveryOverride | None = None
) -> DesktopModelGateway | None:
    """Build the live gateway when this Desktop KB has opted into a model config.

    Fresh Desktop knowledge bases can still establish local retrieval before the
    settings ticket supplies credentials. Once a config file or credential is
    present, configuration failures are surfaced through the required Model
    Gateway path rather than silently skipped.
    """
    resolved = kb_dir.expanduser().resolve()
    config_path = resolved / ".openkb" / "config.yaml"
    try:
        bundle = resolve_credential_bundle(resolved)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        bundle = LlmCredentialBundle()
    try:
        config = load_config(config_path)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return _gateway_for(override.model if override is not None else None, bundle, override)
    model = (
        override.model
        if override is not None and override.model is not None
        else config.get("model")
    )
    if bundle.api_key is None and not config_path.exists() and override is None:
        return None
    return _gateway_for(model, bundle, override)


def _gateway_for(
    model: object, bundle: LlmCredentialBundle, override: DesktopRecoveryOverride | None
) -> DesktopModelGateway:
    timeout = (
        override.initial_timeout_seconds
        if override is not None and override.initial_timeout_seconds is not None
        else INITIAL_RESPONSE_TIMEOUT_SECONDS
    )
    return DesktopModelGateway(
        DesktopLiteLLMTransport(model=model, bundle=bundle), initial_timeout_seconds=timeout
    )


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
        if not hasattr(response, "__iter__"):
            raise DesktopModelTransportError("response_format")
        parts: list[str] = []
        for chunk in response:
            delta = _stream_delta(chunk)
            if delta:
                parts.append(delta)
                on_delta(delta)
        return "".join(parts)

    def _completion(
        self, request: DesktopModelRequest, timeout_seconds: float, *, stream: bool
    ) -> object:
        if not isinstance(self._model, str) or not self._model.strip():
            raise DesktopModelTransportError("configuration")
        if not self._bundle.api_key:
            raise DesktopModelTransportError("configuration")

        try:
            from litellm import completion

            response = completion(
                model=self._model,
                messages=_messages_for(request),
                timeout=timeout_seconds,
                api_key=self._bundle.api_key,
                base_url=self._bundle.base_url,
                **({"stream": True} if stream else {}),
                **(
                    {"extra_headers": self._bundle.extra_headers}
                    if self._bundle.extra_headers
                    else {}
                ),
            )
        except Exception as error:
            category = _provider_error_category(error)
            if category is not None:
                raise DesktopModelTransportError(category) from error
            raise
        return response


def _messages_for(request: DesktopModelRequest) -> list[dict[str, str]]:
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
    status_code = _value(error, "status_code")
    if not isinstance(status_code, int):
        status_code = _value(_value(error, "response"), "status_code")
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return "authentication"
        if status_code == 408:
            return "timeout"
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "server"
        if 400 <= status_code <= 499:
            return "input"

    name = type(error).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "rate" in name and "limit" in name:
        return "rate_limited"
    if any(fragment in name for fragment in ("authentication", "permission", "unauthorized")):
        return "authentication"
    if any(fragment in name for fragment in ("connection", "network")):
        return "network"
    if any(fragment in name for fragment in ("internalserver", "serviceunavailable")):
        return "server"
    return None


def _value(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
