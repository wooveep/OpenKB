"""Phase-diverse original-source windows for Knowledge Navigation."""

from __future__ import annotations

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_navigation_ranking import unrequested_lifecycle_penalty

_PROCEDURE_CHECKPOINT_LIMITS = {"scope": 2, "safety": 2, "validation": 1}
_SHALLOW_PHASES_BEFORE_CHECKPOINTS = 8
_PROCEDURE_CONTINUATION_MARKERS = (
    "add ",
    "after ",
    "complete",
    "finish",
    "then ",
    "加到",
    "增加",
    "完成",
    "将",
    "然后",
)
_PROCEDURE_CHECKPOINT_MARKERS = (
    (
        "scope",
        (
            "both",
            "两台",
            "都",
            "both nodes",
            "both hosts",
            "两台主机都",
            "两台主机均",
            "两台服务器都",
            "两台服务器均",
            "第1台、第2",
            "都选择",
            "均需",
            "all nodes",
            "all hosts",
            "each node",
            "each host",
            "every node",
            "every host",
            "每个节点",
            "每台主机",
            "所有节点",
        ),
    ),
    (
        "safety",
        (
            "caution",
            "do not",
            "important",
            "must not",
            "warning",
            "不可",
            "不能",
            "切记",
            "勿",
            "禁止",
            "重要",
        ),
    ),
    (
        "validation",
        (
            "test ",
            "测试",
            "validate",
            "验证",
            "verify",
            "检查",
            "confirm",
            "确认",
            "successful",
            "正常",
        ),
    ),
)


def phase_diverse_source_window(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    terms: tuple[str, ...] = (),
) -> tuple[DesktopEvidenceRef, ...]:
    """Keep shallow phases and procedural checkpoints ahead of deep detail."""
    representatives: dict[tuple[str, str], tuple[int, DesktopEvidenceRef]] = {}
    for ordinal, reference in enumerate(references):
        key = (reference.document_id, " ".join(reference.section.split()).casefold())
        existing = representatives.get(key)
        if existing is None or _prefer_phase_representative(reference, existing[1]):
            representatives[key] = (existing[0] if existing is not None else ordinal, reference)
    outline = [item for _ordinal, item in representatives.values()]
    first_ordinals = {item.evidence_id: ordinal for ordinal, item in representatives.values()}
    outline.sort(
        key=lambda item: (
            item.section.count(" / "),
            unrequested_lifecycle_penalty(item.section, terms),
            first_ordinals[item.evidence_id],
        )
    )
    minimum_depth = _minimum_section_depth_by_document(outline)
    shallow_outline = tuple(
        item for item in outline if item.section.count(" / ") <= minimum_depth[item.document_id] + 1
    )
    shallow_ids = {item.evidence_id for item in shallow_outline}
    has_deep_outline = any(item.evidence_id not in shallow_ids for item in outline)
    shallow_head = (
        shallow_outline[:_SHALLOW_PHASES_BEFORE_CHECKPOINTS]
        if has_deep_outline
        else shallow_outline
    )
    shallow_tail = shallow_outline[len(shallow_head) :]
    checkpoint_outline = (
        _procedure_checkpoint_outline(references, excluded=shallow_ids, terms=terms)
        if has_deep_outline
        else ()
    )
    reserved_ids = shallow_ids | {item.evidence_id for item in checkpoint_outline}
    deep_outline = tuple(item for item in outline if item.evidence_id not in reserved_ids)
    outline_ids = {reference.evidence_id for reference in outline}
    remaining = tuple(
        item
        for item in references
        if item.evidence_id not in outline_ids and item.evidence_id not in reserved_ids
    )
    return (
        *shallow_head,
        *checkpoint_outline,
        *shallow_tail,
        *deep_outline,
        *(item for item in remaining if not _is_outline_only(item)),
        *(item for item in remaining if _is_outline_only(item)),
    )


def _minimum_section_depth_by_document(
    references: list[DesktopEvidenceRef],
) -> dict[str, int]:
    depths: dict[str, int] = {}
    for reference in references:
        depth = reference.section.count(" / ")
        depths[reference.document_id] = min(depths.get(reference.document_id, depth), depth)
    return depths


def _procedure_checkpoint_outline(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    excluded: set[str],
    terms: tuple[str, ...],
) -> tuple[DesktopEvidenceRef, ...]:
    selected: list[DesktopEvidenceRef] = []
    selected_ids = set(excluded)
    safety_checkpoints: list[DesktopEvidenceRef] = []
    first_ordinals = {
        reference.evidence_id: ordinal for ordinal, reference in enumerate(references)
    }
    for _kind, markers in _PROCEDURE_CHECKPOINT_MARKERS:
        candidates = tuple(
            reference
            for reference in references
            if reference.evidence_id not in selected_ids
            and not _is_outline_only(reference)
            and _checkpoint_score(reference, markers)
        )
        ranked = sorted(
            candidates,
            key=lambda item: (
                unrequested_lifecycle_penalty(item.section, terms),
                -_checkpoint_score(item, markers),
                first_ordinals[item.evidence_id],
            ),
        )
        selected_sections: set[tuple[str, str]] = set()
        for reference in ranked:
            section_key = (reference.document_id, " ".join(reference.section.split()).casefold())
            if section_key in selected_sections:
                continue
            selected.append(reference)
            selected_ids.add(reference.evidence_id)
            selected_sections.add(section_key)
            if _kind == "safety":
                safety_checkpoints.append(reference)
            if len(selected_sections) == _PROCEDURE_CHECKPOINT_LIMITS[_kind]:
                break
    continuation = _best_safety_continuation(
        references,
        safety_checkpoints=tuple(safety_checkpoints),
        excluded=selected_ids,
        first_ordinals=first_ordinals,
    )
    if continuation is not None:
        selected.append(continuation)
    return tuple(selected)


def _checkpoint_score(reference: DesktopEvidenceRef, markers: tuple[str, ...]) -> int:
    searchable = f"{reference.section} {reference.excerpt}".casefold()
    return sum(
        len(markers) - ordinal for ordinal, marker in enumerate(markers) if marker in searchable
    )


def _best_safety_continuation(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    safety_checkpoints: tuple[DesktopEvidenceRef, ...],
    excluded: set[str],
    first_ordinals: dict[str, int],
) -> DesktopEvidenceRef | None:
    checkpoint_sections = {
        (item.document_id, " ".join(item.section.split()).casefold()) for item in safety_checkpoints
    }
    ranked = sorted(
        (
            (_continuation_score(reference), first_ordinals[reference.evidence_id], reference)
            for reference in references
            if reference.evidence_id not in excluded
            and not _is_outline_only(reference)
            and (
                reference.document_id,
                " ".join(reference.section.split()).casefold(),
            )
            in checkpoint_sections
        ),
        key=lambda item: (-item[0], item[1]),
    )
    if not ranked or ranked[0][0] < 2:
        return None
    return ranked[0][2]


def _continuation_score(reference: DesktopEvidenceRef) -> int:
    searchable = f"{reference.section} {reference.excerpt}".casefold()
    return sum(marker in searchable for marker in _PROCEDURE_CONTINUATION_MARKERS)


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
    if _is_outline_only(candidate):
        return False
    if _is_outline_only(existing):
        return True
    existing_characters = sum(character.isalnum() for character in existing.excerpt)
    candidate_characters = sum(character.isalnum() for character in candidate.excerpt)
    return existing_characters < 20 and candidate_characters > existing_characters


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
