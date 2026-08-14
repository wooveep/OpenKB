"""Structure-preserving Markdown and DOCX adapters for Desktop import."""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from openkb.desktop_import_artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    SourceImage,
    decode_text,
    source_format_for_path,
)

_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_FENCE_PATTERN = re.compile(r"^\s*(```+|~~~+)")
_LIST_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMAGE_PATTERN = re.compile(r"<img\b(?P<attributes>[^>]*)>", flags=re.IGNORECASE)
_HTML_IMAGE_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<name>src|alt)\s*=\s*"
    r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s\"'=<>`]+))",
    flags=re.IGNORECASE,
)
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"w": _W_NS, "r": _R_NS, "a": _A_NS, "rel": _REL_NS}


@dataclass(frozen=True)
class ParsedDocument:
    """Document IR and extracted original images before the publish transaction."""

    blocks: tuple[DocumentIRBlock, ...]
    source_images: tuple[SourceImage, ...]


def parse_structured_document(source: Path, raw_bytes: bytes) -> ParsedDocument:
    """Parse a Markdown or DOCX Raw Asset directly into structured authority data."""
    source_format = source_format_for_path(source)
    if source_format == "markdown":
        return _parse_markdown(source, decode_text(raw_bytes, source))
    if source_format == "docx":
        return _parse_docx(source, raw_bytes)
    raise DesktopImportError(
        "unsupported_import_format", f"No structured parser is registered for {source.name}."
    )


def analysis_text(blocks: tuple[DocumentIRBlock, ...]) -> str:
    """Build LLM input from the IR instead of from a rendered reader view."""
    text = "\n\n".join(block.text for block in blocks if block.text.strip())
    if not text.strip():
        raise DesktopImportError(
            "empty_text_document", "Document did not contain usable text blocks."
        )
    return text


def materialize_reader_markdown(blocks: tuple[DocumentIRBlock, ...]) -> str:
    """Render a read-only Markdown view from authority IR, never the reverse."""
    rendered: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            level = _reader_heading_level(block)
            rendered.append(f"{'#' * level} {block.text}")
        elif block.kind == "figure":
            rendered.append(f"[Image: {block.text}]")
        else:
            rendered.append(block.text)
    return "\n\n".join(part for part in rendered if part).strip()


def _parse_markdown(source: Path, text: str) -> ParsedDocument:
    lines = text.split("\n")
    blocks: list[DocumentIRBlock] = []
    images: list[SourceImage] = []
    heading_path: list[str] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def add_block(
        kind: str, content: str, start: int, end: int, *, heading_level: int | None = None
    ) -> None:
        if not content.strip():
            return
        locator: dict[str, object] = {"line_start": start, "line_end": end}
        if heading_level is not None:
            locator["heading_level"] = heading_level
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind=kind,
                text=content,
                heading_path=tuple(heading_path),
                line_start=start,
                line_end=end,
                locator=locator,
            )
        )
        if kind in {"paragraph", "list", "table"}:
            _append_markdown_images(source, content, start, end, heading_path, blocks, images)

    def flush_paragraph(end_line: int) -> None:
        nonlocal paragraph_lines
        if paragraph_lines:
            add_block("paragraph", "\n".join(paragraph_lines), paragraph_start, end_line)
            paragraph_lines = []

    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        line_number = line_index + 1
        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            flush_paragraph(line_number - 1)
            level = len(heading_match.group(1))
            title = heading_match.group(2)
            heading_path[level - 1 :] = [title]
            add_block("heading", title, line_number, line_number, heading_level=level)
            line_index += 1
            continue

        fence_match = _FENCE_PATTERN.match(line)
        if fence_match:
            flush_paragraph(line_number - 1)
            fence = fence_match.group(1)[0]
            fence_length = len(fence_match.group(1))
            code_lines = [line]
            line_index += 1
            while line_index < len(lines):
                code_line = lines[line_index]
                code_lines.append(code_line)
                if code_line.lstrip().startswith(fence * fence_length):
                    line_index += 1
                    break
                line_index += 1
            add_block("code", "\n".join(code_lines), line_number, line_index)
            continue

        if _is_html_table_start(line):
            flush_paragraph(line_number - 1)
            table_lines = [line]
            line_index += 1
            while line_index < len(lines):
                table_line = lines[line_index]
                table_lines.append(table_line)
                line_index += 1
                if "</table>" in table_line.casefold():
                    break
            add_block("table", "\n".join(table_lines), line_number, line_index)
            continue

        if _is_markdown_table(lines, line_index):
            flush_paragraph(line_number - 1)
            table_lines = [line, lines[line_index + 1]]
            line_index += 2
            while (
                line_index < len(lines) and "|" in lines[line_index] and lines[line_index].strip()
            ):
                table_lines.append(lines[line_index])
                line_index += 1
            add_block("table", "\n".join(table_lines), line_number, line_index)
            continue

        if _LIST_PATTERN.match(line):
            flush_paragraph(line_number - 1)
            list_lines = [line]
            line_index += 1
            while line_index < len(lines):
                list_line = lines[line_index]
                if not list_line.strip():
                    break
                if not _LIST_PATTERN.match(list_line) and not list_line.startswith((" ", "\t")):
                    break
                list_lines.append(list_line)
                line_index += 1
            add_block("list", "\n".join(list_lines), line_number, line_index)
            continue

        if not line.strip():
            flush_paragraph(line_number - 1)
            line_index += 1
            continue

        if not paragraph_lines:
            paragraph_start = line_number
        paragraph_lines.append(line)
        line_index += 1

    flush_paragraph(len(lines))
    if not blocks:
        raise DesktopImportError(
            "empty_text_document",
            f"Markdown source did not contain usable text blocks: {source.name}",
        )
    return ParsedDocument(tuple(blocks), tuple(images))


def _append_markdown_images(
    source: Path,
    text: str,
    line_start: int,
    line_end: int,
    heading_path: list[str],
    blocks: list[DocumentIRBlock],
    images: list[SourceImage],
) -> None:
    for alt_text, target in _markdown_image_references(text):
        image_path = _local_markdown_image_path(source, target)
        if image_path is None:
            continue
        try:
            content = image_path.read_bytes()
        except OSError:
            continue
        image = _source_image(
            content=content,
            filename=image_path.name,
            alt_text=alt_text or None,
            ordinal=len(images),
            locator={"line_start": line_start, "line_end": line_end},
        )
        images.append(image)
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind="figure",
                text=image.alt_text or image.filename,
                heading_path=tuple(heading_path),
                line_start=line_start,
                line_end=line_end,
                locator={
                    "line_start": line_start,
                    "line_end": line_end,
                    "source_image_id": image.image_id,
                },
            )
        )


def _markdown_image_references(text: str) -> tuple[tuple[str, str], ...]:
    references = list(_IMAGE_PATTERN.findall(text))
    for image_match in _HTML_IMAGE_PATTERN.finditer(text):
        attributes: dict[str, str] = {}
        attribute_text = image_match.group("attributes")
        for attribute_match in _HTML_IMAGE_ATTRIBUTE_PATTERN.finditer(attribute_text):
            value = (
                attribute_match.group("double")
                or attribute_match.group("single")
                or attribute_match.group("bare")
                or ""
            )
            attributes[attribute_match.group("name").casefold()] = value
        target = attributes.get("src")
        if target:
            references.append((attributes.get("alt", ""), target))
    return tuple(references)


def _local_markdown_image_path(source: Path, target: str) -> Path | None:
    candidate = target.strip()
    if ' "' in candidate:
        candidate = candidate.split(' "', maxsplit=1)[0]
    candidate = candidate.strip("<>")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or candidate.startswith("data:"):
        return None
    path_text = unquote(candidate.split("#", maxsplit=1)[0])
    if not path_text:
        return None
    path = (source.parent / path_text).resolve()
    return path if path.is_file() else None


def _is_html_table_start(line: str) -> bool:
    return line.lstrip().casefold().startswith("<table")


def _is_markdown_table(lines: list[str], line_index: int) -> bool:
    return (
        line_index + 1 < len(lines)
        and "|" in lines[line_index]
        and bool(_TABLE_SEPARATOR_PATTERN.match(lines[line_index + 1]))
    )


def _parse_docx(source: Path, raw_bytes: bytes) -> ParsedDocument:
    try:
        archive = zipfile.ZipFile(BytesIO(raw_bytes))
        document_xml = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise DesktopImportError(
            "invalid_docx_document", f"DOCX source cannot be read: {source.name}"
        ) from error
    try:
        document_root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as error:
        raise DesktopImportError(
            "invalid_docx_document", f"DOCX source has invalid document XML: {source.name}"
        ) from error

    body = document_root.find("w:body", _NS)
    if body is None:
        raise DesktopImportError(
            "invalid_docx_document", f"DOCX source has no document body: {source.name}"
        )
    with archive:
        relationships = _docx_relationships(archive)
        style_names = _docx_style_names(archive)
        blocks: list[DocumentIRBlock] = []
        images: list[SourceImage] = []
        heading_path: list[str] = []
        paragraph_index = 0
        table_index = 0
        body_order = 0
        for item in list(body):
            if item.tag == _w("p"):
                body_order += 1
                paragraph_index += 1
                _append_docx_paragraph(
                    item,
                    paragraph_index=paragraph_index,
                    body_order=body_order,
                    style_names=style_names,
                    relationships=relationships,
                    archive=archive,
                    heading_path=heading_path,
                    blocks=blocks,
                    images=images,
                )
            elif item.tag == _w("tbl"):
                body_order += 1
                table_index += 1
                _append_docx_table(
                    item,
                    table_index=table_index,
                    body_order=body_order,
                    relationships=relationships,
                    archive=archive,
                    heading_path=heading_path,
                    blocks=blocks,
                    images=images,
                )
    if not blocks:
        raise DesktopImportError(
            "empty_text_document", f"DOCX source did not contain usable text blocks: {source.name}"
        )
    return ParsedDocument(tuple(blocks), tuple(images))


def _append_docx_paragraph(
    paragraph: ElementTree.Element,
    *,
    paragraph_index: int,
    body_order: int,
    style_names: dict[str, str],
    relationships: dict[str, str],
    archive: zipfile.ZipFile,
    heading_path: list[str],
    blocks: list[DocumentIRBlock],
    images: list[SourceImage],
) -> None:
    text = _paragraph_text(paragraph).strip()
    locator: dict[str, object] = {"paragraph": paragraph_index, "body_order": body_order}
    style_name = _paragraph_style_name(paragraph, style_names)
    heading_level = _heading_level(style_name)
    if text:
        kind = "paragraph"
        if heading_level is not None:
            kind = "heading"
            heading_path[heading_level - 1 :] = [text]
            locator["heading_level"] = heading_level
        elif _paragraph_is_list(paragraph):
            kind = "list"
        elif "code" in style_name.casefold():
            kind = "code"
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind=kind,
                text=text,
                heading_path=tuple(heading_path),
                line_start=body_order,
                line_end=body_order,
                locator=locator,
            )
        )
    _append_docx_images(
        paragraph,
        locator=locator,
        heading_path=heading_path,
        relationships=relationships,
        archive=archive,
        blocks=blocks,
        images=images,
    )


def _append_docx_table(
    table: ElementTree.Element,
    *,
    table_index: int,
    body_order: int,
    relationships: dict[str, str],
    archive: zipfile.ZipFile,
    heading_path: list[str],
    blocks: list[DocumentIRBlock],
    images: list[SourceImage],
) -> None:
    rows: list[list[str]] = []
    for row in table.findall("w:tr", _NS):
        cells: list[str] = []
        for cell in row.findall("w:tc", _NS):
            paragraphs = [_paragraph_text(value).strip() for value in cell.findall("w:p", _NS)]
            cells.append("<br>".join(value for value in paragraphs if value).replace("|", "\\|"))
        if cells:
            rows.append(cells)
    locator: dict[str, object] = {"table": table_index, "body_order": body_order}
    if rows:
        widest = max(len(row) for row in rows)
        normalized = [row + [""] * (widest - len(row)) for row in rows]
        table_lines = ["| " + " | ".join(normalized[0]) + " |"]
        table_lines.append("| " + " | ".join("---" for _ in range(widest)) + " |")
        table_lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind="table",
                text="\n".join(table_lines),
                heading_path=tuple(heading_path),
                line_start=body_order,
                line_end=body_order,
                locator=locator,
            )
        )
    _append_docx_images(
        table,
        locator=locator,
        heading_path=heading_path,
        relationships=relationships,
        archive=archive,
        blocks=blocks,
        images=images,
    )


def _append_docx_images(
    element: ElementTree.Element,
    *,
    locator: dict[str, object],
    heading_path: list[str],
    relationships: dict[str, str],
    archive: zipfile.ZipFile,
    blocks: list[DocumentIRBlock],
    images: list[SourceImage],
) -> None:
    for blip in element.findall(".//a:blip", _NS):
        relationship_id = blip.attrib.get(_r("embed"))
        if relationship_id is None or relationship_id not in relationships:
            continue
        archive_path = relationships[relationship_id]
        try:
            content = archive.read(archive_path)
        except KeyError:
            continue
        image = _source_image(
            content=content,
            filename=Path(archive_path).name,
            alt_text=None,
            ordinal=len(images),
            locator={**locator, "relationship_id": relationship_id},
        )
        images.append(image)
        body_order = cast(int, locator["body_order"])
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind="figure",
                text=image.filename,
                heading_path=tuple(heading_path),
                line_start=body_order,
                line_end=body_order,
                locator={
                    **locator,
                    "relationship_id": relationship_id,
                    "source_image_id": image.image_id,
                },
            )
        )


def _source_image(
    *,
    content: bytes,
    filename: str,
    alt_text: str | None,
    ordinal: int,
    locator: dict[str, object],
) -> SourceImage:
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


def _docx_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
    except (KeyError, ElementTree.ParseError):
        return {}
    relationships: dict[str, str] = {}
    for relationship in root.findall("rel:Relationship", _NS):
        if relationship.attrib.get("TargetMode") == "External":
            continue
        relationship_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        if not relationship_id or not target:
            continue
        archive_path = posixpath.normpath(posixpath.join("word", target))
        if archive_path.startswith("../"):
            continue
        relationships[relationship_id] = archive_path
    return relationships


def _docx_style_names(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ElementTree.ParseError):
        return {}
    styles: dict[str, str] = {}
    for style in root.findall("w:style", _NS):
        if style.attrib.get(_w("type")) != "paragraph":
            continue
        style_id = style.attrib.get(_w("styleId"))
        name = style.find("w:name", _NS)
        if style_id and name is not None:
            styles[style_id] = name.attrib.get(_w("val"), style_id)
    return styles


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for item in paragraph.iter():
        if item.tag == _w("t"):
            parts.append(item.text or "")
        elif item.tag == _w("tab"):
            parts.append("\t")
        elif item.tag in {_w("br"), _w("cr")}:
            parts.append("\n")
    return "".join(parts)


def _paragraph_style_name(paragraph: ElementTree.Element, style_names: dict[str, str]) -> str:
    style = paragraph.find("w:pPr/w:pStyle", _NS)
    if style is None:
        return ""
    style_id = style.attrib.get(_w("val"), "")
    return style_names.get(style_id, style_id)


def _heading_level(style_name: str) -> int | None:
    match = re.search(r"heading\s*([1-6])", style_name, flags=re.IGNORECASE)
    return int(match.group(1)) if match is not None else None


def _reader_heading_level(block: DocumentIRBlock) -> int:
    """Prefer the source heading level retained in the authority IR locator."""
    heading_level = (block.locator or {}).get("heading_level")
    if isinstance(heading_level, int) and not isinstance(heading_level, bool):
        return max(1, min(6, heading_level))
    return max(1, min(6, len(block.heading_path)))


def _paragraph_is_list(paragraph: ElementTree.Element) -> bool:
    return paragraph.find("w:pPr/w:numPr", _NS) is not None


def _w(local_name: str) -> str:
    return f"{{{_W_NS}}}{local_name}"


def _r(local_name: str) -> str:
    return f"{{{_R_NS}}}{local_name}"
