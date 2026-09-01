"""Bounded channel fusion policy for Desktop retrieval candidates."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_answer_types import DesktopEvidenceRef

BASELINE_EVIDENCE_PACK_LIMIT = 6
ROUTED_EVIDENCE_PACK_LIMIT = 16
BASELINE_MINIMUM_QUOTA = 4
PAGE_TREE_MINIMUM_QUOTA = 12
GRAPH_CANDIDATE_LIMIT = 2
_RRF_OFFSET = 60


@dataclass(frozen=True)
class RetrievalCandidate:
    reference: DesktopEvidenceRef
    channel: str
    rank: int
    weight: float = 1.0


def fuse_candidates(
    candidates: tuple[RetrievalCandidate, ...],
    *,
    protected: tuple[RetrievalCandidate, ...] = (),
    routed: tuple[RetrievalCandidate, ...] = (),
) -> tuple[DesktopEvidenceRef, ...]:
    """Protect deterministic recall and reserve context for successful tree routing."""
    scores: defaultdict[str, float] = defaultdict(float)
    channels: defaultdict[str, set[str]] = defaultdict(set)
    references: dict[str, DesktopEvidenceRef] = {}
    channel_first: dict[str, str] = {}
    for candidate in candidates:
        evidence_id = candidate.reference.evidence_id
        scores[evidence_id] += candidate.weight / (_RRF_OFFSET + candidate.rank)
        channels[evidence_id].add(candidate.channel)
        existing_reference = references.get(evidence_id)
        if existing_reference is None or (
            candidate.channel == "knowledge_navigation_source_window"
            and len(candidate.reference.excerpt) > len(existing_reference.excerpt)
        ):
            references[evidence_id] = candidate.reference
        channel_first.setdefault(candidate.channel, evidence_id)

    evidence_limit = ROUTED_EVIDENCE_PACK_LIMIT if routed else BASELINE_EVIDENCE_PACK_LIMIT
    selected = list(_rank_protected_candidate_ids(protected)[:BASELINE_MINIMUM_QUOTA])
    for evidence_id in _rank_candidate_ids(routed)[:PAGE_TREE_MINIMUM_QUOTA]:
        if evidence_id not in selected:
            selected.append(evidence_id)
    for channel in ("fts", "structure_lexical", "wiki"):
        if len(selected) == evidence_limit:
            break
        first_evidence_id = channel_first.get(channel)
        if first_evidence_id is not None and first_evidence_id not in selected:
            selected.append(first_evidence_id)
    ranked = sorted(scores, key=lambda evidence_id: (-scores[evidence_id], evidence_id))
    for evidence_id in ranked:
        if len(selected) == evidence_limit:
            break
        if evidence_id not in selected:
            selected.append(evidence_id)
    return tuple(
        DesktopEvidenceRef(
            **{
                **references[key].__dict__,
                "channels": tuple(sorted(channels[key])),
            }
        )
        for key in selected
    )


def _rank_candidate_ids(candidates: tuple[RetrievalCandidate, ...]) -> tuple[str, ...]:
    scores: defaultdict[str, float] = defaultdict(float)
    channel_first: dict[str, str] = {}
    for candidate in candidates:
        evidence_id = candidate.reference.evidence_id
        scores[evidence_id] += candidate.weight / (_RRF_OFFSET + candidate.rank)
        channel_first.setdefault(candidate.channel, evidence_id)
    selected: list[str] = []
    for channel in ("fts", "structure_lexical", "wiki"):
        first_evidence_id = channel_first.get(channel)
        if first_evidence_id is not None and first_evidence_id not in selected:
            selected.append(first_evidence_id)
    for evidence_id in sorted(scores, key=lambda key: (-scores[key], key)):
        if evidence_id not in selected:
            selected.append(evidence_id)
    return tuple(selected)


def _rank_protected_candidate_ids(
    candidates: tuple[RetrievalCandidate, ...],
) -> tuple[str, ...]:
    """Prefer informative deterministic hits without discarding terse fallbacks."""
    substantive = tuple(
        candidate
        for candidate in candidates
        if _has_substantive_excerpt(candidate.reference.excerpt)
    )
    selected = list(_rank_candidate_ids(substantive))
    for evidence_id in _rank_candidate_ids(candidates):
        if evidence_id not in selected:
            selected.append(evidence_id)
    return tuple(selected)


def _has_substantive_excerpt(excerpt: str) -> bool:
    return sum(1 for character in excerpt if character.isalnum()) >= 4


def with_graph_budget(
    baseline: tuple[DesktopEvidenceRef, ...], graph: tuple[RetrievalCandidate, ...]
) -> tuple[DesktopEvidenceRef, ...]:
    """Reserve baseline evidence before an optional graph can add context."""
    graph_references = {candidate.reference.evidence_id: candidate.reference for candidate in graph}

    def with_graph_channel(reference: DesktopEvidenceRef) -> DesktopEvidenceRef:
        graph_reference = graph_references.get(reference.evidence_id)
        if graph_reference is None:
            return reference
        return DesktopEvidenceRef(
            **{
                **reference.__dict__,
                "channels": tuple(sorted(set(reference.channels) | set(graph_reference.channels))),
            }
        )

    baseline_references = tuple(with_graph_channel(reference) for reference in baseline)
    baseline_by_evidence_id = {
        reference.evidence_id: reference for reference in baseline_references
    }
    selected: list[DesktopEvidenceRef] = []
    selected_ids: set[str] = set()

    def append(reference: DesktopEvidenceRef) -> None:
        if (
            reference.evidence_id not in selected_ids
            and len(selected) < BASELINE_EVIDENCE_PACK_LIMIT
        ):
            selected.append(reference)
            selected_ids.add(reference.evidence_id)

    for reference in baseline_references[:BASELINE_MINIMUM_QUOTA]:
        append(reference)
    graph_added = 0
    for candidate in graph:
        if graph_added == GRAPH_CANDIDATE_LIMIT:
            break
        reference = baseline_by_evidence_id.get(
            candidate.reference.evidence_id, candidate.reference
        )
        if reference.evidence_id in selected_ids:
            continue
        append(reference)
        graph_added += 1
    for reference in baseline_references:
        append(reference)
    return tuple(selected)
