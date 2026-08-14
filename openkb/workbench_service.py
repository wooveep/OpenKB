"""Public application-service seam for the Desktop Workbench.

The service deliberately starts with one read-only command.  It gives the
future Desktop Bridge a stable command/result/event shape while reusing the
existing knowledge-base inventory and status behaviour.  New desktop commands
belong here instead of teaching Tauri, HTTP routes, or CLI commands about each
other.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.locks import kb_read_lock

_TYPE_DISPLAY_MAP = {
    "long_pdf": "pageindex",
    "pageindex_cloud": "pageindex",
}

_SHORT_DOC_TYPES = {
    "pdf",
    "docx",
    "md",
    "markdown",
    "html",
    "htm",
    "txt",
    "csv",
    "pptx",
    "xlsx",
    "xls",
}


class DesktopWorkbenchError(RuntimeError):
    """A stable, typed error exposed at the Desktop Workbench boundary."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KnowledgeBaseNotFoundError(DesktopWorkbenchError):
    """Raised when a command addresses a directory that is not a knowledge base."""

    def __init__(self, kb_dir: Path) -> None:
        super().__init__("knowledge_base_not_found", f"Not a knowledge base: {kb_dir}")


class KnowledgeBaseStateError(DesktopWorkbenchError):
    """Raised when persisted knowledge-base state has an invalid boundary shape."""

    def __init__(self, message: str) -> None:
        super().__init__("knowledge_base_state_invalid", message)


class KnowledgeBaseReadError(DesktopWorkbenchError):
    """Raised when a Desktop Workbench read cannot access persisted state."""

    def __init__(self, kb_dir: Path, cause: OSError) -> None:
        super().__init__(
            "knowledge_base_read_failed", f"Could not read knowledge base {kb_dir}: {cause}"
        )


@dataclass(frozen=True)
class InspectKnowledgeBaseCommand:
    """Read a Desktop Knowledge Base through the public service seam."""

    kb_dir: Path


@dataclass(frozen=True)
class KnowledgeBaseDocument:
    """One validated document entry visible in a knowledge-base inventory."""

    file_hash: str
    name: str
    raw_type: str
    display_type: str
    pages: int | None

    def as_dict(self) -> dict[str, object]:
        """Preserve the legacy web/CLI inventory representation."""
        return {
            "hash": self.file_hash,
            "name": self.name,
            "type": self.raw_type,
            "display_type": self.display_type,
            "pages": self.pages,
        }


@dataclass(frozen=True)
class KnowledgeBaseInventory:
    """Typed document inventory returned by the public service seam."""

    documents: tuple[KnowledgeBaseDocument, ...]
    summaries: tuple[str, ...]
    concepts: tuple[str, ...]
    entities: tuple[str, ...]
    reports: tuple[str, ...]

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def as_dict(self) -> dict[str, object]:
        """Preserve the legacy web/CLI inventory representation."""
        return {
            "documents": [document.as_dict() for document in self.documents],
            "document_count": self.document_count,
            "summaries": list(self.summaries),
            "concepts": list(self.concepts),
            "entities": list(self.entities),
            "reports": list(self.reports),
        }


@dataclass(frozen=True)
class KnowledgeBaseStatus:
    """Typed compilation status returned by the public service seam."""

    source_count: int
    summary_count: int
    concept_count: int
    report_count: int
    raw_count: int
    total_indexed: int
    last_compile: str | None
    last_lint: str | None

    @property
    def directories(self) -> dict[str, int]:
        return {
            "sources": self.source_count,
            "summaries": self.summary_count,
            "concepts": self.concept_count,
            "reports": self.report_count,
        }

    def as_dict(self) -> dict[str, object]:
        """Preserve the legacy web/CLI status representation."""
        return {
            "directories": self.directories,
            "raw_count": self.raw_count,
            "total_indexed": self.total_indexed,
            "last_compile": self.last_compile,
            "last_lint": self.last_lint,
        }


@dataclass(frozen=True)
class KnowledgeBaseSnapshot:
    """The inventory and status visible to a Desktop Workbench caller."""

    kb_dir: str
    inventory: KnowledgeBaseInventory
    status: KnowledgeBaseStatus


@dataclass(frozen=True)
class KnowledgeBaseInspectedEventData:
    """Payload for the completed knowledge-base inspection event."""

    kb_dir: str
    document_count: int

    def as_dict(self) -> dict[str, object]:
        return {"kb_dir": self.kb_dir, "document_count": self.document_count}


@dataclass(frozen=True)
class DesktopWorkbenchEvent:
    """One public event returned with a completed Desktop Workbench command."""

    kind: Literal["knowledge_base.inspected"]
    data: KnowledgeBaseInspectedEventData


@dataclass(frozen=True)
class DesktopWorkbenchOutcome:
    """The result and events returned by a completed Desktop Workbench command."""

    snapshot: KnowledgeBaseSnapshot
    events: tuple[DesktopWorkbenchEvent, ...]


class DesktopWorkbenchService:
    """Public command/query boundary shared by Desktop and existing read paths."""

    def execute(self, command: InspectKnowledgeBaseCommand) -> DesktopWorkbenchOutcome:
        """Execute a typed command and return its public result and events."""
        snapshot = self.inspect_knowledge_base(command.kb_dir)
        event = DesktopWorkbenchEvent(
            kind="knowledge_base.inspected",
            data=KnowledgeBaseInspectedEventData(
                kb_dir=snapshot.kb_dir,
                document_count=snapshot.inventory.document_count,
            ),
        )
        return DesktopWorkbenchOutcome(snapshot=snapshot, events=(event,))

    def inspect_knowledge_base(self, kb_dir: Path) -> KnowledgeBaseSnapshot:
        """Return the read-only snapshot for one existing knowledge base."""
        try:
            resolved = kb_dir.expanduser().resolve()
            _require_knowledge_base(resolved)
            with kb_read_lock(resolved / ".openkb"):
                hashes = _read_hashes(resolved)
                return KnowledgeBaseSnapshot(
                    kb_dir=str(resolved),
                    inventory=_inventory_from_hashes(resolved, hashes),
                    status=_status_from_hashes(resolved, hashes),
                )
        except DesktopWorkbenchError:
            raise
        except OSError as exc:
            raise KnowledgeBaseReadError(kb_dir, exc) from exc


def read_knowledge_base_inventory(kb_dir: Path) -> dict[str, object]:
    """Return the legacy inventory shape through the Desktop Workbench seam."""
    return DesktopWorkbenchService().inspect_knowledge_base(kb_dir).inventory.as_dict()


def read_knowledge_base_status(kb_dir: Path) -> dict[str, object]:
    """Return the legacy status shape through the Desktop Workbench seam."""
    return DesktopWorkbenchService().inspect_knowledge_base(kb_dir).status.as_dict()


def _inventory_from_hashes(
    kb_dir: Path, hashes: dict[str, dict[str, object]]
) -> KnowledgeBaseInventory:
    """Build one validated inventory from a single registry snapshot."""
    documents = tuple(
        _document_from_metadata(file_hash, metadata) for file_hash, metadata in hashes.items()
    )
    wiki_dir = kb_dir / "wiki"
    return KnowledgeBaseInventory(
        documents=documents,
        summaries=tuple(_markdown_stems(wiki_dir / "summaries")),
        concepts=tuple(_markdown_stems(wiki_dir / "concepts")),
        entities=tuple(_markdown_stems(wiki_dir / "entities")),
        reports=tuple(_markdown_names(wiki_dir / "reports")),
    )


def _status_from_hashes(kb_dir: Path, hashes: dict[str, dict[str, object]]) -> KnowledgeBaseStatus:
    """Build one typed status record from the same registry snapshot."""
    wiki_dir = kb_dir / "wiki"
    source_paths = _markdown_paths(wiki_dir / "sources")
    summary_paths = _markdown_paths(wiki_dir / "summaries")
    concept_paths = _markdown_paths(wiki_dir / "concepts")
    report_paths = _markdown_paths(wiki_dir / "reports")
    raw_dir = kb_dir / "raw"
    raw_count = (
        len([path for path in raw_dir.iterdir() if path.is_file()]) if raw_dir.exists() else 0
    )
    return KnowledgeBaseStatus(
        source_count=len(source_paths),
        summary_count=len(summary_paths),
        concept_count=len(concept_paths),
        report_count=len(report_paths),
        raw_count=raw_count,
        total_indexed=len(hashes),
        last_compile=_newest_mtime_iso(summary_paths),
        last_lint=_newest_mtime_iso(report_paths),
    )


def _require_knowledge_base(kb_dir: Path) -> None:
    if not (kb_dir / ".openkb").is_dir() or not (kb_dir / "wiki").is_dir():
        raise KnowledgeBaseNotFoundError(kb_dir)


def _read_hashes(kb_dir: Path) -> dict[str, dict[str, object]]:
    hashes_file = kb_dir / ".openkb" / "hashes.json"
    if not hashes_file.exists():
        return {}
    try:
        payload = json.loads(hashes_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeBaseStateError(f"Could not read knowledge-base registry: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeBaseStateError("Knowledge-base registry must be a mapping.")
    hashes: dict[str, dict[str, object]] = {}
    for file_hash, metadata in payload.items():
        if not isinstance(file_hash, str) or not isinstance(metadata, dict):
            raise KnowledgeBaseStateError(
                "Knowledge-base registry contains an invalid document entry."
            )
        hashes[file_hash] = dict(metadata)
    return hashes


def _document_from_metadata(file_hash: str, metadata: dict[str, object]) -> KnowledgeBaseDocument:
    name = metadata.get("name", "unknown")
    raw_type = metadata.get("type", "unknown")
    if not isinstance(name, str) or not isinstance(raw_type, str):
        raise KnowledgeBaseStateError(
            "Knowledge-base registry document name and type must be strings."
        )
    return KnowledgeBaseDocument(
        file_hash=file_hash,
        name=name,
        raw_type=raw_type,
        display_type=display_document_type(raw_type),
        pages=_normalise_pages(metadata.get("pages")),
    )


def _normalise_pages(value: object) -> int | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        raise KnowledgeBaseStateError("Knowledge-base registry document pages must be an integer.")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value) or None
    raise KnowledgeBaseStateError("Knowledge-base registry document pages must be an integer.")


def display_document_type(raw_type: str) -> str:
    """Map a persisted raw document type to its existing display category."""
    if raw_type in _TYPE_DISPLAY_MAP:
        return _TYPE_DISPLAY_MAP[raw_type]
    if raw_type in _SHORT_DOC_TYPES:
        return "short"
    return raw_type


def _markdown_stems(directory: Path) -> list[str]:
    return sorted(path.stem for path in _markdown_paths(directory))


def _markdown_names(directory: Path) -> list[str]:
    return sorted(path.name for path in _markdown_paths(directory))


def _markdown_paths(directory: Path) -> list[Path]:
    return list(directory.glob("*.md")) if directory.exists() else []


def _newest_mtime_iso(paths: list[Path]) -> str | None:
    if not paths:
        return None
    newest = max(paths, key=lambda path: path.stat().st_mtime)
    local_tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=local_tz).isoformat()
