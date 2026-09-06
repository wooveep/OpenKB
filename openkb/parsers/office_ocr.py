"""Enhanced OCR recovery for image-heavy modern Office documents."""

from __future__ import annotations

from uuid import uuid4

from openkb.importing.artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    ParsedDocument,
)


def enhance_office_document(
    parsed: ParsedDocument,
    *,
    source_format: str,
) -> ParsedDocument:
    """Append OCR text for retained source images while preserving direct-parser IR."""
    images = tuple(image for image in parsed.source_images if image.content)
    if not images:
        return parsed
    try:
        from openkb.parsers.pdf import ocr_image_text

        recovered = tuple((image, ocr_image_text(image.content)) for image in images)
    except DesktopImportError as error:
        raise DesktopImportError(
            "enhanced_office_parser_unavailable",
            "Enhanced Office image parsing is unavailable.",
            suggested_action="Use the fast parser or provide a document with selectable text.",
        ) from error
    blocks = list(parsed.blocks)
    for image, text in recovered:
        if not text.strip():
            continue
        locator = {
            **image.locator,
            "source_image_id": image.image_id,
            "parser_route": "bundled_onnx_ocr",
            "recovery_route": f"{source_format}_embedded_image",
        }
        blocks.append(
            DocumentIRBlock(
                block_id=uuid4().hex,
                ordinal=len(blocks),
                kind="paragraph",
                text=text.strip(),
                heading_path=(),
                line_start=max(1, len(blocks) + 1),
                line_end=max(1, len(blocks) + 1),
                locator=locator,
            )
        )
    return ParsedDocument(tuple(blocks), parsed.source_images)
