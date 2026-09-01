"""Citation-addressable how-to outlines and final answer claim guards."""

from __future__ import annotations

import re

from openkb.desktop_answer_types import DesktopEvidenceRef

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
