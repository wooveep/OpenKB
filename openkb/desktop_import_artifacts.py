"""TXT normalization helpers shared by Desktop import stage runners."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_sources import SUPPORTED_DESKTOP_IMPORT_SUFFIXES

_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")


class DesktopImportError(RuntimeError):
    """A stable domain error for Desktop document import."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocumentIRBlock:
    """A normalized structured block retained in the SQLite Document IR."""

    block_id: str
    ordinal: int
    kind: str
    text: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int


def validate_text_source(source_path: Path) -> Path:
    """Resolve and validate the first supported Desktop source format."""
    source = source_path.expanduser().resolve()
    if source.suffix.lower() not in SUPPORTED_DESKTOP_IMPORT_SUFFIXES:
        raise DesktopImportError(
            "unsupported_import_format", "The first Desktop import path supports TXT files only."
        )
    if not source.is_file():
        raise DesktopImportError("import_source_not_found", f"TXT source was not found: {source}")
    return source


def decode_text(content: bytes, source: Path) -> str:
    """Decode the raw asset into normalized non-empty UTF-8 text."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DesktopImportError(
            "invalid_text_document", f"TXT source is not valid UTF-8 text: {source.name}"
        ) from error
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise DesktopImportError("empty_text_document", f"TXT source is empty: {source.name}")
    return text


def build_document_ir(text: str) -> tuple[DocumentIRBlock, ...]:
    """Parse simple Markdown-style headings and paragraphs into ordered blocks."""
    lines = text.split("\n")
    blocks: list[DocumentIRBlock] = []
    heading_path: list[str] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def flush_paragraph(end_line: int) -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind="paragraph",
                text="\n".join(paragraph_lines),
                heading_path=tuple(heading_path),
                line_start=paragraph_start,
                line_end=end_line,
            )
        )
        paragraph_lines = []

    for line_number, line in enumerate(lines, start=1):
        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            flush_paragraph(line_number - 1)
            level = len(heading_match.group(1))
            title = heading_match.group(2)
            heading_path[level - 1 :] = [title]
            blocks.append(
                DocumentIRBlock(
                    block_id=uuid.uuid4().hex,
                    ordinal=len(blocks),
                    kind="heading",
                    text=title,
                    heading_path=tuple(heading_path),
                    line_start=line_number,
                    line_end=line_number,
                )
            )
            continue
        if not line.strip():
            flush_paragraph(line_number - 1)
            continue
        if not paragraph_lines:
            paragraph_start = line_number
        paragraph_lines.append(line)

    flush_paragraph(len(lines))
    if not blocks:
        raise DesktopImportError(
            "empty_text_document", "TXT source did not contain usable text blocks."
        )
    return tuple(blocks)


def build_evidence(
    blocks: tuple[DocumentIRBlock, ...],
) -> tuple[tuple[str, DocumentIRBlock], ...]:
    """Give each Document IR block its stable current import evidence record."""
    return tuple((uuid.uuid4().hex, block) for block in blocks)


def document_ir_checkpoint(blocks: tuple[DocumentIRBlock, ...]) -> list[dict[str, object]]:
    """Serialize completed conversion output for a later Stage Run resume."""
    return [
        {
            "block_id": block.block_id,
            "ordinal": block.ordinal,
            "kind": block.kind,
            "text": block.text,
            "heading_path": list(block.heading_path),
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        for block in blocks
    ]


def document_ir_from_checkpoint(payload: object) -> tuple[DocumentIRBlock, ...]:
    """Rehydrate only a structurally valid persisted Document IR checkpoint."""
    if not isinstance(payload, list):
        raise DesktopImportError("import_checkpoint_invalid", "Document IR checkpoint is invalid.")
    blocks: list[DocumentIRBlock] = []
    block_ids: set[str] = set()
    for expected_ordinal, item in enumerate(payload):
        if not isinstance(item, dict):
            raise DesktopImportError(
                "import_checkpoint_invalid", "Document IR checkpoint is invalid."
            )
        block_id = item.get("block_id")
        kind = item.get("kind")
        text = item.get("text")
        heading_path = item.get("heading_path")
        ordinal = item.get("ordinal")
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        if (
            not isinstance(block_id, str)
            or not block_id
            or block_id in block_ids
            or not isinstance(kind, str)
            or not kind
            or not isinstance(text, str)
            or not isinstance(heading_path, list)
            or not all(isinstance(value, str) for value in heading_path)
            or type(ordinal) is not int
            or ordinal != expected_ordinal
            or type(line_start) is not int
            or type(line_end) is not int
            or line_start < 1
            or line_end < line_start
        ):
            raise DesktopImportError(
                "import_checkpoint_invalid", "Document IR checkpoint is invalid."
            )
        block_ids.add(block_id)
        blocks.append(
            DocumentIRBlock(
                block_id=block_id,
                ordinal=ordinal,
                kind=kind,
                text=text,
                heading_path=tuple(heading_path),
                line_start=line_start,
                line_end=line_end,
            )
        )
    if not blocks:
        raise DesktopImportError("import_checkpoint_invalid", "Document IR checkpoint is invalid.")
    return tuple(blocks)


def evidence_checkpoint(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> list[dict[str, str]]:
    """Persist generated evidence IDs while blocks stay in the prior checkpoint."""
    return [
        {"evidence_id": evidence_id, "block_id": block.block_id} for evidence_id, block in evidence
    ]


def evidence_from_checkpoint(
    payload: object, blocks: tuple[DocumentIRBlock, ...]
) -> tuple[tuple[str, DocumentIRBlock], ...]:
    """Rehydrate evidence references without rerunning the evidence stage."""
    if not isinstance(payload, list):
        raise DesktopImportError("import_checkpoint_invalid", "Evidence checkpoint is invalid.")
    by_id = {block.block_id: block for block in blocks}
    evidence: list[tuple[str, DocumentIRBlock]] = []
    evidence_ids: set[str] = set()
    block_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise DesktopImportError("import_checkpoint_invalid", "Evidence checkpoint is invalid.")
        evidence_id = item.get("evidence_id")
        block_id = item.get("block_id")
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence_ids
            or not isinstance(block_id, str)
            or block_id in block_ids
            or block_id not in by_id
        ):
            raise DesktopImportError("import_checkpoint_invalid", "Evidence checkpoint is invalid.")
        evidence_ids.add(evidence_id)
        block_ids.add(block_id)
        evidence.append((evidence_id, by_id[block_id]))
    if not evidence or block_ids != set(by_id):
        raise DesktopImportError("import_checkpoint_invalid", "Evidence checkpoint is invalid.")
    return tuple(evidence)
