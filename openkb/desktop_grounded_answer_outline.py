"""Citation-addressable how-to outlines and final answer claim guards."""

from __future__ import annotations

import re

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_source_sections import SOURCE_OCCURRENCE_CONTEXT_KEY

_CITATION_PATTERN = re.compile(r"\[(\d+)\]")
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_CITATION_REQUIRED_SECTION_MARKERS = (
    "validation",
    "verify",
    "test",
    "check",
    "warning",
    "safety",
    "验证",
    "测试",
    "检查",
    "警告",
    "安全",
    "注意",
)


def evidence_phase_index(evidence: tuple[DesktopEvidenceRef, ...]) -> str:
    """Collapse detailed source headings into a citation-addressable phase checklist."""
    grouped: dict[
        tuple[str, str],
        tuple[int, int, list[int], dict[str, list[int]]],
    ] = {}
    for ordinal, reference in enumerate(evidence, start=1):
        parts = tuple(part.strip() for part in reference.section.split(" / ") if part.strip())
        if not parts:
            continue
        phase = " / ".join(parts[:2])
        key = (reference.document_name, phase.casefold())
        priority = _phase_index_priority(reference)
        existing = grouped.get(key)
        detail = parts[-1] if len(parts) > 2 else ""
        if existing is None:
            details = {detail: [ordinal]} if detail else {}
            grouped[key] = (priority, ordinal, [ordinal], details)
        else:
            existing[2].append(ordinal)
            if detail:
                existing[3].setdefault(detail, []).append(ordinal)
            grouped[key] = (
                min(priority, existing[0]),
                existing[1],
                existing[2],
                existing[3],
            )
    ordered = sorted(grouped.items(), key=lambda item: (item[1][0], item[1][1]))[:12]
    return "\n".join(
        _phase_index_line(
            evidence,
            document_name=document_name,
            phase_key=phase_key,
            first_ordinal=first_ordinal,
            ordinals=ordinals,
            details=details,
        )
        for (
            document_name,
            phase_key,
        ), (
            _priority,
            first_ordinal,
            ordinals,
            details,
        ) in ordered
    )


def evidence_occurrence_index(evidence: tuple[DesktopEvidenceRef, ...]) -> str:
    """Map one canonical fragment to each distinct source position where it repeats."""
    citation_by_id = {
        reference.evidence_id: ordinal for ordinal, reference in enumerate(evidence, start=1)
    }
    lines: list[str] = []
    for ordinal, reference in enumerate(evidence, start=1):
        raw_contexts = reference.locator.get(SOURCE_OCCURRENCE_CONTEXT_KEY)
        if not isinstance(raw_contexts, list) or len(raw_contexts) < 2:
            continue
        previous_ordinals: list[int] = []
        for context in raw_contexts:
            if not isinstance(context, dict):
                continue
            previous_id = context.get("previous_evidence_id")
            if not isinstance(previous_id, str):
                continue
            previous_ordinal = citation_by_id.get(previous_id)
            if previous_ordinal is not None and previous_ordinal not in previous_ordinals:
                previous_ordinals.append(previous_ordinal)
        if len(previous_ordinals) < 2:
            continue
        lines.extend(
            f"- After step [{previous_ordinal}], repeat and cite warning/exception [{ordinal}]."
            for previous_ordinal in previous_ordinals
        )
    return "\n".join(lines)


def citation_guarded_answer(answer_text: str, *, evidence_count: int) -> str:
    """Remove uncited list claims only from validation and warning sections."""
    lines = answer_text.splitlines()
    guarded: list[str] = []
    required_by_level: dict[int, bool] = {}
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            guarded.append(line)
            continue
        heading = _HEADING_PATTERN.match(stripped)
        if heading:
            level = len(heading.group(1))
            required_by_level = {
                key: value for key, value in required_by_level.items() if key < level
            }
            inherited = any(required_by_level.values())
            title = heading.group(2).casefold()
            required_by_level[level] = inherited or any(
                marker in title for marker in _CITATION_REQUIRED_SECTION_MARKERS
            )
            guarded.append(line)
            continue
        citation_required = any(required_by_level.values())
        if (
            citation_required
            and not in_fence
            and _LIST_ITEM_PATTERN.match(line)
            and not any(
                1 <= int(match) <= evidence_count for match in _CITATION_PATTERN.findall(line)
            )
        ):
            continue
        guarded.append(line)
    return "\n".join(guarded).strip()


def complete_repeated_evidence_occurrences(
    answer_text: str,
    evidence: tuple[DesktopEvidenceRef, ...],
) -> str:
    """Restore a cited source statement at every distinct position lost to deduplication."""
    requirements = _repeated_occurrence_requirements(evidence)
    if not requirements:
        return answer_text
    citation_positions = _citation_positions(answer_text)
    insertions: dict[int, list[str]] = {}
    for anchor_ordinal, repeated_ordinal, excerpt, peer_anchors in requirements:
        anchor_positions = citation_positions.get(anchor_ordinal, ())
        repeated_positions = citation_positions.get(repeated_ordinal, ())
        if not anchor_positions:
            continue
        if any(
            _repeated_after_anchor(
                anchor_position,
                repeated_positions,
                peer_anchors=peer_anchors,
                citation_positions=citation_positions,
            )
            for anchor_position in anchor_positions
        ):
            continue
        insertion_at = _paragraph_end(answer_text, anchor_positions[-1])
        insertions.setdefault(insertion_at, []).append(
            f"{_display_evidence_excerpt(excerpt)} [{repeated_ordinal}]"
        )
    completed = answer_text
    for insertion_at in sorted(insertions, reverse=True):
        addition = "\n\n" + "\n\n".join(dict.fromkeys(insertions[insertion_at]))
        completed = completed[:insertion_at] + addition + completed[insertion_at:]
    return completed.strip()


def _repeated_occurrence_requirements(
    evidence: tuple[DesktopEvidenceRef, ...],
) -> tuple[tuple[int, int, str, tuple[int, ...]], ...]:
    citation_by_id = {
        reference.evidence_id: ordinal for ordinal, reference in enumerate(evidence, start=1)
    }
    requirements: list[tuple[int, int, str, tuple[int, ...]]] = []
    for repeated_ordinal, reference in enumerate(evidence, start=1):
        raw_contexts = reference.locator.get(SOURCE_OCCURRENCE_CONTEXT_KEY)
        if not isinstance(raw_contexts, list) or len(raw_contexts) < 2:
            continue
        anchors = tuple(
            dict.fromkeys(
                citation_by_id[previous_id]
                for context in raw_contexts
                if isinstance(context, dict)
                and isinstance((previous_id := context.get("previous_evidence_id")), str)
                and previous_id in citation_by_id
            )
        )
        if len(anchors) < 2:
            continue
        requirements.extend(
            (anchor, repeated_ordinal, reference.excerpt, anchors) for anchor in anchors
        )
    return tuple(requirements)


def _citation_positions(answer_text: str) -> dict[int, tuple[int, ...]]:
    positions: dict[int, list[int]] = {}
    for match in _CITATION_PATTERN.finditer(answer_text):
        positions.setdefault(int(match.group(1)), []).append(match.start())
    return {ordinal: tuple(values) for ordinal, values in positions.items()}


def _repeated_after_anchor(
    anchor_position: int,
    repeated_positions: tuple[int, ...],
    *,
    peer_anchors: tuple[int, ...],
    citation_positions: dict[int, tuple[int, ...]],
) -> bool:
    next_anchor = min(
        (
            position
            for ordinal in peer_anchors
            for position in citation_positions.get(ordinal, ())
            if position > anchor_position
        ),
        default=None,
    )
    return any(
        position > anchor_position and (next_anchor is None or position < next_anchor)
        for position in repeated_positions
    )


def _paragraph_end(answer_text: str, position: int) -> int:
    boundary = re.search(r"\n\s*\n", answer_text[position:])
    return len(answer_text) if boundary is None else position + boundary.start()


def _display_evidence_excerpt(excerpt: str) -> str:
    value = excerpt.strip()
    for marker in ("\\", "**", "__"):
        while value.startswith(marker) and value.endswith(marker) and len(value) > len(marker) * 2:
            value = value[len(marker) : -len(marker)].strip()
    return value


def _phase_index_line(
    evidence: tuple[DesktopEvidenceRef, ...],
    *,
    document_name: str,
    phase_key: str,
    first_ordinal: int,
    ordinals: list[int],
    details: dict[str, list[int]],
) -> str:
    line = (
        f"- {document_name} — {_phase_display(evidence, first_ordinal, phase_key)}: "
        + ", ".join(f"[{ordinal}]" for ordinal in ordinals)
    )
    if not details:
        return line
    rendered_details = "; ".join(
        f"{label}: " + ", ".join(f"[{ordinal}]" for ordinal in detail_ordinals)
        for label, detail_ordinals in tuple(details.items())[:6]
    )
    return f"{line}\n  - Source steps: {rendered_details}"


def _phase_display(
    evidence: tuple[DesktopEvidenceRef, ...], first_ordinal: int, phase_key: str
) -> str:
    reference = evidence[first_ordinal - 1]
    parts = tuple(part.strip() for part in reference.section.split(" / ") if part.strip())
    display = " / ".join(parts[:2])
    return display if display.casefold() == phase_key else phase_key


def _phase_index_priority(reference: DesktopEvidenceRef) -> int:
    channels = frozenset(reference.channels)
    if "knowledge_navigation_source_window" in channels:
        return 0
    if channels & {"wiki", "document_page_tree", "structure_lexical"}:
        return 1
    return 2
