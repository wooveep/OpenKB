"""KB-local Desktop model configuration entered directly in the workbench."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class DesktopModelSettingsError(DesktopKnowledgeBaseError):
    """A user-correctable Desktop model-settings validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__("desktop_model_settings_invalid", message)


@dataclass(frozen=True)
class DesktopModelSettings:
    """The complete model configuration shown by the Desktop Settings workbench."""

    model: str
    api_base_url: str
    api_key: str
    max_concurrent_model_calls: int
    initial_timeout_seconds: float
    model_call_deadline_seconds: float = MODEL_CALL_DEADLINE_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
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
    desktop = config.get("desktop")
    desktop_values = desktop if isinstance(desktop, dict) else {}
    model = _default_model(config.get("model"))
    return DesktopModelSettings(
        model=model,
        api_base_url=_default_api_base_url(desktop_values.get("api_base_url")),
        api_key=_default_api_key(desktop_values.get("api_key")),
        max_concurrent_model_calls=_default_concurrency(
            desktop_values.get("max_concurrent_model_calls")
        ),
        initial_timeout_seconds=_default_timeout(desktop_values.get("initial_timeout_seconds")),
    )


def save_desktop_model_settings(
    kb_dir: Path,
    *,
    model: object,
    api_base_url: object,
    api_key: object,
    max_concurrent_model_calls: object,
    initial_timeout_seconds: object,
) -> DesktopModelSettings:
    """Persist the user-selected model connection in this Desktop Knowledge Base."""
    normalized_model = _required_model(model)
    normalized_api_base_url = _required_api_base_url(api_base_url)
    normalized_api_key = _required_api_key(api_key)
    normalized_concurrency = _required_concurrency(max_concurrent_model_calls)
    normalized_timeout = _required_timeout(initial_timeout_seconds)
    resolved = kb_dir.expanduser().resolve()
    config_path = resolved / ".openkb" / "config.yaml"
    with kb_ingest_lock(desktop_state_dir(resolved)):
        config = _config_mapping(config_path)
        desktop = config.get("desktop")
        desktop_values = dict(desktop) if isinstance(desktop, dict) else {}
        config["model"] = normalized_model
        desktop_values.update(
            {
                "api_base_url": normalized_api_base_url,
                "api_key": normalized_api_key,
                "max_concurrent_model_calls": normalized_concurrency,
                "initial_timeout_seconds": normalized_timeout,
            }
        )
        desktop_values.pop("credential_reference", None)
        config["desktop"] = desktop_values
        save_config(config_path, config)
    return read_desktop_model_settings(resolved)


def _config_mapping(path: Path) -> dict[str, Any]:
    try:
        return load_config_mapping(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise DesktopModelSettingsError("Desktop model settings could not be read.") from error


def _default_model(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(DEFAULT_CONFIG["model"])


def _required_model(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Choose a non-empty default model.")
    return value.strip()


def _default_api_base_url(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_API_BASE_URL


def _required_api_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Enter a non-empty API Base URL.")
    return value.strip()


def _default_api_key(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _required_api_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Enter a non-empty API Key.")
    return value.strip()


def _default_concurrency(value: object) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_CONCURRENT_MODEL_CALLS
    ):
        return value
    return DEFAULT_MAX_CONCURRENT_MODEL_CALLS


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


def _default_timeout(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and _valid_timeout(value):
        return float(value)
    return INITIAL_RESPONSE_TIMEOUT_SECONDS


def _required_timeout(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not _valid_timeout(value):
        raise DesktopModelSettingsError(
            "Model response timeout must be a number between 1 and 60 seconds."
        )
    return float(value)


def _valid_timeout(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 < float(value) <= MODEL_CALL_DEADLINE_SECONDS
    )
