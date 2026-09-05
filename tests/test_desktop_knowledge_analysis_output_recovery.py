"""Bounded output-limit recovery below one logical Analysis item."""

from __future__ import annotations

import pytest

from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    DesktopKnowledgeAnalysis,
)
from openkb.desktop_knowledge_analysis_batch_store import KnowledgeAnalysisBatch
from openkb.desktop_knowledge_analysis_merge import merge_split_batch_analyses
from openkb.desktop_knowledge_analysis_output_recovery import (
    analyze_batch_with_output_limit_recovery,
)
from openkb.desktop_model_gateway import (
    DesktopModelOutputObservations,
    DesktopModelResult,
)
from openkb.desktop_structured_output import DesktopStructuredOutputInvalidError


def _batch(count: int) -> KnowledgeAnalysisBatch:
    evidence = tuple(
        (
            f"evidence-{ordinal}",
            DocumentIRBlock(
                block_id=f"block-{ordinal}",
                ordinal=ordinal,
                kind="paragraph",
                text=f"Fact {ordinal}",
                heading_path=(f"Section {ordinal}",),
                line_start=ordinal + 1,
                line_end=ordinal + 1,
            ),
        )
        for ordinal in range(count)
    )
    return KnowledgeAnalysisBatch(
        batch_id="parent",
        ordinal=0,
        section_paths=tuple(block.heading_path for _evidence_id, block in evidence),
        evidence=evidence,
        status="pending",
    )


def _output_limit_error(call_id: str) -> DesktopStructuredOutputInvalidError:
    result = DesktopModelResult(
        call_id,
        '{"schema_version":"incomplete',
        1,
        observations=DesktopModelOutputObservations(
            finish_reason="length",
            final_content_observed=True,
            final_chunk_count=1,
            final_character_count=29,
            output_limit_reached=True,
        ),
    )
    return DesktopStructuredOutputInvalidError(
        initial_result=result,
        final_result=result,
        repair_attempted=False,
    )


def _repair_output_limit_error(call_id: str) -> DesktopStructuredOutputInvalidError:
    initial = DesktopModelResult(
        f"{call_id}-initial",
        "{}",
        1,
        observations=DesktopModelOutputObservations(
            finish_reason="stop",
            final_content_observed=True,
            final_chunk_count=1,
            final_character_count=2,
        ),
    )
    repaired = DesktopModelResult(
        f"{call_id}-repair",
        '{"schema_version":"incomplete',
        1,
        observations=DesktopModelOutputObservations(
            finish_reason="length",
            final_content_observed=True,
            final_chunk_count=1,
            final_character_count=29,
            output_limit_reached=True,
        ),
    )
    return DesktopStructuredOutputInvalidError(
        initial_result=initial,
        final_result=repaired,
    )


def test_recovery_recurses_to_single_evidence_and_stays_finite() -> None:
    calls: list[int] = []

    def analyze(batch: KnowledgeAnalysisBatch):
        calls.append(len(batch.evidence))
        if len(batch.evidence) > 1:
            raise _output_limit_error(f"limited-{len(calls)}")
        return (
            DesktopModelResult(f"leaf-{len(calls)}", "{}", 1),
            DesktopKnowledgeAnalysis(
                f"Leaf {batch.evidence[0][0]}",
                (),
                (),
                KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
            ),
        )

    recovered = analyze_batch_with_output_limit_recovery(
        _batch(4),
        analyze=analyze,
        merge=merge_split_batch_analyses,
    )

    assert calls == [4, 2, 1, 1, 2, 1, 1]
    assert recovered.split_leaf_count == 4
    assert recovered.output_limit_recovery_count == 3
    assert recovered.result.attempt_count == 7
    assert recovered.analysis.analysis_scope == KNOWLEDGE_ANALYSIS_BATCH_SCOPE


def test_recovery_splits_when_the_bounded_repair_reaches_its_output_limit() -> None:
    calls: list[int] = []

    def analyze(batch: KnowledgeAnalysisBatch):
        calls.append(len(batch.evidence))
        if len(batch.evidence) > 1:
            raise _repair_output_limit_error(f"limited-{len(calls)}")
        return (
            DesktopModelResult(f"leaf-{len(calls)}", "{}", 1),
            DesktopKnowledgeAnalysis(
                f"Leaf {batch.evidence[0][0]}",
                (),
                (),
                KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
            ),
        )

    recovered = analyze_batch_with_output_limit_recovery(
        _batch(2),
        analyze=analyze,
        merge=merge_split_batch_analyses,
    )

    assert calls == [2, 1, 1]
    assert recovered.split_leaf_count == 2
    assert recovered.output_limit_recovery_count == 1


def test_single_evidence_output_limit_is_terminal_without_recursion() -> None:
    calls = 0

    def analyze(_batch: KnowledgeAnalysisBatch):
        nonlocal calls
        calls += 1
        raise _output_limit_error("unsplittable")

    with pytest.raises(DesktopStructuredOutputInvalidError):
        analyze_batch_with_output_limit_recovery(
            _batch(1),
            analyze=analyze,
            merge=lambda _analyses: (_ for _ in ()).throw(
                AssertionError("An unsplittable result must not be merged.")
            ),
        )

    assert calls == 1
