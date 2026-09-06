"""Bounded split recovery for output-limited Knowledge Analysis batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock
from openkb.knowledge.analysis.batch_store import KnowledgeAnalysisBatch
from openkb.knowledge.analysis.service import DesktopKnowledgeAnalysis
from openkb.models.gateway import (
    DesktopModelResult,
    DesktopProviderTokenUsage,
)
from openkb.models.structured_output import (
    DesktopStructuredOutputInvalidError,
    structured_output_reached_limit,
)

BatchAnalyzer = Callable[
    [KnowledgeAnalysisBatch],
    tuple[DesktopModelResult, DesktopKnowledgeAnalysis],
]
BatchMerger = Callable[
    [tuple[DesktopKnowledgeAnalysis, ...]],
    DesktopKnowledgeAnalysis,
]


@dataclass(frozen=True)
class OutputLimitBatchRecovery:
    """One logical batch result plus its bounded physical-call recovery shape."""

    result: DesktopModelResult
    analysis: DesktopKnowledgeAnalysis
    split_leaf_count: int
    output_limit_recovery_count: int


@dataclass(frozen=True)
class _RecoveredBranch:
    analysis: DesktopKnowledgeAnalysis
    results: tuple[DesktopModelResult, ...]
    split_leaf_count: int
    output_limit_recovery_count: int


def analyze_batch_with_output_limit_recovery(
    batch: KnowledgeAnalysisBatch,
    *,
    analyze: BatchAnalyzer,
    merge: BatchMerger,
) -> OutputLimitBatchRecovery:
    """Recursively split only explicit output-limit failures, never malformed results."""
    recovered = _recover_branch(batch, analyze=analyze, merge=merge)
    if recovered.output_limit_recovery_count == 0:
        return OutputLimitBatchRecovery(
            recovered.results[0],
            recovered.analysis,
            split_leaf_count=1,
            output_limit_recovery_count=0,
        )
    return OutputLimitBatchRecovery(
        _aggregate_result(recovered),
        recovered.analysis,
        split_leaf_count=recovered.split_leaf_count,
        output_limit_recovery_count=recovered.output_limit_recovery_count,
    )


def output_limit_split_leaf_count(checkpoint: dict[str, object]) -> int:
    """Validate and return persisted split-recovery cardinality."""
    value = checkpoint.get("output_limit_split_leaf_count")
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise DesktopImportError(
            "desktop_import_state_invalid",
            "Knowledge Analysis batch split metadata is invalid.",
        )
    return value


def _recover_branch(
    batch: KnowledgeAnalysisBatch,
    *,
    analyze: BatchAnalyzer,
    merge: BatchMerger,
) -> _RecoveredBranch:
    try:
        result, analysis = analyze(batch)
    except DesktopStructuredOutputInvalidError as error:
        if not structured_output_reached_limit(error) or len(batch.evidence) <= 1:
            raise
        left, right = _split_batch(batch)
        recovered = tuple(
            _recover_branch(child, analyze=analyze, merge=merge) for child in (left, right)
        )
        return _RecoveredBranch(
            analysis=merge(tuple(branch.analysis for branch in recovered)),
            results=(
                error.initial_result,
                *(result for branch in recovered for result in branch.results),
            ),
            split_leaf_count=sum(branch.split_leaf_count for branch in recovered),
            output_limit_recovery_count=(
                1 + sum(branch.output_limit_recovery_count for branch in recovered)
            ),
        )
    return _RecoveredBranch(
        analysis=analysis,
        results=(result,),
        split_leaf_count=1,
        output_limit_recovery_count=0,
    )


def _split_batch(
    batch: KnowledgeAnalysisBatch,
) -> tuple[KnowledgeAnalysisBatch, KnowledgeAnalysisBatch]:
    split_at = _natural_split_index(batch)
    return (
        _child_batch(batch, batch.evidence[:split_at], side=0),
        _child_batch(batch, batch.evidence[split_at:], side=1),
    )


def _natural_split_index(batch: KnowledgeAnalysisBatch) -> int:
    midpoint = len(batch.evidence) / 2
    section_boundaries = [
        index
        for index in range(1, len(batch.evidence))
        if batch.evidence[index - 1][1].heading_path != batch.evidence[index][1].heading_path
    ]
    if section_boundaries:
        return min(section_boundaries, key=lambda index: (abs(index - midpoint), index))
    return len(batch.evidence) // 2


def _child_batch(
    parent: KnowledgeAnalysisBatch,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    *,
    side: int,
) -> KnowledgeAnalysisBatch:
    return KnowledgeAnalysisBatch(
        batch_id=f"{parent.batch_id}:split:{side}",
        ordinal=parent.ordinal,
        section_paths=tuple(dict.fromkeys(block.heading_path for _evidence_id, block in evidence)),
        evidence=evidence,
        status="pending",
    )


def _aggregate_result(recovered: _RecoveredBranch) -> DesktopModelResult:
    call_identity = ":".join(result.call_id for result in recovered.results)
    return DesktopModelResult(
        call_id="split-" + hashlib.sha256(call_identity.encode("utf-8")).hexdigest()[:24],
        content=json.dumps(
            recovered.analysis.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        attempt_count=sum(result.attempt_count for result in recovered.results),
        usage=_aggregate_usage(recovered.results),
        diagnostic_context={
            "output_limit_split_leaf_count": recovered.split_leaf_count,
            "output_limit_recovery_count": recovered.output_limit_recovery_count,
        },
    )


def _aggregate_usage(
    results: tuple[DesktopModelResult, ...],
) -> DesktopProviderTokenUsage | None:
    usages = tuple(result.usage for result in results)
    if any(usage is None for usage in usages):
        return None
    complete = tuple(usage for usage in usages if usage is not None)
    return DesktopProviderTokenUsage(
        input_tokens=sum(usage.input_tokens for usage in complete),
        output_tokens=sum(usage.output_tokens for usage in complete),
        total_tokens=sum(usage.total_tokens for usage in complete),
    )
