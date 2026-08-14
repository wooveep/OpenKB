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
from typing import Any, Literal

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


@dataclass(frozen=True)
class InspectKnowledgeBaseCommand:
    """Read a Desktop Knowledge Base through the public service seam."""

    kb_dir: Path


@dataclass(frozen=True)
class KnowledgeBaseSnapshot:
    """The inventory and status visible to a Desktop Workbench caller."""

    kb_dir: str
    inventory: dict[str, Any]
    status: dict[str, Any]


@dataclass(frozen=True)
class DesktopWorkbenchEvent:
    """One public event returned with a completed Desktop Workbench command."""

    kind: Literal["knowledge_base.inspected"]
    data: dict[str, Any]


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
            data={
                "kb_dir": snapshot.kb_dir,
                "document_count": snapshot.inventory["document_count"],
            },
        )
        return DesktopWorkbenchOutcome(snapshot=snapshot, events=(event,))

    def inspect_knowledge_base(self, kb_dir: Path) -> KnowledgeBaseSnapshot:
        """Return the read-only snapshot for one existing knowledge base."""
        resolved = kb_dir.expanduser().resolve()
        _require_knowledge_base(resolved)
        return KnowledgeBaseSnapshot(
            kb_dir=str(resolved),
            inventory=read_knowledge_base_inventory(resolved),
            status=read_knowledge_base_status(resolved),
        )


def read_knowledge_base_inventory(kb_dir: Path) -> dict[str, Any]:
    """Return the legacy inventory shape through the Desktop Workbench seam."""
    hashes = _read_hashes(kb_dir)
    documents: list[dict[str, Any]] = []
    for file_hash, meta in hashes.items():
        raw_type = str(meta.get("type", "unknown"))
        pages = meta.get("pages")
        documents.append(
            {
                "hash": file_hash,
                "name": meta.get("name", "unknown"),
                "type": raw_type,
                "display_type": display_document_type(raw_type),
                "pages": pages if pages not in ("", 0) else None,
            }
        )

    wiki_dir = kb_dir / "wiki"
    return {
        "documents": documents,
        "document_count": len(documents),
        "summaries": _markdown_stems(wiki_dir / "summaries"),
        "concepts": _markdown_stems(wiki_dir / "concepts"),
        "entities": _markdown_stems(wiki_dir / "entities"),
        "reports": _markdown_names(wiki_dir / "reports"),
    }


def read_knowledge_base_status(kb_dir: Path) -> dict[str, Any]:
    """Return the legacy status shape through the Desktop Workbench seam."""
    wiki_dir = kb_dir / "wiki"
    directories = {
        name: len(list((wiki_dir / name).glob("*.md"))) if (wiki_dir / name).exists() else 0
        for name in ("sources", "summaries", "concepts", "reports")
    }
    raw_dir = kb_dir / "raw"
    raw_count = (
        len([path for path in raw_dir.iterdir() if path.is_file()]) if raw_dir.exists() else 0
    )
    summaries = (
        list((wiki_dir / "summaries").glob("*.md")) if (wiki_dir / "summaries").exists() else []
    )
    reports = list((wiki_dir / "reports").glob("*.md")) if (wiki_dir / "reports").exists() else []
    return {
        "directories": directories,
        "raw_count": raw_count,
        "total_indexed": len(_read_hashes(kb_dir)),
        "last_compile": _newest_mtime_iso(summaries),
        "last_lint": _newest_mtime_iso(reports),
    }


def _require_knowledge_base(kb_dir: Path) -> None:
    if not (kb_dir / ".openkb").is_dir() or not (kb_dir / "wiki").is_dir():
        raise KnowledgeBaseNotFoundError(kb_dir)


def _read_hashes(kb_dir: Path) -> dict[str, dict[str, Any]]:
    hashes_file = kb_dir / ".openkb" / "hashes.json"
    if not hashes_file.exists():
        return {}
    try:
        payload = json.loads(hashes_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeBaseStateError(f"Could not read knowledge-base registry: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeBaseStateError("Knowledge-base registry must be a mapping.")
    hashes: dict[str, dict[str, Any]] = {}
    for file_hash, metadata in payload.items():
        if not isinstance(file_hash, str) or not isinstance(metadata, dict):
            raise KnowledgeBaseStateError(
                "Knowledge-base registry contains an invalid document entry."
            )
        hashes[file_hash] = metadata
    return hashes


def display_document_type(raw_type: str) -> str:
    """Map a persisted raw document type to its existing display category."""
    if raw_type in _TYPE_DISPLAY_MAP:
        return _TYPE_DISPLAY_MAP[raw_type]
    if raw_type in _SHORT_DOC_TYPES:
        return "short"
    return raw_type


def _markdown_stems(directory: Path) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.md")) if directory.exists() else []


def _markdown_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.glob("*.md")) if directory.exists() else []


def _newest_mtime_iso(paths: list[Path]) -> str | None:
    if not paths:
        return None
    newest = max(paths, key=lambda path: path.stat().st_mtime)
    local_tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=local_tz).isoformat()
