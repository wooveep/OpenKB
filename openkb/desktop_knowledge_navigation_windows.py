"""Phase-diverse original-source windows for Knowledge Navigation."""

from __future__ import annotations

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_source_sections import SOURCE_BLOCK_KIND_CONTEXT_KEY


def phase_diverse_source_window(
    references: tuple[DesktopEvidenceRef, ...],
) -> tuple[DesktopEvidenceRef, ...]:
    """Expose one block per structural section before source-ordered detail."""
    representatives: dict[tuple[str, str], tuple[int, DesktopEvidenceRef]] = {}
    for ordinal, reference in enumerate(references):
        key = (reference.document_id, " ".join(reference.section.split()).casefold())
        existing = representatives.get(key)
        if existing is None or _prefer_phase_representative(
            reference,
            existing[1],
        ):
            representatives[key] = (existing[0] if existing is not None else ordinal, reference)
    outline = [item for _ordinal, item in representatives.values()]
    first_ordinals = {item.evidence_id: ordinal for ordinal, item in representatives.values()}
    outline.sort(
        key=lambda item: (
            item.section.count(" / "),
            first_ordinals[item.evidence_id],
        )
    )
    outline_ids = {reference.evidence_id for reference in outline}
    remaining = tuple(item for item in references if item.evidence_id not in outline_ids)
    return (
        *outline,
        *(item for item in remaining if not _is_outline_only(item)),
        *(item for item in remaining if _is_outline_only(item)),
    )


def round_robin_source_windows(
    windows: tuple[tuple[DesktopEvidenceRef, ...], ...],
) -> tuple[DesktopEvidenceRef, ...]:
    selected: list[DesktopEvidenceRef] = []
    seen_evidence_ids: set[str] = set()
    for ordinal in range(max((len(window) for window in windows), default=0)):
        for window in windows:
            if ordinal >= len(window):
                continue
            reference = window[ordinal]
            if reference.evidence_id in seen_evidence_ids:
                continue
            seen_evidence_ids.add(reference.evidence_id)
            selected.append(reference)
    return tuple(selected)


def consolidate_seed_source_windows(
    windows: tuple[tuple[DesktopEvidenceRef, ...], ...],
) -> tuple[tuple[DesktopEvidenceRef, ...], ...]:
    """Keep a chapter outline instead of redundant seed reads for its child phases."""
    selected: list[tuple[DesktopEvidenceRef, ...]] = []
    for window in windows:
        if any(_source_window_covers(existing, window) for existing in selected):
            continue
        selected = [
            existing for existing in selected if not _source_window_covers(window, existing)
        ]
        selected.append(window)
    return tuple(selected)


def _is_outline_only(reference: DesktopEvidenceRef) -> bool:
    excerpt = " ".join(reference.excerpt.split())
    leaf = " ".join(reference.section.split()).rsplit(" / ", 1)[-1]
    if not excerpt or excerpt.casefold() == leaf.casefold():
        return True
    lowered = excerpt.casefold()
    return lowered.startswith("image") and lowered.rsplit(".", 1)[-1] in {
        "gif",
        "jpeg",
        "jpg",
        "png",
        "webp",
    }


def _prefer_phase_representative(
    candidate: DesktopEvidenceRef,
    existing: DesktopEvidenceRef,
) -> bool:
    return _representative_priority(candidate) < _representative_priority(existing)


def _representative_priority(reference: DesktopEvidenceRef) -> int:
    """Prefer substantive DocumentIR blocks without interpreting their vocabulary."""
    kind = reference.locator.get(SOURCE_BLOCK_KIND_CONTEXT_KEY)
    if kind == "figure":
        return 2
    if kind == "heading" or _is_outline_only(reference):
        return 1
    return 0


def _source_window_covers(
    parent: tuple[DesktopEvidenceRef, ...],
    child: tuple[DesktopEvidenceRef, ...],
) -> bool:
    normalized_parent = tuple(
        (
            item,
            item.document_id,
            " / ".join(part.strip() for part in item.section.split(" / ")),
        )
        for item in parent
    )
    parent_sections = {(document_id, section) for _item, document_id, section in normalized_parent}
    child_sections = tuple(
        (item.document_id, " / ".join(part.strip() for part in item.section.split(" / ")))
        for item in child
    )
    for document_id, section in parent_sections:
        prefix = f"{section} / "
        covers_all = child_sections and all(
            child_document_id == document_id
            and (child_section == section or child_section.startswith(prefix))
            for child_document_id, child_section in child_sections
        )
        represents_child_phases = all(
            any(
                parent_document_id == child_document_id
                and (
                    parent_section == child_section
                    or parent_section.startswith(f"{child_section} / ")
                )
                and not _is_outline_only(parent_item)
                for parent_item, parent_document_id, parent_section in normalized_parent
            )
            for child_document_id, child_section in set(child_sections)
        )
        if (
            covers_all
            and represents_child_phases
            and any(child_section != section for _document_id, child_section in child_sections)
        ):
            return True
    return False
