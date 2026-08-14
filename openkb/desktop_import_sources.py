"""Deterministic source discovery before a Desktop import begins."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_DESKTOP_IMPORT_SUFFIXES = (".txt", ".md", ".markdown", ".docx")


@dataclass(frozen=True)
class DesktopImportSource:
    """One discovered local source and its current parser eligibility."""

    path: str
    name: str
    status: str
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "status": self.status,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class DesktopImportSourceInspection:
    """A pre-import projection split into actionable and rejected sources."""

    supported: tuple[DesktopImportSource, ...]
    unsupported: tuple[DesktopImportSource, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": [source.as_dict() for source in self.supported],
            "unsupported": [source.as_dict() for source in self.unsupported],
            "supported_extensions": list(SUPPORTED_DESKTOP_IMPORT_SUFFIXES),
        }


def inspect_import_sources(source_paths: Iterable[Path]) -> DesktopImportSourceInspection:
    """Expand files and directories into a stable current-parser source list."""
    direct_sources = _unique_sorted(_resolved(source) for source in source_paths)
    discovered: list[Path] = []
    unsupported: list[DesktopImportSource] = []
    for source in direct_sources:
        if source.is_file():
            discovered.append(source)
        elif source.is_dir():
            children = _directory_files(source)
            if not children:
                unsupported.append(_unsupported(source, "import_directory_empty"))
            discovered.extend(children)
        else:
            unsupported.append(_unsupported(source, "import_source_not_found"))

    supported: list[DesktopImportSource] = []
    for source in _unique_sorted(discovered):
        if source.suffix.lower() in SUPPORTED_DESKTOP_IMPORT_SUFFIXES:
            supported.append(_supported(source))
        else:
            unsupported.append(_unsupported(source, "unsupported_import_format"))
    return DesktopImportSourceInspection(tuple(supported), tuple(unsupported))


def _directory_files(directory: Path) -> tuple[Path, ...]:
    try:
        return _unique_sorted(_resolved(path) for path in directory.rglob("*") if path.is_file())
    except OSError:
        return ()


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _unique_sorted(paths: Iterable[Path]) -> tuple[Path, ...]:
    unique = {str(path): path for path in paths}
    return tuple(sorted(unique.values(), key=lambda path: str(path).casefold()))


def _supported(source: Path) -> DesktopImportSource:
    return DesktopImportSource(str(source), source.name, "supported")


def _unsupported(source: Path, error_code: str) -> DesktopImportSource:
    return DesktopImportSource(str(source), source.name, "unsupported", error_code)
