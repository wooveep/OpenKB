"""Model-aware evidence envelopes preserve small-model safety and large-model recall."""

from __future__ import annotations

from types import SimpleNamespace

from openkb.answers.types import DesktopEvidencePack, DesktopEvidenceRef
from openkb.retrieval.navigation.budget import (
    NavigationEvidenceEnvelope,
    navigation_evidence_envelope,
)
from openkb.retrieval.navigation.evidence import allocate_evidence
from openkb.retrieval.navigation.session import _bounded_initial_pack
from openkb.retrieval.plan import deterministic_plan


class _Gateway:
    def __init__(self, context_capacity: int, *, verified: bool = True) -> None:
        self.context_capacity = context_capacity
        self.verified = verified

    def answer_capability_verified(self) -> bool:
        return self.verified

    def capability_for_operation(self, operation: str):
        assert operation == "grounded_answer"
        return SimpleNamespace(context_capacity=self.context_capacity)


def _references(count: int) -> tuple[DesktopEvidenceRef, ...]:
    return tuple(
        DesktopEvidenceRef(
            evidence_id=f"evidence-{ordinal}",
            document_id="guide",
            document_name="Guide",
            section=f"Section {ordinal // 4}",
            locator={},
            excerpt=f"Evidence detail {ordinal}.",
            channels=("knowledge_navigation_source_window",),
        )
        for ordinal in range(count)
    )


def test_large_answer_context_expands_navigation_but_keeps_a_bounded_envelope() -> None:
    assert navigation_evidence_envelope(_Gateway(1_000_000)) == NavigationEvidenceEnvelope(
        max_evidence_refs=256,
        max_source_tokens=192_000,
    )
    assert navigation_evidence_envelope(_Gateway(128_000)) == NavigationEvidenceEnvelope(
        max_evidence_refs=64,
        max_source_tokens=24_000,
    )


def test_unverified_large_answer_context_keeps_the_conservative_navigation_envelope() -> None:
    assert navigation_evidence_envelope(
        _Gateway(1_000_000, verified=False)
    ) == NavigationEvidenceEnvelope(
        max_evidence_refs=64,
        max_source_tokens=24_000,
    )


def test_expanded_envelope_preserves_more_than_the_legacy_sixty_four_references() -> None:
    references = _references(100)
    envelope = NavigationEvidenceEnvelope(256, 192_000)

    selected = allocate_evidence(
        (),
        references,
        (),
        max_evidence_refs=envelope.max_evidence_refs,
        max_source_tokens=envelope.max_source_tokens,
    )
    bounded, reduced = _bounded_initial_pack(
        DesktopEvidencePack(deterministic_plan("How is the guide configured?"), references),
        evidence_envelope=envelope,
    )

    assert len(selected) == len(references)
    assert {item.evidence_id for item in selected} == {item.evidence_id for item in references}
    assert bounded.evidence == references
    assert not reduced
