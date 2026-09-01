"""Bounded, aspect-aware Evidence allocation for adaptive navigation."""

from __future__ import annotations

from dataclasses import replace

from openkb.desktop_adaptive_navigation import NAVIGATION_MAX_SOURCE_TOKENS
from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_retrieval_trace import DesktopAnswerCoverageTrace

NAVIGATION_MAX_EVIDENCE_REFS = 40
NAVIGATION_PRIOR_EVIDENCE_RESERVE = 35


def allocate_evidence(
    current: tuple[DesktopEvidenceRef, ...],
    supplement: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
    *,
    aspect_evidence_ids: dict[str, tuple[str, ...]] | None = None,
    terms: tuple[str, ...] = (),
) -> tuple[DesktopEvidenceRef, ...]:
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
            *_preserved_current_evidence_ids(current, terms),
            *_aspect_reserved_evidence_ids(aspect_evidence_ids or {}, coverage, frozenset(by_id)),
            *section_diverse_evidence_ids(supplement),
            *_remaining_coverage_evidence_ids(coverage),
            *section_diverse_evidence_ids(
                tuple(item for item in current if not _weak_seed(item, terms))
            ),
            *(item.evidence_id for item in current if not _weak_seed(item, terms)),
            *(item.evidence_id for item in supplement),
            *section_diverse_evidence_ids(
                tuple(item for item in current if _weak_seed(item, terms))
            ),
            *(item.evidence_id for item in current if _weak_seed(item, terms)),
        )
    )
    selected: list[DesktopEvidenceRef] = []
    used_tokens = 0
    for evidence_id in ordered_ids:
        reference = by_id[evidence_id]
        tokens = max(1, (len(reference.excerpt) + 3) // 4)
        if used_tokens + tokens > NAVIGATION_MAX_SOURCE_TOKENS:
            continue
        selected.append(reference)
        used_tokens += tokens
        if len(selected) == NAVIGATION_MAX_EVIDENCE_REFS:
            break
    return tuple(selected)


def _preserved_current_evidence_ids(
    current: tuple[DesktopEvidenceRef, ...], terms: tuple[str, ...]
) -> tuple[str, ...]:
    """Keep bounded recovery from erasing a previously selected source outline."""
    return tuple(item.evidence_id for item in current if not _weak_seed(item, terms))[
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


def _fts_only(reference: DesktopEvidenceRef) -> bool:
    """Let an explicit routed read displace a weak lexical-only seed at the hard cap."""
    return frozenset(reference.channels) == frozenset(("fts",))


def _weak_seed(reference: DesktopEvidenceRef, terms: tuple[str, ...]) -> bool:
    """Defer lexical/catalog seeds that do not share the query's distinctive scope."""
    if _fts_only(reference):
        return True
    trusted_channels = {
        "document_page_tree",
        "knowledge_navigation_source_window",
        "structure_lexical",
        "wiki",
    }
    if trusted_channels & set(reference.channels):
        return False
    distinctive = _distinctive_scope_terms(terms)
    if not distinctive:
        return False
    search_text = " ".join(
        (reference.document_name, reference.section, reference.excerpt)
    ).casefold()
    return not any(term in search_text for term in distinctive)


def _distinctive_scope_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    generic = {
        "build",
        "configure",
        "configuration",
        "deploy",
        "deployment",
        "how",
        "install",
        "installation",
        "migrate",
        "setup",
        "steps",
        "to",
        "安装",
        "安装部署",
        "怎么",
        "怎样",
        "搭建",
        "步骤",
        "流程",
        "迁移",
        "配置",
        "部署",
        "如何",
    }
    return _unique(
        " ".join(term.split()).casefold()
        for term in terms
        if len(" ".join(term.split())) >= 2 and " ".join(term.split()).casefold() not in generic
    )


def _primary_coverage_evidence_ids(
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
) -> tuple[str, ...]:
    """Reserve one already-validated EvidenceRef for every supported aspect."""
    return _unique(item.evidence_ids[0] for item in coverage if item.evidence_ids)


def _remaining_coverage_evidence_ids(
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
) -> tuple[str, ...]:
    """Round-robin additional bindings so one aspect cannot occupy every slot."""
    selected: list[str] = []
    for ordinal in range(max((len(item.evidence_ids) for item in coverage), default=0)):
        selected.extend(
            item.evidence_ids[ordinal] for item in coverage if ordinal < len(item.evidence_ids)
        )
    return _unique(selected)


def _aspect_reserved_evidence_ids(
    evidence_ids_by_aspect: dict[str, tuple[str, ...]],
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
    available_ids: frozenset[str],
) -> tuple[str, ...]:
    """Round-robin evidence from each explicitly targeted open aspect."""
    open_aspects = tuple(
        item.aspect
        for item in coverage
        if item.status in {"missing", "partial"} and item.aspect in evidence_ids_by_aspect
    )
    ranked = {
        aspect: tuple(
            evidence_id
            for evidence_id in evidence_ids_by_aspect[aspect]
            if evidence_id in available_ids
        )
        for aspect in open_aspects
    }
    selected: list[str] = []
    depth = 0
    while any(depth < len(ranked[aspect]) for aspect in open_aspects):
        for aspect in open_aspects:
            if depth < len(ranked[aspect]):
                selected.append(ranked[aspect][depth])
        depth += 1
    return _unique(selected)


def _merge_reference(first: DesktopEvidenceRef, second: DesktopEvidenceRef) -> DesktopEvidenceRef:
    preferred = second if len(second.excerpt) > len(first.excerpt) else first
    return replace(preferred, channels=_unique((*first.channels, *second.channels)))


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))
