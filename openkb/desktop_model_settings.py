"""Safe, KB-local Desktop model defaults and credential references."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from openkb.config import (
    DEFAULT_CONFIG,
    DEFAULT_DESKTOP_CREDENTIAL_REFERENCE,
    credential_reference_environment_variable,
    load_config_mapping,
    resolve_credential_bundle,
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
    """The non-secret defaults shown by the Desktop Settings workbench."""

    model: str
    credential_reference: str
    credential_available: bool
    max_concurrent_model_calls: int
    initial_timeout_seconds: float
    model_call_deadline_seconds: float = MODEL_CALL_DEADLINE_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "credential_reference": self.credential_reference,
            "credential_available": self.credential_available,
            "max_concurrent_model_calls": self.max_concurrent_model_calls,
            "initial_timeout_seconds": self.initial_timeout_seconds,
            "model_call_deadline_seconds": self.model_call_deadline_seconds,
        }


def read_desktop_model_settings(kb_dir: Path) -> DesktopModelSettings:
    """Read settings without exposing the credential value itself."""
    resolved = kb_dir.expanduser().resolve()
    config = _config_mapping(resolved / ".openkb" / "config.yaml")
    desktop = config.get("desktop")
    desktop_values = desktop if isinstance(desktop, dict) else {}
    model = _default_model(config.get("model"))
    credential_reference = _default_credential_reference(desktop_values.get("credential_reference"))
    return DesktopModelSettings(
        model=model,
        credential_reference=credential_reference,
        credential_available=_credential_available(resolved),
        max_concurrent_model_calls=_default_concurrency(
            desktop_values.get("max_concurrent_model_calls")
        ),
        initial_timeout_seconds=_default_timeout(desktop_values.get("initial_timeout_seconds")),
    )


def save_desktop_model_settings(
    kb_dir: Path,
    *,
    model: object,
    credential_reference: object,
    max_concurrent_model_calls: object,
    initial_timeout_seconds: object,
) -> DesktopModelSettings:
    """Persist only safe defaults; secrets stay in environment-backed references."""
    normalized_model = _required_model(model)
    normalized_reference = _required_credential_reference(credential_reference)
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
                "credential_reference": normalized_reference,
                "max_concurrent_model_calls": normalized_concurrency,
                "initial_timeout_seconds": normalized_timeout,
            }
        )
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


def _default_credential_reference(value: object) -> str:
    if _credential_env_name(value) is not None:
        assert isinstance(value, str)
        return value.strip()
    return DEFAULT_DESKTOP_CREDENTIAL_REFERENCE


def _required_credential_reference(value: object) -> str:
    if _credential_env_name(value) is None:
        raise DesktopModelSettingsError("Credential reference must use the form env:VARIABLE_NAME.")
    assert isinstance(value, str)
    return value.strip()


def _credential_env_name(value: object) -> str | None:
    return credential_reference_environment_variable(value)


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


def _credential_available(kb_dir: Path) -> bool:
    try:
        return bool(resolve_credential_bundle(kb_dir).api_key)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return False
