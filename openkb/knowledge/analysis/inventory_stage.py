"""Model-owned candidate admission at the document Knowledge Analysis seam."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.importing.artifacts import DocumentIRBlock
from openkb.knowledge.analysis.plan import KnowledgeAnalysisPlan
from openkb.knowledge.analysis.service import DesktopKnowledgeAnalysis
from openkb.models.gateway import DesktopModelRequest, DesktopModelResult


@dataclass(frozen=True)
class KnowledgeInventoryStageResult:
    analysis: DesktopKnowledgeAnalysis
    result: DesktopModelResult
    operation: str
    metadata: dict[str, object]


def apply_document_inventory_stage(
    *,
    document_version_id: str,
    document_name: str,
    harvest: DesktopKnowledgeAnalysis,
    harvest_result: DesktopModelResult,
    harvest_operation: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    plan: KnowledgeAnalysisPlan,
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    on_operation_validated: Callable[[DesktopModelRequest], None],
    knowledge_language: str | None,
    database_path: Path | None = None,
) -> KnowledgeInventoryStageResult:
    """Return the validated model admissions without a second semantic classifier call."""
    del (
        document_version_id,
        document_name,
        evidence,
        plan,
        analyze,
        on_operation_validated,
        knowledge_language,
        database_path,
    )
    return KnowledgeInventoryStageResult(
        analysis=harvest,
        result=harvest_result,
        operation=harvest_operation,
        metadata={"candidate_admission_authority": "knowledge_analysis_model"},
    )
