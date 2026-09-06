"""Bounded, aspect-aware Evidence allocation for adaptive navigation."""

from __future__ import annotations

from dataclasses import replace

from openkb.answers.types import DesktopEvidenceRef
from openkb.documents.source_sections import SOURCE_OCCURRENCE_CONTEXT_KEY
from openkb.retrieval.navigation.adaptive import NAVIGATION_MAX_SOURCE_TOKENS
from openkb.retrieval.trace import DesktopFacetCoverageTrace

# Keep the reference-count guard above the size of several ordinary, explicitly
# targeted source sections. The source-token envelope remains the primary bound.
NAVIGATION_MAX_EVIDENCE_REFS = 64
NAVIGATION_PRIOR_EVIDENCE_RESERVE = 35
NAVIGATION_PRIOR_EVIDENCE_MINIMUM = 4
NAVIGATION_MAX_PRIORITY_EVIDENCE_PER_ROUND = NAVIGATION_MAX_EVIDENCE_REFS


def allocate_evidence(
    current: tuple[DesktopEvidenceRef, ...],
    supplement: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    *,
    facet_evidence_ids: dict[str, tuple[str, ...]] | None = None,
    priority_evidence_ids: tuple[str, ...] = (),
    max_evidence_refs: int = NAVIGATION_MAX_EVIDENCE_REFS,
    max_source_tokens: int = NAVIGATION_MAX_SOURCE_TOKENS,
) -> tuple[DesktopEvidenceRef, ...]:
    if max_evidence_refs <= 0 or max_source_tokens <= 0:
        return ()
    by_id: dict[str, DesktopEvidenceRef] = {}
    for reference in (*current, *supplement):
        existing = by_id.get(reference.evidence_id)
        if existing is None:
            by_id[reference.evidence_id] = reference
        else:
            by_id[reference.evidence_id] = _merge_reference(existing, reference)
    ordered_ids = _unique(
        (
            *_primary_coverage_evidence_ids(coverage),
            *(evidence_id for evidence_id in priority_evidence_ids if evidence_id in by_id),
            *_preserved_current_evidence_ids(current)[:NAVIGATION_PRIOR_EVIDENCE_MINIMUM],
            *_facet_reserved_evidence_ids(facet_evidence_ids or {}, coverage, by_id),
            *_preserved_current_evidence_ids(current),
            *section_diverse_evidence_ids(supplement),
            *_remaining_coverage_evidence_ids(coverage),
            *section_diverse_evidence_ids(tuple(item for item in current if not _weak_seed(item))),
            *(item.evidence_id for item in current if not _weak_seed(item)),
            *(item.evidence_id for item in supplement),
            *section_diverse_evidence_ids(tuple(item for item in current if _weak_seed(item))),
            *(item.evidence_id for item in current if _weak_seed(item)),
        )
    )
    selected: list[DesktopEvidenceRef] = []
    used_tokens = 0
    for evidence_id in ordered_ids:
        selected_reference = by_id.get(evidence_id)
        if selected_reference is None:
            continue
        tokens = max(1, (len(selected_reference.excerpt) + 3) // 4)
        if used_tokens + tokens > max_source_tokens:
            continue
        selected.append(selected_reference)
        used_tokens += tokens
        if len(selected) == max_evidence_refs:
            break
    return tuple(selected)


def new_action_evidence_ids(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    prior_ids: frozenset[str],
) -> tuple[str, ...]:
    """Keep every new targeted source block before diverse fallback evidence."""
    new_references = tuple(
        reference for reference in references if reference.evidence_id not in prior_ids
    )
    targeted_source_ids = targeted_source_evidence_ids(
        new_references,
        prior_ids=frozenset(),
    )
    return _unique(
        (
            *targeted_source_ids,
            *section_diverse_evidence_ids(new_references),
        )
    )


def targeted_source_evidence_ids(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    prior_ids: frozenset[str],
) -> tuple[str, ...]:
    """Return newly read Original Evidence in its source-window priority order."""
    return tuple(
        reference.evidence_id
        for reference in references
        if reference.evidence_id not in prior_ids
        and "knowledge_navigation_source_window" in reference.channels
    )


def targeted_source_sequence_evidence_ids(
    references: tuple[DesktopEvidenceRef, ...],
    *,
    prior_ids: frozenset[str],
) -> tuple[str, ...]:
    """Promote the whole logical section when a direct read adds any block from it."""
    new_targeted_ids = frozenset(targeted_source_evidence_ids(references, prior_ids=prior_ids))
    target_sections = {
        (reference.document_id, " ".join(reference.section.split()).casefold())
        for reference in references
        if reference.evidence_id in new_targeted_ids
    }
    return _unique(
        reference.evidence_id
        for reference in references
        if "knowledge_navigation_source_window" in reference.channels
        and (
            reference.document_id,
            " ".join(reference.section.split()).casefold(),
        )
        in target_sections
    )


def _preserved_current_evidence_ids(
    current: tuple[DesktopEvidenceRef, ...],
) -> tuple[str, ...]:
    """Keep bounded recovery from erasing a previously selected source outline."""
    return tuple(item.evidence_id for item in current if not _weak_seed(item))[
        :NAVIGATION_PRIOR_EVIDENCE_RESERVE
    ]


def section_diverse_evidence_ids(
    references: tuple[DesktopEvidenceRef, ...],
) -> tuple[str, ...]:
    selected: list[str] = []
    seen_sections: set[tuple[str, str]] = set()
    for reference in references:
        section_key = (
            reference.document_id,
            " ".join(reference.section.split()).casefold(),
        )
        if section_key in seen_sections:
            continue
        seen_sections.add(section_key)
        selected.append(reference.evidence_id)
    return tuple(selected)


def _weak_seed(reference: DesktopEvidenceRef) -> bool:
    """Classify seed confidence from retrieval provenance, never corpus vocabulary."""
    source_grounded_channels = {
        "document_page_tree",
        "knowledge_navigation_source_window",
        "structure_lexical",
        "wiki",
    }
    return not bool(source_grounded_channels & set(reference.channels))


def _primary_coverage_evidence_ids(
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> tuple[str, ...]:
    """Reserve one already-validated EvidenceRef for every supported facet."""
    return _unique(item.evidence_ids[0] for item in coverage if item.evidence_ids)


def _remaining_coverage_evidence_ids(
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> tuple[str, ...]:
    """Round-robin additional bindings so one facet cannot occupy every slot."""
    selected: list[str] = []
    for ordinal in range(max((len(item.evidence_ids) for item in coverage), default=0)):
        selected.extend(
            item.evidence_ids[ordinal] for item in coverage if ordinal < len(item.evidence_ids)
        )
    return _unique(selected)


def _facet_reserved_evidence_ids(
    evidence_ids_by_facet: dict[str, tuple[str, ...]],
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    evidence_by_id: dict[str, DesktopEvidenceRef],
) -> tuple[str, ...]:
    """Keep explicit source sequences whole before round-robin fallback evidence."""
    open_facets = tuple(
        item.facet_id
        for item in coverage
        if item.state in {"missing", "partial"} and item.facet_id in evidence_ids_by_facet
    )
    ranked = {
        facet_id: tuple(
            evidence_id
            for evidence_id in evidence_ids_by_facet[facet_id]
            if evidence_id in evidence_by_id
        )
        for facet_id in open_facets
    }
    targeted_source_ids = _unique(
        evidence_id
        for facet_id in open_facets
        for evidence_id in ranked[facet_id]
        if "knowledge_navigation_source_window" in evidence_by_id[evidence_id].channels
    )
    targeted_source_id_set = frozenset(targeted_source_ids)
    selected = list(targeted_source_ids)
    depth = 0
    while any(depth < len(ranked[facet_id]) for facet_id in open_facets):
        for facet_id in open_facets:
            if (
                depth < len(ranked[facet_id])
                and ranked[facet_id][depth] not in targeted_source_id_set
            ):
                selected.append(ranked[facet_id][depth])
        depth += 1
    return _unique(selected)


def _merge_reference(first: DesktopEvidenceRef, second: DesktopEvidenceRef) -> DesktopEvidenceRef:
    preferred = second if len(second.excerpt) > len(first.excerpt) else first
    locator = dict(preferred.locator)
    occurrence_contexts = max(
        (
            value
            for reference in (first, second)
            if isinstance((value := reference.locator.get(SOURCE_OCCURRENCE_CONTEXT_KEY)), list)
        ),
        key=len,
        default=None,
    )
    if occurrence_contexts:
        locator[SOURCE_OCCURRENCE_CONTEXT_KEY] = occurrence_contexts
    return replace(
        preferred,
        locator=locator,
        channels=_unique((*first.channels, *second.channels)),
    )


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))
