"""Lazy parser capability inspection and reusable heavy-runtime warm-up."""

from __future__ import annotations

import importlib
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.importing.artifacts import DesktopImportError

ParserFamily = Literal["native_office", "legacy_office", "pdf_ocr", "deep_document"]
ParserMode = Literal["auto", "fast", "enhanced"]
ParserResourceState = Literal["resources_ready", "unavailable"]
ParserRuntimeState = Literal["not_loaded", "initializing", "ready", "unavailable"]


@dataclass(frozen=True)
class ParserReadiness:
    """Sanitized resource and runtime state for one parser family."""

    family: ParserFamily
    formats: tuple[str, ...]
    resource_state: ParserResourceState
    runtime_state: ParserRuntimeState
    diagnostic: str

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "formats": list(self.formats),
            "resource_state": self.resource_state,
            "runtime_state": self.runtime_state,
            "diagnostic": self.diagnostic,
        }


@dataclass
class _RuntimeEntry:
    state: ParserRuntimeState = "not_loaded"
    ready: threading.Event | None = None
    error: DesktopImportError | None = None


class ParserWarmup:
    """A joinable handle shared by parsing and raw-asset preparation."""

    def __init__(self, family: ParserFamily, entry: _RuntimeEntry) -> None:
        self.family = family
        self._entry = entry

    def wait(self) -> None:
        ready = self._entry.ready
        if ready is not None:
            ready.wait()
        if self._entry.error is not None:
            raise self._entry.error


_FORMATS: dict[ParserFamily, tuple[str, ...]] = {
    "native_office": ("docx", "pptx", "xlsx"),
    "legacy_office": ("doc", "ppt"),
    "pdf_ocr": ("pdf",),
    "deep_document": (),
}
_entries: dict[ParserFamily, _RuntimeEntry] = {
    family: _RuntimeEntry("ready" if family == "native_office" else "not_loaded")
    for family in _FORMATS
}
_entries_lock = threading.Lock()


def inspect_parser_readiness() -> dict[ParserFamily, ParserReadiness]:
    """Inspect package resources without importing or constructing heavy engines."""
    availability = {
        "native_office": True,
        "legacy_office": _legacy_resources_available(),
        "pdf_ocr": _ocr_resources_available(),
        "deep_document": _deep_document_resources_available(),
    }
    with _entries_lock:
        return {
            family: _readiness(family, availability[family], _entries[family].state)
            for family in _FORMATS
        }


def parser_runtime_snapshot() -> dict[ParserFamily, ParserReadiness]:
    """Return current runtime state while re-checking only lightweight resources."""
    return inspect_parser_readiness()


def begin_parser_warmup(source: Path) -> ParserWarmup | None:
    """Start only the heavy parser needed by this source, at most once per Engine."""
    family = _family_for_source(source)
    if family is None or family == "native_office":
        return None
    readiness = inspect_parser_readiness()[family]
    with _entries_lock:
        entry = _entries[family]
        if readiness.resource_state == "unavailable":
            entry.state = "unavailable"
            return None
        if entry.state in {"initializing", "ready"}:
            return ParserWarmup(family, entry)
        entry.state = "initializing"
        entry.error = None
        entry.ready = threading.Event()
        thread = threading.Thread(
            target=_initialize_entry,
            args=(family, source, entry),
            daemon=True,
            name=f"openkb-parser-{family}",
        )
        thread.start()
        return ParserWarmup(family, entry)


def require_parser_mode(value: str) -> ParserMode:
    if value not in {"auto", "fast", "enhanced"}:
        raise ValueError("parser_mode must be auto, fast, or enhanced")
    return value  # type: ignore[return-value]


def reset_parser_runtime_for_tests() -> None:
    """Reset process state for deterministic unit tests."""
    with _entries_lock:
        for family in _entries:
            _entries[family] = _RuntimeEntry("ready" if family == "native_office" else "not_loaded")


def _initialize_entry(family: ParserFamily, source: Path, entry: _RuntimeEntry) -> None:
    try:
        _initialize_family(family, source)
    except DesktopImportError as error:
        entry.error = error
        entry.state = "unavailable"
    except Exception as error:
        entry.error = _runtime_unavailable(family)
        entry.state = "unavailable"
        entry.error.__cause__ = error
    else:
        entry.state = "ready"
    finally:
        if entry.ready is not None:
            entry.ready.set()


def _initialize_family(family: ParserFamily, source: Path | None) -> None:
    if family == "legacy_office":
        from openkb.parsers.legacy_office import prewarm_legacy_office_runtime

        prewarm_legacy_office_runtime(source)
        return
    if family == "pdf_ocr":
        from openkb.parsers.pdf import warm_pdf_ocr_runtime

        warm_pdf_ocr_runtime()
        return
    if family == "deep_document":
        importlib.import_module("deepdoc")


def _family_for_source(source: Path) -> ParserFamily | None:
    suffix = source.suffix.casefold()
    if suffix in {".doc", ".ppt"}:
        return "legacy_office"
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return "native_office"
    return None


def _readiness(
    family: ParserFamily,
    available: bool,
    runtime_state: ParserRuntimeState,
) -> ParserReadiness:
    if not available:
        return ParserReadiness(
            family,
            _FORMATS[family],
            "unavailable",
            "unavailable",
            "parser_resources_missing",
        )
    state = runtime_state if runtime_state != "unavailable" else "not_loaded"
    return ParserReadiness(
        family,
        _FORMATS[family],
        "resources_ready",
        state,
        "parser_ready" if state == "ready" else "parser_resources_available",
    )


def _legacy_resources_available() -> bool:
    try:
        from openkb.parsers.legacy_office import legacy_office_resources_available

        return legacy_office_resources_available()
    except (ImportError, OSError, RuntimeError):
        return False


def _ocr_resources_available() -> bool:
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def _deep_document_resources_available() -> bool:
    return importlib.util.find_spec("deepdoc") is not None


def _runtime_unavailable(family: ParserFamily) -> DesktopImportError:
    return DesktopImportError(
        "parser_runtime_unavailable",
        f"The {family.replace('_', ' ')} parser is unavailable.",
        suggested_action="Use the fast parser or convert the source to a modern supported format.",
    )
