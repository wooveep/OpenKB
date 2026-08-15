"""Normalized Desktop Import artifacts shared by stage runners."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_sources import SUPPORTED_DESKTOP_IMPORT_SUFFIXES

_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_SOURCE_FORMATS = {
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".pdf": "pdf",
}
_SOURCE_MEDIA_TYPES = {
    "txt": "text/plain",
    "markdown": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}
_SOURCE_SUFFIXES = {
    "txt": (".txt",),
    "markdown": (".md", ".markdown"),
    "docx": (".docx",),
    "xls": (".xls",),
    "xlsx": (".xlsx",),
    "pptx": (".pptx",),
    "pdf": (".pdf",),
}
_TEXT_SOURCE_FORMATS = {"txt", "markdown"}


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
    locator: dict[str, object] | None = None


@dataclass(frozen=True)
class SourceImage:
    """An original document image retained separately from its complete Raw Asset."""

    image_id: str
    ordinal: int
    image_sha256: str
    byte_size: int
    media_type: str
    filename: str
    extension: str
    alt_text: str | None
    locator: dict[str, object]
    content: bytes = b""


@dataclass(frozen=True)
class ParsedDocument:
    """Document IR and extracted original images before the publish transaction."""

    blocks: tuple[DocumentIRBlock, ...]
    source_images: tuple[SourceImage, ...]


def validate_text_source(source_path: Path) -> Path:
    """Resolve and validate a source handled by the current Desktop import path."""
    source = source_path.expanduser().resolve()
    if source.suffix.lower() not in SUPPORTED_DESKTOP_IMPORT_SUFFIXES:
        raise DesktopImportError(
            "unsupported_import_format",
            "Desktop import supports TXT, Markdown, DOCX, XLS, XLSX, PPTX, and PDF files.",
        )
    if not source.is_file():
        raise DesktopImportError(
            "import_source_not_found", f"Import source was not found: {source}"
        )
    return source


def source_format_for_path(source: Path) -> str:
    """Return the stable persisted source format for a validated input path."""
    try:
        return _SOURCE_FORMATS[source.suffix.lower()]
    except KeyError as error:
        raise DesktopImportError(
            "unsupported_import_format", f"Unsupported import source: {source.name}"
        ) from error


def source_media_type(source_format: str) -> str:
    """Return the persisted MIME type for one complete Raw Asset."""
    try:
        return _SOURCE_MEDIA_TYPES[source_format]
    except KeyError as error:
        raise DesktopImportError(
            "unsupported_import_format", f"Unsupported import source format: {source_format}"
        ) from error


def source_suffixes_for_format(source_format: str) -> tuple[str, ...]:
    """Return all accepted original suffixes for an already-classified format."""
    try:
        return _SOURCE_SUFFIXES[source_format]
    except KeyError as error:
        raise DesktopImportError(
            "unsupported_import_format", f"Unsupported import source format: {source_format}"
        ) from error


def source_format_is_textual(source_format: str) -> bool:
    """Whether a raw asset must be decoded before its document-IR parser runs."""
    return source_format in _TEXT_SOURCE_FORMATS


def source_format_uses_structured_ir(source_format: str) -> bool:
    """Whether the reader and model analysis must use persisted Document IR."""
    return source_format in _SOURCE_SUFFIXES and source_format != "txt"


def source_image_from_content(
    *,
    content: bytes,
    filename: str,
    alt_text: str | None,
    ordinal: int,
    locator: dict[str, object],
) -> SourceImage:
    """Build one retained Source Image record from extracted original bytes."""
    extension = Path(filename).suffix.lower() or ".bin"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return SourceImage(
        image_id=uuid.uuid4().hex,
        ordinal=ordinal,
        image_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        media_type=media_type,
        filename=filename or f"image-{ordinal}{extension}",
        extension=extension,
        alt_text=alt_text,
        locator=locator,
        content=content,
    )


def decode_text(content: bytes, source: Path) -> str:
    """Decode a text-like raw asset into normalized non-empty UTF-8 text."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DesktopImportError(
            "invalid_text_document", f"Text source is not valid UTF-8: {source.name}"
        ) from error
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise DesktopImportError("empty_text_document", f"Text source is empty: {source.name}")
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


def document_ir_checkpoint(
    blocks: tuple[DocumentIRBlock, ...], source_images: tuple[SourceImage, ...] = ()
) -> list[dict[str, object]] | dict[str, object]:
    """Serialize completed conversion output for a later Stage Run resume."""
    block_payload = [
        {
            "block_id": block.block_id,
            "ordinal": block.ordinal,
            "kind": block.kind,
            "text": block.text,
            "heading_path": list(block.heading_path),
            "line_start": block.line_start,
            "line_end": block.line_end,
            "locator": block.locator,
        }
        for block in blocks
    ]
    if not source_images:
        # Preserve the first TXT checkpoint shape so old paused jobs remain resumable.
        return block_payload
    return {
        "blocks": block_payload,
        "source_images": [_source_image_checkpoint(image) for image in source_images],
    }


def document_ir_from_checkpoint(payload: object) -> tuple[DocumentIRBlock, ...]:
    """Rehydrate only a structurally valid persisted Document IR checkpoint."""
    block_payload = payload.get("blocks") if isinstance(payload, dict) else payload
    if not isinstance(block_payload, list):
        raise DesktopImportError("import_checkpoint_invalid", "Document IR checkpoint is invalid.")
    blocks: list[DocumentIRBlock] = []
    block_ids: set[str] = set()
    for expected_ordinal, item in enumerate(block_payload):
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
        locator = item.get("locator")
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
            or (locator is not None and not isinstance(locator, dict))
            or not _is_json_object(locator)
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
                locator=dict(locator) if locator is not None else None,
            )
        )
    if not blocks:
        raise DesktopImportError("import_checkpoint_invalid", "Document IR checkpoint is invalid.")
    return tuple(blocks)


def source_images_from_checkpoint(payload: object) -> tuple[SourceImage, ...]:
    """Rehydrate retained source-image records without repeating conversion work."""
    if not isinstance(payload, dict):
        return ()
    image_payload = payload.get("source_images")
    if image_payload is None:
        return ()
    if not isinstance(image_payload, list):
        raise DesktopImportError("import_checkpoint_invalid", "Source image checkpoint is invalid.")
    images: list[SourceImage] = []
    image_ids: set[str] = set()
    for expected_ordinal, item in enumerate(image_payload):
        if not isinstance(item, dict):
            raise DesktopImportError(
                "import_checkpoint_invalid", "Source image checkpoint is invalid."
            )
        image_id = item.get("image_id")
        ordinal = item.get("ordinal")
        image_sha256 = item.get("image_sha256")
        byte_size = item.get("byte_size")
        media_type = item.get("media_type")
        filename = item.get("filename")
        extension = item.get("extension")
        alt_text = item.get("alt_text")
        locator = item.get("locator")
        if (
            not isinstance(image_id, str)
            or not image_id
            or image_id in image_ids
            or type(ordinal) is not int
            or ordinal != expected_ordinal
            or not isinstance(image_sha256, str)
            or len(image_sha256) != 64
            or any(character not in "0123456789abcdef" for character in image_sha256)
            or type(byte_size) is not int
            or byte_size < 0
            or not isinstance(media_type, str)
            or not media_type
            or not isinstance(filename, str)
            or not filename
            or not isinstance(extension, str)
            or not extension.startswith(".")
            or (alt_text is not None and not isinstance(alt_text, str))
            or not isinstance(locator, dict)
            or not _is_json_object(locator)
        ):
            raise DesktopImportError(
                "import_checkpoint_invalid", "Source image checkpoint is invalid."
            )
        image_ids.add(image_id)
        images.append(
            SourceImage(
                image_id=image_id,
                ordinal=ordinal,
                image_sha256=image_sha256,
                byte_size=byte_size,
                media_type=media_type,
                filename=filename,
                extension=extension,
                alt_text=alt_text,
                locator=dict(locator),
            )
        )
    return tuple(images)


def _source_image_checkpoint(image: SourceImage) -> dict[str, object]:
    return {
        "image_id": image.image_id,
        "ordinal": image.ordinal,
        "image_sha256": image.image_sha256,
        "byte_size": image.byte_size,
        "media_type": image.media_type,
        "filename": image.filename,
        "extension": image.extension,
        "alt_text": image.alt_text,
        "locator": image.locator,
    }


def _is_json_object(value: object) -> bool:
    if value is None:
        return True
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return True


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
