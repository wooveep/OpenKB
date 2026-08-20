"""KB-local Desktop model configuration entered directly in the workbench."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from openkb.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CONFIG,
    load_config_mapping,
    save_config,
)
from openkb.desktop_model_gateway import (
    INITIAL_RESPONSE_TIMEOUT_SECONDS,
    MODEL_CALL_DEADLINE_SECONDS,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseError, desktop_state_dir
from openkb.locks import kb_ingest_lock

DEFAULT_MAX_CONCURRENT_MODEL_CALLS = 1
_MAX_CONCURRENT_MODEL_CALLS = 8
MODEL_PROVIDER_CUSTOM = "custom"
MODEL_PROVIDER_DEEPSEEK = "deepseek"
_SUPPORTED_MODEL_PROVIDERS = frozenset({MODEL_PROVIDER_CUSTOM, MODEL_PROVIDER_DEEPSEEK})
_DEEPSEEK_API_HOST = "api.deepseek.com"
_OPENAI_COMPATIBLE_PROVIDER = "openai"


class DesktopModelSettingsError(DesktopKnowledgeBaseError):
    """A user-correctable Desktop model-settings validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__("desktop_model_settings_invalid", message)


@dataclass(frozen=True)
class DesktopModelSettings:
    """The complete model configuration shown by the Desktop Settings workbench."""

    provider: str
    model: str
    api_base_url: str
    api_key: str
    max_concurrent_model_calls: int
    initial_timeout_seconds: float
    model_call_deadline_seconds: float = MODEL_CALL_DEADLINE_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "api_key_configured": bool(self.api_key),
            "max_concurrent_model_calls": self.max_concurrent_model_calls,
            "initial_timeout_seconds": self.initial_timeout_seconds,
            "model_call_deadline_seconds": self.model_call_deadline_seconds,
        }

    def as_diagnostic_dict(self) -> dict[str, object]:
        """Return settings metadata without including the directly stored API Key."""
        payload = self.as_dict()
        payload.pop("api_key", None)
        return payload


def read_desktop_model_settings(kb_dir: Path) -> DesktopModelSettings:
    """Read the KB-local model configuration for direct editing in Desktop."""
    resolved = kb_dir.expanduser().resolve()
    config = _config_mapping(resolved / ".openkb" / "config.yaml")
    if "desktop" in config:
        desktop = config["desktop"]
        if not isinstance(desktop, dict):
            raise DesktopModelSettingsError("Desktop model settings must be a mapping.")
        desktop_values = desktop
    else:
        desktop_values = {}

    api_base_url = (
        DEFAULT_API_BASE_URL
        if "api_base_url" not in desktop_values
        else _required_api_base_url(desktop_values["api_base_url"])
    )
    provider = (
        _default_provider(None, api_base_url)
        if "provider" not in desktop_values
        else _configured_provider(desktop_values["provider"])
    )
    model = (
        str(DEFAULT_CONFIG["model"]) if "model" not in config else _required_model(config["model"])
    )
    api_key = (
        "" if "api_key" not in desktop_values else _configured_api_key(desktop_values["api_key"])
    )
    concurrency = (
        DEFAULT_MAX_CONCURRENT_MODEL_CALLS
        if "max_concurrent_model_calls" not in desktop_values
        else _required_concurrency(desktop_values["max_concurrent_model_calls"])
    )
    timeout = (
        INITIAL_RESPONSE_TIMEOUT_SECONDS
        if "initial_timeout_seconds" not in desktop_values
        else _required_timeout(desktop_values["initial_timeout_seconds"])
    )
    return DesktopModelSettings(
        provider=provider,
        model=_display_model_for_provider(provider, model),
        api_base_url=api_base_url,
        api_key=api_key,
        max_concurrent_model_calls=concurrency,
        initial_timeout_seconds=timeout,
    )


def save_desktop_model_settings(
    kb_dir: Path,
    *,
    provider: object = None,
    model: object,
    api_base_url: object,
    api_key: object,
    max_concurrent_model_calls: object,
    initial_timeout_seconds: object,
) -> DesktopModelSettings:
    """Persist the user-selected model connection in this Desktop Knowledge Base."""
    settings = validate_desktop_model_settings(
        provider=provider,
        model=model,
        api_base_url=api_base_url,
        api_key=api_key,
        max_concurrent_model_calls=max_concurrent_model_calls,
        initial_timeout_seconds=initial_timeout_seconds,
    )
    resolved = kb_dir.expanduser().resolve()
    config_path = resolved / ".openkb" / "config.yaml"
    with kb_ingest_lock(desktop_state_dir(resolved)):
        config = _config_mapping(config_path)
        desktop = config.get("desktop")
        desktop_values = dict(desktop) if isinstance(desktop, dict) else {}
        config["model"] = settings.model
        desktop_values.update(
            {
                "provider": settings.provider,
                "api_base_url": settings.api_base_url,
                "api_key": settings.api_key,
                "max_concurrent_model_calls": settings.max_concurrent_model_calls,
                "initial_timeout_seconds": settings.initial_timeout_seconds,
            }
        )
        desktop_values.pop("credential_reference", None)
        config["desktop"] = desktop_values
        save_config(config_path, config)
    return read_desktop_model_settings(resolved)


def validate_desktop_model_settings(
    *,
    provider: object = None,
    model: object,
    api_base_url: object,
    api_key: object,
    max_concurrent_model_calls: object,
    initial_timeout_seconds: object,
) -> DesktopModelSettings:
    """Validate a Settings draft without persisting it."""
    normalized_api_base_url = _required_api_base_url(api_base_url)
    normalized_provider = _required_provider(provider, normalized_api_base_url)
    normalized_model = _display_model_for_provider(normalized_provider, _required_model(model))
    normalized_api_key = _required_api_key(api_key)
    normalized_concurrency = _required_concurrency(max_concurrent_model_calls)
    normalized_timeout = _required_timeout(initial_timeout_seconds)
    return DesktopModelSettings(
        provider=normalized_provider,
        model=normalized_model,
        api_base_url=normalized_api_base_url,
        api_key=normalized_api_key,
        max_concurrent_model_calls=normalized_concurrency,
        initial_timeout_seconds=normalized_timeout,
    )


def _config_mapping(path: Path) -> dict[str, Any]:
    try:
        return load_config_mapping(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise DesktopModelSettingsError("Desktop model settings could not be read.") from error


def _default_provider(value: object, api_base_url: str) -> str:
    if isinstance(value, str) and value.strip() in _SUPPORTED_MODEL_PROVIDERS:
        return value.strip()
    return (
        MODEL_PROVIDER_DEEPSEEK
        if _is_deepseek_api_base_url(api_base_url)
        else MODEL_PROVIDER_CUSTOM
    )


def _configured_provider(value: object) -> str:
    if isinstance(value, str) and value.strip() in _SUPPORTED_MODEL_PROVIDERS:
        return value.strip()
    raise DesktopModelSettingsError("Choose a supported model provider.")


def _required_provider(value: object, api_base_url: str) -> str:
    if value is None:
        return _default_provider(value, api_base_url)
    if isinstance(value, str) and value.strip() in _SUPPORTED_MODEL_PROVIDERS:
        return value.strip()
    raise DesktopModelSettingsError("Choose a supported model provider.")


def litellm_model_identifier(provider: str, model: object) -> object:
    """Return LiteLLM's routed model identifier without changing the saved model name."""
    if not isinstance(model, str):
        return model
    normalized_model = model.strip()
    if not normalized_model:
        return model
    if provider == MODEL_PROVIDER_DEEPSEEK:
        return _with_litellm_provider(MODEL_PROVIDER_DEEPSEEK, normalized_model)
    if provider == MODEL_PROVIDER_CUSTOM:
        return _with_litellm_provider(_OPENAI_COMPATIBLE_PROVIDER, normalized_model)
    return model


def _display_model_for_provider(provider: str, model: str) -> str:
    routed_provider = (
        MODEL_PROVIDER_DEEPSEEK
        if provider == MODEL_PROVIDER_DEEPSEEK
        else _OPENAI_COMPATIBLE_PROVIDER
        if provider == MODEL_PROVIDER_CUSTOM
        else None
    )
    if routed_provider is not None:
        prefix = f"{routed_provider}/"
        if model.startswith(prefix):
            return model.removeprefix(prefix)
    return model


def _with_litellm_provider(provider: str, model: str) -> str:
    prefix = f"{provider}/"
    return model if model.startswith(prefix) else f"{prefix}{model}"


def _required_model(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Choose a non-empty default model.")
    return value.strip()


def _is_deepseek_api_base_url(api_base_url: str) -> bool:
    return urlsplit(api_base_url).hostname == _DEEPSEEK_API_HOST


def _required_api_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Enter a non-empty API Base URL.")
    normalized = value.strip()
    try:
        urlsplit(normalized)
    except ValueError as error:
        raise DesktopModelSettingsError("Enter a valid API Base URL.") from error
    return normalized


def _configured_api_key(value: object) -> str:
    if not isinstance(value, str):
        raise DesktopModelSettingsError("Desktop API Key must be a string.")
    return value.strip()


def _required_api_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Enter a non-empty API Key.")
    return value.strip()


def _required_concurrency(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= _MAX_CONCURRENT_MODEL_CALLS
    ):
        raise DesktopModelSettingsError(
            f"Model concurrency must be an integer between 1 and {_MAX_CONCURRENT_MODEL_CALLS}."
        )
    return value


def _required_timeout(value: object) -> float:
    message = "Model response timeout must be a number between 1 and 60 seconds."
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DesktopModelSettingsError(message)
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise DesktopModelSettingsError(message) from error
    if not math.isfinite(normalized) or not 0 < normalized <= MODEL_CALL_DEADLINE_SECONDS:
        raise DesktopModelSettingsError(message)
    return normalized
