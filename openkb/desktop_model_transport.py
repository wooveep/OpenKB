"""Configured LiteLLM adapter for Desktop document analysis."""

from __future__ import annotations

from pathlib import Path

import yaml

from openkb.config import LlmCredentialBundle, load_config, resolve_credential_bundle
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelTransportError,
)


def desktop_model_gateway_for(kb_dir: Path) -> DesktopModelGateway | None:
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
        config = load_config(config_path)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return DesktopModelGateway(
            DesktopLiteLLMTransport(model=None, bundle=LlmCredentialBundle())
        )
    if bundle.api_key is None and not config_path.exists():
        return None
    return DesktopModelGateway(DesktopLiteLLMTransport(model=config.get("model"), bundle=bundle))


class DesktopLiteLLMTransport:
    """One synchronous LiteLLM request; errors remain classified by the gateway."""

    def __init__(self, *, model: object, bundle: LlmCredentialBundle) -> None:
        self._model = model
        self._bundle = bundle

    def __call__(self, request: DesktopModelRequest, timeout_seconds: float) -> object:
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
        return _response_content(response)


def _messages_for(request: DesktopModelRequest) -> list[dict[str, str]]:
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
