"""KB-local configuration used by the Desktop Engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openkb.locks import atomic_write_text

DEFAULT_CONFIG: dict[str, Any] = {"model": "gpt-5.4"}
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"


def load_config_mapping(config_path: Path) -> dict[str, Any]:
    """Read one Desktop KB configuration file without injecting defaults."""
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError("OpenKB config must be a YAML mapping.")
    return dict(data)


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    """Atomically persist Desktop KB settings."""
    atomic_write_text(
        config_path,
        yaml.safe_dump(config, allow_unicode=True, sort_keys=True),
    )


@dataclass(frozen=True)
class LlmCredentialBundle:
    """One resolved, non-persisted provider configuration for a Desktop request."""

    api_key: str | None = None
    base_url: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


def resolve_credential_bundle(kb_dir: Path) -> LlmCredentialBundle:
    """Resolve the active KB's directly configured provider connection."""
    resolved = kb_dir.expanduser().resolve()
    config = load_config_mapping(resolved / ".openkb" / "config.yaml")
    desktop = config.get("desktop")
    desktop_values = desktop if isinstance(desktop, dict) else {}
    return LlmCredentialBundle(
        api_key=_configured_text(desktop_values.get("api_key")),
        base_url=_configured_text(desktop_values.get("api_base_url")) or DEFAULT_API_BASE_URL,
        extra_headers=_extra_headers(config),
    )


def preferred_knowledge_language(kb_dir: Path) -> str | None:
    """Return the optional KB-wide synthesized-page language override."""
    config = load_config_mapping(kb_dir.expanduser().resolve() / ".openkb" / "config.yaml")
    knowledge = config.get("knowledge")
    values = knowledge if isinstance(knowledge, dict) else {}
    language = _configured_text(values.get("language"))
    return language if language in {"zh", "en"} else None


def _configured_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate or None


def _extra_headers(config: dict[str, Any]) -> dict[str, str]:
    raw = config.get("extra_headers")
    if not isinstance(raw, dict):
        return {}
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.strip() and isinstance(value, (str, int, float, bool)):
            headers[key.strip()] = str(value)
    return headers


def validate_timeout_seconds(value: object) -> float | None:
    """Return a finite positive timeout value when a caller needs one."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None
