"""KB-local Desktop model configuration entered directly in the workbench."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit

import yaml

from openkb.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CONFIG,
    load_config_mapping,
    save_config,
)
from openkb.desktop_model_capabilities import (
    DesktopModelCapabilityProfile,
    model_capability_profile,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseError, desktop_state_dir
from openkb.locks import kb_ingest_lock

DEFAULT_MAX_CONCURRENT_MODEL_CALLS = 2
_MAX_CONCURRENT_MODEL_CALLS = 4
MODEL_PROVIDER_CUSTOM = "custom"
MODEL_PROVIDER_DEEPSEEK = "deepseek"
_SUPPORTED_MODEL_PROVIDERS = frozenset({MODEL_PROVIDER_CUSTOM, MODEL_PROVIDER_DEEPSEEK})
_DEEPSEEK_API_HOST = "api.deepseek.com"
_OPENAI_COMPATIBLE_PROVIDER = "openai"
_REASONING_SETTINGS = frozenset({"off", "low", "medium", "high"})


class _RoleValues(TypedDict):
    analysis_model: str | None
    answer_model: str | None
    default_context_capacity: int | None
    analysis_context_capacity: int | None
    answer_context_capacity: int | None
    default_reasoning: str | None
    analysis_reasoning: str | None
    answer_reasoning: str | None
    default_input_price_per_million: float | None
    default_output_price_per_million: float | None
    analysis_input_price_per_million: float | None
    analysis_output_price_per_million: float | None
    answer_input_price_per_million: float | None
    answer_output_price_per_million: float | None


class DesktopModelSettingsError(DesktopKnowledgeBaseError):
    """A user-correctable Desktop model-settings validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__("desktop_model_settings_invalid", message)


@dataclass(frozen=True)
class DesktopModelRoleSettings:
    """Resolved settings for one capability role after default-role inheritance."""

    model: str
    context_capacity: int | None
    reasoning: str | None
    input_price_per_million: float | None
    output_price_per_million: float | None


@dataclass(frozen=True)
class DesktopModelSettings:
    """The complete model configuration shown by the Desktop Settings workbench."""

    provider: str
    model: str
    api_base_url: str
    api_key: str
    max_concurrent_model_calls: int
    analysis_model: str | None = None
    answer_model: str | None = None
    default_context_capacity: int | None = None
    analysis_context_capacity: int | None = None
    answer_context_capacity: int | None = None
    default_reasoning: str | None = None
    analysis_reasoning: str | None = None
    answer_reasoning: str | None = None
    default_input_price_per_million: float | None = None
    default_output_price_per_million: float | None = None
    analysis_input_price_per_million: float | None = None
    analysis_output_price_per_million: float | None = None
    answer_input_price_per_million: float | None = None
    answer_output_price_per_million: float | None = None

    @property
    def analysis_model_name(self) -> str:
        return self.role_settings("analysis").model

    @property
    def answer_model_name(self) -> str:
        return self.role_settings("answer").model

    def role_settings(self, role: str) -> DesktopModelRoleSettings:
        """Resolve one role through the single default-inheritance policy."""
        default = DesktopModelRoleSettings(
            model=self.model,
            context_capacity=self.default_context_capacity,
            reasoning=self.default_reasoning,
            input_price_per_million=self.default_input_price_per_million,
            output_price_per_million=self.default_output_price_per_million,
        )
        configured = {
            "default": default,
            "analysis": DesktopModelRoleSettings(
                model=self.analysis_model or default.model,
                context_capacity=(
                    self.analysis_context_capacity
                    if self.analysis_context_capacity is not None
                    else default.context_capacity
                ),
                reasoning=(
                    self.analysis_reasoning
                    if self.analysis_reasoning is not None
                    else default.reasoning
                ),
                input_price_per_million=(
                    self.analysis_input_price_per_million
                    if self.analysis_input_price_per_million is not None
                    else default.input_price_per_million
                ),
                output_price_per_million=(
                    self.analysis_output_price_per_million
                    if self.analysis_output_price_per_million is not None
                    else default.output_price_per_million
                ),
            ),
            "answer": DesktopModelRoleSettings(
                model=self.answer_model or default.model,
                context_capacity=(
                    self.answer_context_capacity
                    if self.answer_context_capacity is not None
                    else default.context_capacity
                ),
                reasoning=(
                    self.answer_reasoning
                    if self.answer_reasoning is not None
                    else default.reasoning
                ),
                input_price_per_million=(
                    self.answer_input_price_per_million
                    if self.answer_input_price_per_million is not None
                    else default.input_price_per_million
                ),
                output_price_per_million=(
                    self.answer_output_price_per_million
                    if self.answer_output_price_per_million is not None
                    else default.output_price_per_million
                ),
            ),
        }
        try:
            return configured[role]
        except KeyError as error:
            raise ValueError(f"Unknown model role: {role}") from error

    def model_for_role(self, role: str) -> str:
        return self.role_settings(role).model

    def capability_for_role(self, role: str) -> DesktopModelCapabilityProfile:
        role_settings = self.role_settings(role)
        return model_capability_profile(
            role_settings.model,
            context_capacity=role_settings.context_capacity,
        )

    def reasoning_for_role(self, role: str) -> str | None:
        return self.role_settings(role).reasoning

    def pricing_for_role(self, role: str) -> tuple[float | None, float | None]:
        """Return only prices explicitly supplied by the user, with default-role fallback."""
        settings = self.role_settings(role)
        return settings.input_price_per_million, settings.output_price_per_million

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "api_key_configured": bool(self.api_key),
            "max_concurrent_model_calls": self.max_concurrent_model_calls,
            "analysis_model": self.analysis_model,
            "answer_model": self.answer_model,
            "analysis_concurrency": self.max_concurrent_model_calls,
            "default_context_capacity": self.default_context_capacity,
            "analysis_context_capacity": self.analysis_context_capacity,
            "answer_context_capacity": self.answer_context_capacity,
            "default_reasoning": self.default_reasoning,
            "analysis_reasoning": self.analysis_reasoning,
            "answer_reasoning": self.answer_reasoning,
            "default_input_price_per_million": self.default_input_price_per_million,
            "default_output_price_per_million": self.default_output_price_per_million,
            "analysis_input_price_per_million": self.analysis_input_price_per_million,
            "analysis_output_price_per_million": self.analysis_output_price_per_million,
            "answer_input_price_per_million": self.answer_input_price_per_million,
            "answer_output_price_per_million": self.answer_output_price_per_million,
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
    role_values = _role_values(desktop_values)
    return DesktopModelSettings(
        provider=provider,
        model=_display_model_for_provider(provider, model),
        api_base_url=api_base_url,
        api_key=api_key,
        max_concurrent_model_calls=concurrency,
        **role_values,
    )


def save_desktop_model_settings(
    kb_dir: Path,
    *,
    provider: object = None,
    model: object,
    api_base_url: object,
    api_key: object,
    max_concurrent_model_calls: object,
    analysis_model: object = None,
    answer_model: object = None,
    default_context_capacity: object = None,
    analysis_context_capacity: object = None,
    answer_context_capacity: object = None,
    default_reasoning: object = None,
    analysis_reasoning: object = None,
    answer_reasoning: object = None,
    default_input_price_per_million: object = None,
    default_output_price_per_million: object = None,
    analysis_input_price_per_million: object = None,
    analysis_output_price_per_million: object = None,
    answer_input_price_per_million: object = None,
    answer_output_price_per_million: object = None,
    **legacy_values: object,
) -> DesktopModelSettings:
    """Persist the user-selected model connection in this Desktop Knowledge Base."""
    _ignore_legacy_timeout_values(legacy_values)
    settings = validate_desktop_model_settings(
        provider=provider,
        model=model,
        api_base_url=api_base_url,
        api_key=api_key,
        max_concurrent_model_calls=max_concurrent_model_calls,
        analysis_model=analysis_model,
        answer_model=answer_model,
        default_context_capacity=default_context_capacity,
        analysis_context_capacity=analysis_context_capacity,
        answer_context_capacity=answer_context_capacity,
        default_reasoning=default_reasoning,
        analysis_reasoning=analysis_reasoning,
        answer_reasoning=answer_reasoning,
        default_input_price_per_million=default_input_price_per_million,
        default_output_price_per_million=default_output_price_per_million,
        analysis_input_price_per_million=analysis_input_price_per_million,
        analysis_output_price_per_million=analysis_output_price_per_million,
        answer_input_price_per_million=answer_input_price_per_million,
        answer_output_price_per_million=answer_output_price_per_million,
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
                "analysis_model": settings.analysis_model,
                "answer_model": settings.answer_model,
                "default_context_capacity": settings.default_context_capacity,
                "analysis_context_capacity": settings.analysis_context_capacity,
                "answer_context_capacity": settings.answer_context_capacity,
                "default_reasoning": settings.default_reasoning,
                "analysis_reasoning": settings.analysis_reasoning,
                "answer_reasoning": settings.answer_reasoning,
                "default_input_price_per_million": settings.default_input_price_per_million,
                "default_output_price_per_million": settings.default_output_price_per_million,
                "analysis_input_price_per_million": settings.analysis_input_price_per_million,
                "analysis_output_price_per_million": settings.analysis_output_price_per_million,
                "answer_input_price_per_million": settings.answer_input_price_per_million,
                "answer_output_price_per_million": settings.answer_output_price_per_million,
            }
        )
        desktop_values.pop("credential_reference", None)
        desktop_values.pop("initial_timeout_seconds", None)
        desktop_values.pop("model_call_deadline_seconds", None)
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
    analysis_model: object = None,
    answer_model: object = None,
    default_context_capacity: object = None,
    analysis_context_capacity: object = None,
    answer_context_capacity: object = None,
    default_reasoning: object = None,
    analysis_reasoning: object = None,
    answer_reasoning: object = None,
    default_input_price_per_million: object = None,
    default_output_price_per_million: object = None,
    analysis_input_price_per_million: object = None,
    analysis_output_price_per_million: object = None,
    answer_input_price_per_million: object = None,
    answer_output_price_per_million: object = None,
    **legacy_values: object,
) -> DesktopModelSettings:
    """Validate a Settings draft without persisting it."""
    _ignore_legacy_timeout_values(legacy_values)
    normalized_api_base_url = _required_api_base_url(api_base_url)
    normalized_provider = _required_provider(provider, normalized_api_base_url)
    normalized_model = _display_model_for_provider(normalized_provider, _required_model(model))
    normalized_api_key = _required_api_key(api_key)
    normalized_concurrency = _required_concurrency(max_concurrent_model_calls)
    role_values: _RoleValues = {
        "analysis_model": _optional_model(analysis_model),
        "answer_model": _optional_model(answer_model),
        "default_context_capacity": _optional_context_capacity(default_context_capacity),
        "analysis_context_capacity": _optional_context_capacity(analysis_context_capacity),
        "answer_context_capacity": _optional_context_capacity(answer_context_capacity),
        "default_reasoning": _optional_reasoning(default_reasoning),
        "analysis_reasoning": _optional_reasoning(analysis_reasoning),
        "answer_reasoning": _optional_reasoning(answer_reasoning),
        "default_input_price_per_million": _optional_price(default_input_price_per_million),
        "default_output_price_per_million": _optional_price(default_output_price_per_million),
        "analysis_input_price_per_million": _optional_price(analysis_input_price_per_million),
        "analysis_output_price_per_million": _optional_price(analysis_output_price_per_million),
        "answer_input_price_per_million": _optional_price(answer_input_price_per_million),
        "answer_output_price_per_million": _optional_price(answer_output_price_per_million),
    }
    return DesktopModelSettings(
        provider=normalized_provider,
        model=normalized_model,
        api_base_url=normalized_api_base_url,
        api_key=normalized_api_key,
        max_concurrent_model_calls=normalized_concurrency,
        **role_values,
    )


def _role_values(values: dict[str, Any]) -> _RoleValues:
    return {
        "analysis_model": _optional_model(values.get("analysis_model")),
        "answer_model": _optional_model(values.get("answer_model")),
        "default_context_capacity": _optional_context_capacity(
            values.get("default_context_capacity")
        ),
        "analysis_context_capacity": _optional_context_capacity(
            values.get("analysis_context_capacity")
        ),
        "answer_context_capacity": _optional_context_capacity(
            values.get("answer_context_capacity")
        ),
        "default_reasoning": _optional_reasoning(values.get("default_reasoning")),
        "analysis_reasoning": _optional_reasoning(values.get("analysis_reasoning")),
        "answer_reasoning": _optional_reasoning(values.get("answer_reasoning")),
        "default_input_price_per_million": _optional_price(
            values.get("default_input_price_per_million")
        ),
        "default_output_price_per_million": _optional_price(
            values.get("default_output_price_per_million")
        ),
        "analysis_input_price_per_million": _optional_price(
            values.get("analysis_input_price_per_million")
        ),
        "analysis_output_price_per_million": _optional_price(
            values.get("analysis_output_price_per_million")
        ),
        "answer_input_price_per_million": _optional_price(
            values.get("answer_input_price_per_million")
        ),
        "answer_output_price_per_million": _optional_price(
            values.get("answer_output_price_per_million")
        ),
    }


def _ignore_legacy_timeout_values(values: dict[str, object]) -> None:
    unexpected = set(values) - {"initial_timeout_seconds", "model_call_deadline_seconds"}
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise TypeError(f"Unexpected Desktop model setting: {names}")


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


def _optional_model(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise DesktopModelSettingsError("Optional role models must be non-empty strings.")
    return value.strip()


def _optional_context_capacity(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 4_096:
        raise DesktopModelSettingsError(
            "Model context-capacity overrides must be integers of at least 4096 tokens."
        )
    return value


def _optional_reasoning(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in _REASONING_SETTINGS:
        raise DesktopModelSettingsError(
            "Reasoning must use provider behavior, off, low, medium, or high."
        )
    return value


def _optional_price(value: object) -> float | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DesktopModelSettingsError("Optional model pricing must be a non-negative number.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise DesktopModelSettingsError("Optional model pricing must be a non-negative number.")
    return normalized


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
