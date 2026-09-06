"""Deterministic usability checks at the DocumentIR authority boundary."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock

_MINIMUM_TEXT_CHARACTERS = 4
_REPLACEMENT_CHARACTER = "\ufffd"
_SUGGESTED_ACTION = (
    "Retry with enhanced document parsing, or convert the source to a modern supported format."
)
DOCUMENT_IR_FAILURE_CODES = frozenset(
    {
        "document_ir_empty",
        "document_ir_garbled",
        "document_ir_insufficient",
        "document_ir_invalid",
        "document_ir_unlocated",
    }
)


@dataclass(frozen=True)
class DocumentIRUsability:
    """Content-free metrics used to decide whether downstream analysis is safe."""

    usable: bool
    code: str | None
    text_characters: int
    readable_characters: int
    located_blocks: int
    total_blocks: int
    structured_blocks: int

    @property
    def needs_enhanced_parsing(self) -> bool:
        """Return whether a richer parser may reasonably recover this result."""
        return self.code in {
            "document_ir_empty",
            "document_ir_garbled",
            "document_ir_insufficient",
            "document_ir_unlocated",
        }


def assess_document_ir(
    blocks: tuple[DocumentIRBlock, ...],
    *,
    minimum_text_characters: int = _MINIMUM_TEXT_CHARACTERS,
) -> DocumentIRUsability:
    """Assess quantity, readability, location, and structural integrity without I/O."""
    total = len(blocks)
    text = "".join(block.text for block in blocks if block.kind != "figure")
    visible = [character for character in text if not character.isspace()]
    readable = [
        character
        for character in visible
        if character != _REPLACEMENT_CHARACTER
        and unicodedata.category(character) not in {"Cc", "Cs", "Co", "Cn"}
    ]
    located = sum(_has_locator(block) for block in blocks)
    structured = sum(
        block.kind in {"heading", "paragraph", "list", "table", "code"} for block in blocks
    )
    code: str | None = None
    if not blocks or not visible:
        code = "document_ir_empty"
    elif not _structure_is_valid(blocks):
        code = "document_ir_invalid"
    elif len(readable) / len(visible) < 0.8:
        code = "document_ir_garbled"
    elif located != total:
        code = "document_ir_unlocated"
    elif len(readable) < minimum_text_characters:
        code = "document_ir_insufficient"
    return DocumentIRUsability(
        usable=code is None,
        code=code,
        text_characters=len(visible),
        readable_characters=len(readable),
        located_blocks=located,
        total_blocks=total,
        structured_blocks=structured,
    )


def require_usable_document_ir(
    blocks: tuple[DocumentIRBlock, ...],
) -> DocumentIRUsability:
    """Stop before evidence or model work when DocumentIR is not trustworthy."""
    report = assess_document_ir(blocks)
    if report.usable:
        return report
    messages = {
        "document_ir_empty": "Document parsing produced no usable text.",
        "document_ir_garbled": "Document parsing produced mostly unreadable text.",
        "document_ir_unlocated": "Document parsing produced text without source locations.",
        "document_ir_invalid": "Document parsing produced an invalid block structure.",
        "document_ir_insufficient": "Document parsing produced too little usable text.",
    }
    code = report.code or "document_ir_invalid"
    raise DesktopImportError(
        code,
        messages[code],
        suggested_action=_SUGGESTED_ACTION,
    )


def _has_locator(block: DocumentIRBlock) -> bool:
    if block.line_start > 0 and block.line_end >= block.line_start:
        return True
    if not isinstance(block.locator, dict):
        return False
    return any(
        key in block.locator
        for key in ("page", "page_index", "slide", "sheet", "paragraph", "cell", "bbox")
    )


def _structure_is_valid(blocks: tuple[DocumentIRBlock, ...]) -> bool:
    block_ids: set[str] = set()
    for expected_ordinal, block in enumerate(blocks):
        if (
            not block.block_id
            or block.block_id in block_ids
            or block.ordinal != expected_ordinal
            or not block.kind
            or block.line_start < 0
            or block.line_end < block.line_start
            or any(not isinstance(item, str) for item in block.heading_path)
        ):
            return False
        block_ids.add(block.block_id)
    return True
