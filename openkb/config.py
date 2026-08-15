"""KB-local configuration used by the Desktop Engine."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openkb.locks import atomic_write_text

DEFAULT_CONFIG: dict[str, Any] = {"model": "gpt-5.4"}
DEFAULT_DESKTOP_CREDENTIAL_REFERENCE = "env:LLM_API_KEY"
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "openkb"

_CREDENTIAL_REFERENCE_RE = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]*)\Z")


def credential_reference_environment_variable(value: object) -> str | None:
    """Return the variable name encoded by a non-secret ``env:NAME`` reference."""
    if not isinstance(value, str):
        return None
    match = _CREDENTIAL_REFERENCE_RE.fullmatch(value.strip())
    return match.group(1) if match else None


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
    """Resolve the active KB's environment-backed LLM configuration.

    The KB-local ``.env`` wins over the host environment, followed by the
    optional application-wide ``~/.config/openkb/.env`` fallback. Credentials
    never cross the Desktop Bridge or get written to ``config.yaml``.
    """
    resolved = kb_dir.expanduser().resolve()
    config = load_config_mapping(resolved / ".openkb" / "config.yaml")
    values = _environment_values(resolved)
    desktop = config.get("desktop")
    reference = desktop.get("credential_reference") if isinstance(desktop, dict) else None
    credential_name = credential_reference_environment_variable(reference) or "LLM_API_KEY"
    return LlmCredentialBundle(
        api_key=values.get(credential_name),
        base_url=values.get("OPENAI_API_BASE"),
        extra_headers=_extra_headers(config),
    )


def _environment_values(kb_dir: Path) -> dict[str, str]:
    local = _dotenv_values(kb_dir / ".env")
    global_values = _dotenv_values(GLOBAL_CONFIG_DIR / ".env")
    names = set(global_values) | set(os.environ) | set(local)
    return {
        name: value
        for name in names
        if (value := local.get(name) or os.environ.get(name) or global_values.get(name))
    }


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    from dotenv import dotenv_values

    return {
        key: value
        for key, value in dotenv_values(path).items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


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
