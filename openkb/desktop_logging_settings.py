"""Normalized, process-independent settings for Desktop diagnostics."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

TRACE_LEVEL = 5
MAX_SENSITIVE_TRACE_DURATION = timedelta(hours=24)

DIAGNOSTIC_COMPONENTS = (
    "shell",
    "bridge",
    "runtime",
    "import",
    "parser",
    "model",
    "page_tree",
    "retrieval",
    "knowledge",
    "projection",
    "storage",
)

_LEVELS = {
    "TRACE": TRACE_LEVEL,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}


def normalize_level_name(value: object) -> str | None:
    """Return one canonical diagnostic level, accepting the WARNING alias."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized == "WARNING":
        normalized = "WARN"
    return normalized if normalized in _LEVELS else None


def _parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not (normalized.endswith("Z") or normalized.endswith("+00:00")):
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _default_sensitive_trace_root(environ: Mapping[str, str]) -> Path:
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "OpenKB" / "sensitive-traces"
    return Path.home() / ".local" / "share" / "OpenKB" / "sensitive-traces"


@dataclass(frozen=True)
class DiagnosticLoggingSettings:
    """Settings already normalized by the Shell, with Engine-side validation."""

    level_name: str = "WARN"
    component_levels: Mapping[str, str] = field(default_factory=dict)
    runtime_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    log_directory: Path | None = None
    allow_sensitive_trace: bool = False
    sensitive_trace_expires_at: datetime | None = None
    sensitive_trace_capture_id: str | None = None
    sensitive_trace_root: Path | None = None
    sensitive_trace_stop_file: Path | None = None
    warnings: tuple[str, ...] = ()

    def configured_level_name(self, component: str) -> str:
        return self.component_levels.get(component, self.level_name)

    def sensitive_trace_enabled_at(self, now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires_at = self.sensitive_trace_expires_at
        if not self.allow_sensitive_trace or expires_at is None:
            return False
        if not self.sensitive_trace_capture_id or self.sensitive_trace_root is None:
            return False
        if current >= expires_at:
            return False
        stop_file = self.sensitive_trace_stop_file
        return stop_file is None or not stop_file.exists()

    @property
    def sensitive_trace_enabled(self) -> bool:
        return self.sensitive_trace_enabled_at()

    @property
    def trace_components(self) -> tuple[str, ...]:
        return tuple(
            component
            for component in DIAGNOSTIC_COMPONENTS
            if self.configured_level_name(component) == "TRACE"
        )

    def effective_level(self, component: str, *, now: datetime | None = None) -> int:
        return _LEVELS[self.effective_level_name(component, now=now)]

    def effective_level_name(self, component: str, *, now: datetime | None = None) -> str:
        """Return the canonical level actually active for one component."""
        configured = self.configured_level_name(component)
        if self.trace_components and not self.sensitive_trace_enabled_at(now):
            return "WARN"
        return configured if configured in _LEVELS else "WARN"


def settings_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> DiagnosticLoggingSettings:
    """Parse the Shell's private, normalized environment contract fail-closed."""
    source = os.environ if environ is None else environ
    warnings: list[str] = []
    contract_invalid = False
    level = normalize_level_name(source.get("OPENKB_LOG_LEVEL", "WARN"))
    if level is None:
        level = "WARN"
        warnings.append("logging_level_invalid")
        contract_invalid = True

    component_levels: dict[str, str] = {}
    raw_components = source.get("OPENKB_LOG_COMPONENT_LEVELS")
    if raw_components:
        try:
            decoded = json.loads(raw_components)
        except (TypeError, ValueError):
            decoded = None
        if not isinstance(decoded, dict):
            warnings.append("logging_component_levels_invalid")
            contract_invalid = True
        else:
            for component, raw_level in decoded.items():
                normalized = normalize_level_name(raw_level)
                if component not in DIAGNOSTIC_COMPONENTS:
                    warnings.append("logging_component_override_ignored")
                    continue
                if normalized is None:
                    warnings.append("logging_component_override_invalid")
                    contract_invalid = True
                    continue
                component_levels[component] = normalized

    if contract_invalid:
        level = "WARN"
        component_levels = {}

    runtime_session_id = source.get("OPENKB_RUNTIME_SESSION_ID", "").strip() or str(uuid.uuid4())
    allow_sensitive_trace = source.get("OPENKB_ALLOW_SENSITIVE_TRACE", "").lower() == "true"
    expires_at = _parse_utc_datetime(source.get("OPENKB_SENSITIVE_TRACE_EXPIRES_AT"))
    capture_id = source.get("OPENKB_SENSITIVE_TRACE_CAPTURE_ID", "").strip() or None
    trace_root_text = source.get("OPENKB_SENSITIVE_TRACE_ROOT", "").strip()
    trace_root = Path(trace_root_text) if trace_root_text else _default_sensitive_trace_root(source)
    stop_file_text = source.get("OPENKB_SENSITIVE_TRACE_STOP_FILE", "").strip()
    stop_file = (
        Path(stop_file_text)
        if stop_file_text
        else trace_root / capture_id / ".stop"
        if capture_id is not None
        else None
    )
    log_directory_text = source.get("OPENKB_LOG_DIR", "").strip()
    log_directory = Path(log_directory_text).expanduser() if log_directory_text else None

    trace_requested = level == "TRACE" or "TRACE" in component_levels.values()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    authorization_valid = (
        allow_sensitive_trace
        and expires_at is not None
        and current < expires_at <= current + MAX_SENSITIVE_TRACE_DURATION
        and capture_id is not None
    )
    if trace_requested and not authorization_valid:
        level = "WARN"
        component_levels = {}
        allow_sensitive_trace = False
        expires_at = None
        capture_id = None
        warnings.append("sensitive_trace_authorization_invalid")

    return DiagnosticLoggingSettings(
        level_name=level,
        component_levels=component_levels,
        runtime_session_id=runtime_session_id,
        log_directory=log_directory,
        allow_sensitive_trace=allow_sensitive_trace,
        sensitive_trace_expires_at=expires_at,
        sensitive_trace_capture_id=capture_id,
        sensitive_trace_root=trace_root,
        sensitive_trace_stop_file=stop_file,
        warnings=tuple(dict.fromkeys(warnings)),
    )


_COMPONENT_PREFIXES = (
    ("openkb.desktop_model", "model"),
    ("openkb.desktop_import", "import"),
    ("openkb.desktop_engine_page_tree", "page_tree"),
    ("openkb.desktop_engine_knowledge", "knowledge"),
    ("openkb.desktop_document_parser", "parser"),
    ("openkb.desktop_legacy_office", "parser"),
    ("openkb.desktop_pdf", "parser"),
    ("openkb.desktop_office", "parser"),
    ("openkb.desktop_parser", "parser"),
    ("openkb.desktop_presentation", "parser"),
    ("openkb.desktop_spreadsheet", "parser"),
    ("openkb.desktop_text", "parser"),
    ("openkb.desktop_page", "page_tree"),
    ("openkb.desktop_retrieval", "retrieval"),
    ("openkb.desktop_grounded", "retrieval"),
    ("openkb.desktop_knowledge", "knowledge"),
    ("openkb.desktop_missing_sources", "projection"),
    ("openkb.desktop_okf_projection", "projection"),
    ("openkb.desktop_projection", "projection"),
    ("openkb.desktop_material", "projection"),
    ("openkb.desktop_catalog", "storage"),
    ("openkb.desktop_workspace", "storage"),
    ("openkb.desktop_engine", "bridge"),
)


def component_for_logger(logger_name: str) -> str:
    """Map today's module logger names into the stable public component set."""
    for prefix, component in _COMPONENT_PREFIXES:
        if logger_name.startswith(prefix):
            return component
    return "runtime"
