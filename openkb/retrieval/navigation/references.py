"""Bounded follow-up actions for explicit section references in Original Evidence."""

from __future__ import annotations

import re

from openkb.answers.types import DesktopEvidenceRef
from openkb.retrieval.navigation.validation import NavigationAction, unsafe_navigation_term
from openkb.retrieval.trace import DesktopFacetCoverageTrace

_REFERENCE_CONTEXT = re.compile(
    r"(?:详见|参见|参考|见|see(?:\s+also)?|refer\s+to)[^。；;\n]{0,180}",
    re.IGNORECASE,
)
_QUOTED_TITLE = re.compile(r"[“‘\"']([^”’\"']{2,80})[”’\"']")


def unresolved_reference_actions(
    evidence: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    *,
    visited_action_ids: frozenset[str],
    maximum: int,
) -> tuple[NavigationAction, ...]:
    """Search named referenced sections that are not represented by retrieved Evidence."""
    if maximum <= 0:
        return ()
    by_id = {item.evidence_id: item for item in evidence}
    actions: list[NavigationAction] = []
    seen_titles: set[str] = set()
    for coverage_item in coverage:
        if coverage_item.state not in {"missing", "partial"}:
            continue
        for evidence_id in coverage_item.evidence_ids:
            reference = by_id.get(evidence_id)
            if reference is None:
                continue
            for title in named_reference_titles(reference.excerpt):
                normalized_title = title.casefold()
                if (
                    normalized_title in seen_titles
                    or unsafe_navigation_term(title)
                    or _reference_is_resolved(
                        title,
                        source_evidence_id=evidence_id,
                        evidence=evidence,
                    )
                ):
                    continue
                action = NavigationAction(
                    "search_routes",
                    coverage_item.facet_id,
                    terms=(title,),
                )
                if action.identity in visited_action_ids:
                    continue
                seen_titles.add(normalized_title)
                actions.append(action)
                if len(actions) == maximum:
                    return tuple(actions)
    return tuple(actions)


def unique_actions(actions: tuple[NavigationAction, ...]) -> tuple[NavigationAction, ...]:
    """Preserve action order while removing duplicate validated identities."""
    selected: list[NavigationAction] = []
    seen: set[str] = set()
    for action in actions:
        if action.identity not in seen:
            seen.add(action.identity)
            selected.append(action)
    return tuple(selected)


def named_reference_titles(text: str) -> tuple[str, ...]:
    """Extract quoted section titles only from bounded cross-reference clauses."""
    titles: list[str] = []
    seen: set[str] = set()
    for context in _REFERENCE_CONTEXT.findall(text):
        for match in _QUOTED_TITLE.finditer(context):
            title = " ".join(match.group(1).split())
            normalized = title.casefold()
            if normalized not in seen:
                seen.add(normalized)
                titles.append(title)
    return tuple(titles)


def _reference_is_resolved(
    title: str,
    *,
    source_evidence_id: str,
    evidence: tuple[DesktopEvidenceRef, ...],
) -> bool:
    normalized_title = title.casefold()
    return any(
        item.evidence_id != source_evidence_id
        and normalized_title in " ".join(item.section.split()).casefold()
        for item in evidence
    )
