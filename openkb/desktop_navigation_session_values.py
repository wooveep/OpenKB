"""Small immutable-value helpers shared by the bounded Navigation Session."""

from __future__ import annotations

from dataclasses import replace

from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopRetrievalModelCost,
)
from openkb.desktop_retrieval_trace import DesktopFacetCoverageTrace, DesktopRetrievalTrace


def order_evidence_by_coverage(
    evidence: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> tuple[DesktopEvidenceRef, ...]:
    """Put facet-bound Evidence first without changing the available set."""
    by_id = {item.evidence_id: item for item in evidence}
    ordered = _unique(
        (
            *(evidence_id for item in coverage for evidence_id in item.evidence_ids),
            *(item.evidence_id for item in evidence),
        )
    )
    return tuple(by_id[evidence_id] for evidence_id in ordered if evidence_id in by_id)


def with_added_cost(
    pack: DesktopEvidencePack, cost: DesktopRetrievalModelCost
) -> DesktopEvidencePack:
    return replace(pack, retrieval_model_cost=sum_cost(pack.retrieval_model_cost, cost))


def sum_cost(
    first: DesktopRetrievalModelCost, second: DesktopRetrievalModelCost
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=first.model_calls + second.model_calls,
        input_characters=first.input_characters + second.input_characters,
        output_characters=first.output_characters + second.output_characters,
    )


def logical_reads(trace: DesktopRetrievalTrace) -> int:
    return (
        trace.navigation_read_count + trace.source_window_count + trace.page_tree_supplement_count
    )


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))
