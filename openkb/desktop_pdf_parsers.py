"""PDF parsing routes for the Desktop Document IR boundary.

The fast route reads native PDF text and geometry with PyMuPDF.  Scanned,
garbled, and low-text-density documents switch to the bundled ONNX OCR wheel;
neither route is a Model Call and neither participates in LLM retry accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

import pymupdf

from openkb.desktop_import_artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    ParsedDocument,
    SourceImage,
    source_image_from_content,
)

_FAST_ROUTE = "pymupdf_fast"
_ENHANCED_ROUTE = "bundled_onnx_ocr"
_LOW_TEXT_DENSITY = 0.00015
_OCR_RENDER_SCALE = 2

# Desktop deliberately uses its own fixed parsing routes instead of a first-use
# optional layout package, so do not print PyMuPDF's installation suggestion.
pymupdf.no_recommend_layout()


def warm_pdf_ocr_runtime() -> None:
    """Construct and cache the bundled OCR engine without parsing a document."""
    _ocr_engine()


def ocr_image_text(content: bytes) -> str:
    """Extract ordered text from one retained image with the shared cached OCR engine."""
    try:
        result, _ = _ocr_engine()(content)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise DesktopImportError(
            "enhanced_image_parse_failed",
            "Enhanced image parsing failed.",
        ) from error
    if not result:
        return ""
    values: list[tuple[float, float, str]] = []
    for item in result:
        try:
            box, text, _confidence = item
            x = min(float(point[0]) for point in box)
            y = min(float(point[1]) for point in box)
        except (TypeError, ValueError, IndexError):
            continue
        normalized = " ".join(str(text).split())
        if normalized:
            values.append((y, x, normalized))
    return "\n".join(value[2] for value in sorted(values))


@dataclass(frozen=True)
class _PageTextSnapshot:
    page_index: int
    text: str
    density: float
    is_garbled: bool


@dataclass(frozen=True)
class _DetectedTable:
    bbox: list[float]
    rows: tuple[tuple[str, ...], ...]
    strategy: str = "pymupdf_geometry"


@dataclass(frozen=True)
class _OcrLine:
    text: str
    bbox: list[float]
    confidence: float


@dataclass(frozen=True)
class _OcrTable:
    table: _DetectedTable
    line_indexes: tuple[int, ...]


def parse_pdf_document(
    source: Path,
    raw_bytes: bytes,
    *,
    parser_mode: str = "auto",
) -> ParsedDocument:
    """Parse a PDF Raw Asset into page-aware Document IR and source images."""
    document = _open_pdf(source, raw_bytes)
    try:
        if document.needs_pass:
            raise DesktopImportError(
                "encrypted_pdf_document", f"PDF source is password protected: {source.name}"
            )
        snapshots = tuple(
            _page_text_snapshot(page, page_index)
            for page_index, page in enumerate(document, start=1)
        )
        if parser_mode not in {"auto", "fast", "enhanced"}:
            raise DesktopImportError("parser_mode_invalid", "Parser mode is invalid.")
        parser_route = (
            _FAST_ROUTE
            if parser_mode == "fast"
            else _ENHANCED_ROUTE
            if parser_mode == "enhanced"
            else _parser_route(snapshots)
        )
        ocr_engine = _ocr_engine() if parser_route == _ENHANCED_ROUTE else None
        blocks: list[DocumentIRBlock] = []
        images: list[SourceImage] = []
        for page_index, page in enumerate(document, start=1):
            heading_path = (f"Page {page_index}",)
            _append_page_heading(page_index, heading_path, parser_route, blocks)
            tables = _detected_tables(page)
            if parser_route == _FAST_ROUTE:
                _append_native_text(page, page_index, heading_path, tables, blocks)
            else:
                ocr_lines = _ocr_lines(page, page_index, ocr_engine)
                ocr_tables = _ocr_tables(ocr_lines, tables)
                table_line_indexes = {
                    line_index for table in ocr_tables for line_index in table.line_indexes
                }
                _append_ocr_text(
                    page_index,
                    heading_path,
                    ocr_lines,
                    table_line_indexes,
                    blocks,
                )
                tables = tables + tuple(table.table for table in ocr_tables)
            _append_tables(page_index, heading_path, parser_route, tables, blocks)
            _append_page_images(
                document,
                page,
                page_index,
                heading_path,
                parser_route,
                blocks,
                images,
            )
    except DesktopImportError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise DesktopImportError(
            "invalid_pdf_document", f"PDF source cannot be read: {source.name}"
        ) from error
    finally:
        document.close()

    if not any(block.kind != "heading" for block in blocks):
        raise DesktopImportError(
            "empty_text_document", f"PDF source did not contain usable content: {source.name}"
        )
    return ParsedDocument(tuple(blocks), tuple(images))


def _open_pdf(source: Path, raw_bytes: bytes) -> Any:
    try:
        return pymupdf.open(stream=raw_bytes, filetype="pdf")
    except (pymupdf.FileDataError, OSError, RuntimeError, ValueError) as error:
        raise DesktopImportError(
            "invalid_pdf_document", f"PDF source cannot be read: {source.name}"
        ) from error


def _page_text_snapshot(page: Any, page_index: int) -> _PageTextSnapshot:
    text = str(page.get_text("text", sort=True)).strip()
    area = max(float(page.rect.width) * float(page.rect.height), 1.0)
    visible = [character for character in text if not character.isspace()]
    non_printable = sum(not character.isprintable() for character in visible)
    return _PageTextSnapshot(
        page_index=page_index,
        text=text,
        density=len(visible) / area,
        is_garbled="\ufffd" in text or (bool(visible) and non_printable / len(visible) > 0.05),
    )


def _parser_route(snapshots: tuple[_PageTextSnapshot, ...]) -> str:
    if not snapshots or any(snapshot.is_garbled for snapshot in snapshots):
        return _ENHANCED_ROUTE
    if not any(snapshot.text for snapshot in snapshots):
        return _ENHANCED_ROUTE
    low_density_pages = sum(snapshot.density < _LOW_TEXT_DENSITY for snapshot in snapshots)
    if low_density_pages * 2 >= len(snapshots):
        return _ENHANCED_ROUTE
    return _FAST_ROUTE


def _append_page_heading(
    page_index: int,
    heading_path: tuple[str, ...],
    parser_route: str,
    blocks: list[DocumentIRBlock],
) -> None:
    blocks.append(
        DocumentIRBlock(
            block_id=uuid4().hex,
            ordinal=len(blocks),
            kind="heading",
            text=heading_path[-1],
            heading_path=heading_path,
            line_start=page_index,
            line_end=page_index,
            locator={
                "page": page_index,
                "page_index": page_index,
                "heading_level": 1,
                "parser_route": parser_route,
            },
        )
    )


def _append_native_text(
    page: Any,
    page_index: int,
    heading_path: tuple[str, ...],
    tables: tuple[_DetectedTable, ...],
    blocks: list[DocumentIRBlock],
) -> None:
    page_dict = page.get_text("dict", sort=True)
    for block_index, source_block in enumerate(page_dict.get("blocks", ()), start=1):
        if source_block.get("type") != 0:
            continue
        bbox = _bbox(source_block.get("bbox"))
        if bbox is None or any(_bbox_overlaps(bbox, table.bbox) for table in tables):
            continue
        text = _text_from_pdf_block(source_block)
        if not text:
            continue
        blocks.append(
            DocumentIRBlock(
                block_id=uuid4().hex,
                ordinal=len(blocks),
                kind="paragraph",
                text=text,
                heading_path=heading_path,
                line_start=page_index,
                line_end=page_index,
                locator={
                    "page": page_index,
                    "page_index": page_index,
                    "bbox": bbox,
                    "block_index": block_index,
                    "parser_route": _FAST_ROUTE,
                    "layout_strategy": "pymupdf_text_blocks",
                },
            )
        )


def _text_from_pdf_block(source_block: dict[str, object]) -> str:
    lines: list[str] = []
    source_lines = source_block.get("lines", ())
    if not isinstance(source_lines, (list, tuple)):
        return ""
    for line in source_lines:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans", ())
        if not isinstance(spans, (list, tuple)):
            continue
        text = "".join(
            str(span.get("text", "")) for span in spans if isinstance(span, dict)
        ).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _detected_tables(page: Any) -> tuple[_DetectedTable, ...]:
    try:
        candidates = page.find_tables().tables
    except (AttributeError, RuntimeError, ValueError):
        return ()
    tables: list[_DetectedTable] = []
    for candidate in candidates:
        bbox = _bbox(candidate.bbox)
        rows = _table_rows(candidate.extract())
        if bbox is not None and rows:
            tables.append(_DetectedTable(bbox, rows))
    return tuple(tables)


def _table_rows(values: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(values, list):
        return ()
    rows = [tuple(_table_cell(value) for value in row) for row in values if isinstance(row, list)]
    return tuple(row for row in rows if row)


def _table_cell(value: object) -> str:
    return " ".join(str(value or "").replace("|", "\\|").splitlines()).strip()


def _append_tables(
    page_index: int,
    heading_path: tuple[str, ...],
    parser_route: str,
    tables: tuple[_DetectedTable, ...],
    blocks: list[DocumentIRBlock],
) -> None:
    for table_index, table in enumerate(tables, start=1):
        text = _markdown_table(table.rows)
        if not text:
            continue
        blocks.append(
            DocumentIRBlock(
                block_id=uuid4().hex,
                ordinal=len(blocks),
                kind="table",
                text=text,
                heading_path=heading_path,
                line_start=page_index,
                line_end=page_index,
                locator={
                    "page": page_index,
                    "page_index": page_index,
                    "bbox": table.bbox,
                    "table_index": table_index,
                    "row_count": len(table.rows),
                    "column_count": max(len(row) for row in table.rows),
                    "parser_route": parser_route,
                    "table_strategy": table.strategy,
                },
            )
        )


def _markdown_table(rows: tuple[tuple[str, ...], ...]) -> str:
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    normalized = [list(row) + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    if not any(header):
        header = [f"Column {index}" for index in range(1, width + 1)]
    rendered = [f"| {' | '.join(header)} |", f"| {' | '.join('---' for _ in header)} |"]
    rendered.extend(f"| {' | '.join(row)} |" for row in normalized[1:])
    return "\n".join(rendered)


def _ocr_lines(page: Any, page_index: int, ocr_engine: Any) -> tuple[_OcrLine, ...]:
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(_OCR_RENDER_SCALE, _OCR_RENDER_SCALE), alpha=False
    )
    try:
        result, _ = ocr_engine(pixmap.tobytes("png"))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise DesktopImportError(
            "enhanced_pdf_parse_failed", f"Enhanced PDF parsing failed: Page {page_index}"
        ) from error
    if not result:
        return ()
    return tuple(
        sorted(
            (_ocr_line(item, page, pixmap.width, pixmap.height) for item in result),
            key=lambda line: (line.bbox[1], line.bbox[0]),
        )
    )


def _append_ocr_text(
    page_index: int,
    heading_path: tuple[str, ...],
    lines: tuple[_OcrLine, ...],
    table_line_indexes: set[int],
    blocks: list[DocumentIRBlock],
) -> None:
    for line_index, line in enumerate(lines, start=1):
        if line_index - 1 in table_line_indexes:
            continue
        blocks.append(
            DocumentIRBlock(
                block_id=uuid4().hex,
                ordinal=len(blocks),
                kind="paragraph",
                text=line.text,
                heading_path=heading_path,
                line_start=page_index,
                line_end=page_index,
                locator={
                    "page": page_index,
                    "page_index": page_index,
                    "bbox": line.bbox,
                    "ocr_confidence": line.confidence,
                    "ocr_line_index": line_index,
                    "parser_route": _ENHANCED_ROUTE,
                    "layout_strategy": "onnx_ocr_line_boxes",
                },
            )
        )


def _ocr_tables(
    lines: tuple[_OcrLine, ...], native_tables: tuple[_DetectedTable, ...]
) -> tuple[_OcrTable, ...]:
    """Reconstruct regular scanned tables from OCR cell geometry, without an LLM."""
    tables: list[_OcrTable] = []
    for candidate in _ocr_table_candidates(lines):
        if any(_bbox_overlaps(candidate.table.bbox, table.bbox) for table in native_tables):
            continue
        tables.append(candidate)
    return tuple(tables)


def _ocr_table_candidates(lines: tuple[_OcrLine, ...]) -> tuple[_OcrTable, ...]:
    rows = _ocr_rows(lines)
    candidates: list[_OcrTable] = []
    current: list[tuple[int, ...]] = []
    for row in rows:
        if len(row) < 2:
            _append_ocr_table_candidate(current, lines, candidates)
            current = []
        elif not current or _ocr_rows_align(current[-1], row, lines):
            current.append(row)
        else:
            _append_ocr_table_candidate(current, lines, candidates)
            current = [row]
    _append_ocr_table_candidate(current, lines, candidates)
    return tuple(candidates)


def _ocr_rows(lines: tuple[_OcrLine, ...]) -> tuple[tuple[int, ...], ...]:
    if not lines:
        return ()
    heights = [line.bbox[3] - line.bbox[1] for line in lines]
    tolerance = max(4.0, median(heights) * 0.8)
    rows: list[list[int]] = []
    row_center: float | None = None
    for line_index, line in enumerate(lines):
        center = _vertical_center(line)
        if row_center is None or abs(center - row_center) > tolerance:
            rows.append([line_index])
            row_center = center
        else:
            rows[-1].append(line_index)
            row_center = sum(_vertical_center(lines[index]) for index in rows[-1]) / len(rows[-1])
    return tuple(tuple(sorted(row, key=lambda index: lines[index].bbox[0])) for row in rows)


def _ocr_rows_align(
    previous: tuple[int, ...], current: tuple[int, ...], lines: tuple[_OcrLine, ...]
) -> bool:
    if len(previous) != len(current):
        return False
    vertical_gap = _vertical_center(lines[current[0]]) - _vertical_center(lines[previous[0]])
    row_height = max(_row_height(previous, lines), _row_height(current, lines))
    if vertical_gap > max(24.0, row_height * 5):
        return False
    for previous_index, current_index in zip(previous, current, strict=True):
        previous_line = lines[previous_index]
        current_line = lines[current_index]
        tolerance = max(18.0, _line_width(previous_line) * 0.8, _line_width(current_line) * 0.8)
        if abs(_horizontal_center(previous_line) - _horizontal_center(current_line)) > tolerance:
            return False
    return True


def _append_ocr_table_candidate(
    rows: list[tuple[int, ...]], lines: tuple[_OcrLine, ...], candidates: list[_OcrTable]
) -> None:
    if len(rows) < 3:
        return
    line_indexes = tuple(index for row in rows for index in row)
    bbox = _combined_bbox(lines[index].bbox for index in line_indexes)
    if bbox is None:
        return
    candidates.append(
        _OcrTable(
            table=_DetectedTable(
                bbox=bbox,
                rows=tuple(tuple(lines[index].text for index in row) for row in rows),
                strategy="onnx_ocr_cell_geometry",
            ),
            line_indexes=line_indexes,
        )
    )


def _combined_bbox(boxes: Any) -> list[float] | None:
    values = list(boxes)
    if not values:
        return None
    return [
        round(min(box[0] for box in values), 3),
        round(min(box[1] for box in values), 3),
        round(max(box[2] for box in values), 3),
        round(max(box[3] for box in values), 3),
    ]


def _vertical_center(line: _OcrLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def _horizontal_center(line: _OcrLine) -> float:
    return (line.bbox[0] + line.bbox[2]) / 2


def _line_width(line: _OcrLine) -> float:
    return line.bbox[2] - line.bbox[0]


def _row_height(row: tuple[int, ...], lines: tuple[_OcrLine, ...]) -> float:
    return max(lines[index].bbox[3] - lines[index].bbox[1] for index in row)


def _ocr_line(item: object, page: Any, pixel_width: int, pixel_height: int) -> _OcrLine:
    if not isinstance(item, (list, tuple)) or len(item) < 3:
        raise DesktopImportError(
            "enhanced_pdf_parse_failed", "Enhanced PDF OCR returned invalid text."
        )
    text = str(item[1]).strip()
    points = item[0]
    pixel_bbox = _points_bbox(points)
    if not text or pixel_bbox is None:
        raise DesktopImportError(
            "enhanced_pdf_parse_failed", "Enhanced PDF OCR returned invalid text."
        )
    x_scale = float(page.rect.width) / max(pixel_width, 1)
    y_scale = float(page.rect.height) / max(pixel_height, 1)
    bbox = [
        round(pixel_bbox[0] * x_scale, 3),
        round(pixel_bbox[1] * y_scale, 3),
        round(pixel_bbox[2] * x_scale, 3),
        round(pixel_bbox[3] * y_scale, 3),
    ]
    try:
        confidence = round(float(item[2]), 4)
    except (AttributeError, TypeError, ValueError):
        confidence = 0.0
    return _OcrLine(text, bbox, confidence)


def _points_bbox(points: object) -> list[float] | None:
    if not isinstance(points, (list, tuple)):
        return None
    pairs: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        try:
            pairs.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            return None
    if len(pairs) < 2:
        return None
    return [
        round(min(pair[0] for pair in pairs), 3),
        round(min(pair[1] for pair in pairs), 3),
        round(max(pair[0] for pair in pairs), 3),
        round(max(pair[1] for pair in pairs), 3),
    ]


def _append_page_images(
    document: Any,
    page: Any,
    page_index: int,
    heading_path: tuple[str, ...],
    parser_route: str,
    blocks: list[DocumentIRBlock],
    images: list[SourceImage],
) -> None:
    try:
        image_entries = page.get_images(full=True)
    except (RuntimeError, ValueError):
        return
    for image_index, entry in enumerate(image_entries, start=1):
        if not entry:
            continue
        xref = int(entry[0])
        try:
            extracted = document.extract_image(xref)
            content = extracted.get("image")
            extension = str(extracted.get("ext") or "bin").lstrip(".")
            rects = page.get_image_rects(xref)
        except (RuntimeError, ValueError):
            continue
        if not isinstance(content, bytes) or not content:
            continue
        for placement_index, rect in enumerate(rects, start=1):
            bbox = _bbox(rect)
            if bbox is None:
                continue
            ordinal = len(images) + 1
            name = f"page-{page_index}-image-{image_index}-{placement_index}.{extension}"
            locator = {
                "page": page_index,
                "page_index": page_index,
                "bbox": bbox,
                "xref": xref,
                "image_index": image_index,
                "placement_index": placement_index,
                "parser_route": parser_route,
            }
            image = source_image_from_content(
                content=content,
                filename=name,
                alt_text=f"Page {page_index} image {image_index}",
                ordinal=ordinal,
                locator=locator,
            )
            images.append(image)
            blocks.append(
                DocumentIRBlock(
                    block_id=uuid4().hex,
                    ordinal=len(blocks),
                    kind="figure",
                    text=image.alt_text or name,
                    heading_path=heading_path,
                    line_start=page_index,
                    line_end=page_index,
                    locator={**locator, "source_image_id": image.image_id},
                )
            )


def _bbox(value: object) -> list[float] | None:
    try:
        if isinstance(value, (list, tuple)):
            values = [float(item) for item in value]
        else:
            values = [
                float(getattr(value, "x0")),
                float(getattr(value, "y0")),
                float(getattr(value, "x1")),
                float(getattr(value, "y1")),
            ]
    except (AttributeError, TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    return [round(item, 3) for item in values]


def _bbox_overlaps(left: list[float], right: list[float]) -> bool:
    horizontally_separate = left[2] <= right[0] or left[0] >= right[2]
    vertically_separate = left[3] <= right[1] or left[1] >= right[3]
    return not horizontally_separate and not vertically_separate


@lru_cache(maxsize=1)
def _ocr_engine() -> Any:
    """Load the packaged DeepDoc OCR pair only for an enhanced PDF route."""
    try:
        from openkb.desktop_deepdoc_runtime import deepdoc_ocr_engine

        deepdoc_engine = deepdoc_ocr_engine()
        if deepdoc_engine is not None:
            return deepdoc_engine
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        raise DesktopImportError(
            "enhanced_pdf_parser_unavailable",
            "The bundled ONNX PDF parser could not be initialized.",
        ) from error
