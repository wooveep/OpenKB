"""Token-budgeted Knowledge Analysis batch packing and prompt construction."""

from __future__ import annotations

import json
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
)
from openkb.desktop_knowledge_analysis_batch_store import KnowledgeAnalysisBatch
from openkb.desktop_knowledge_analysis_plan import estimate_model_tokens

_MAX_EVIDENCE_TEXT_CHARACTERS = 12_000


def plan_knowledge_analysis_batches(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    *,
    natural_sections: tuple[tuple[tuple[str, DocumentIRBlock], ...], ...] | None = None,
    document_name: str = "Document",
    input_budget_tokens: int = 8_000,
    max_evidence: int | None = None,
    max_prompt_characters: int | None = None,
) -> tuple[tuple[tuple[str, DocumentIRBlock], ...], ...]:
    """Pack natural sections by estimated tokens, splitting only at DocumentIR blocks."""
    if (
        not evidence
        or input_budget_tokens < 1
        or (max_evidence is not None and max_evidence < 1)
        or (max_prompt_characters is not None and max_prompt_characters < 1)
    ):
        raise _state_error("Knowledge Analysis batch input is invalid.")
    sections = (
        natural_sections
        if natural_sections is not None and _sections_cover_evidence(natural_sections, evidence)
        else _natural_sections(evidence)
    )
    batches: list[tuple[tuple[str, DocumentIRBlock], ...]] = []
    current: list[tuple[str, DocumentIRBlock]] = []
    for section in sections:
        if current and _batch_fits(
            (*current, *section),
            document_name=document_name,
            input_budget_tokens=input_budget_tokens,
            max_evidence=max_evidence,
            max_prompt_characters=max_prompt_characters,
        ):
            current.extend(section)
            continue
        if current:
            batches.append(tuple(current))
            current = []
        if _batch_fits(
            section,
            document_name=document_name,
            input_budget_tokens=input_budget_tokens,
            max_evidence=max_evidence,
            max_prompt_characters=max_prompt_characters,
        ):
            current.extend(section)
            continue
        for item in section:
            if current and not _batch_fits(
                (*current, item),
                document_name=document_name,
                input_budget_tokens=input_budget_tokens,
                max_evidence=max_evidence,
                max_prompt_characters=max_prompt_characters,
            ):
                batches.append(tuple(current))
                current = []
            if not _batch_fits(
                (item,),
                document_name=document_name,
                input_budget_tokens=input_budget_tokens,
                max_evidence=max_evidence,
                max_prompt_characters=max_prompt_characters,
            ):
                raise _state_error(
                    "One DocumentIR block exceeds the pinned Analysis Model input budget."
                )
            current.append(item)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def knowledge_analysis_batch_prompt(
    document_name: str,
    batch: KnowledgeAnalysisBatch,
    *,
    batch_total: int,
    input_budget_tokens: int = 8_000,
) -> str:
    payload = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
        "document_name": Path(document_name).name,
        "batch_ordinal": batch.ordinal,
        "batch_total": batch_total,
        "section_paths": [list(path) for path in batch.section_paths],
        "evidence": [_evidence_payload(item) for item in batch.evidence],
    }
    content = _json(payload)
    if estimate_model_tokens(content) > input_budget_tokens:
        raise _state_error("Knowledge Analysis batch exceeds its prompt bound.")
    return content


def estimate_knowledge_analysis_batch_tokens(
    document_name: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    *,
    batch_ordinal: int,
    batch_total: int,
) -> int:
    payload = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
        "document_name": Path(document_name).name,
        "batch_ordinal": batch_ordinal,
        "batch_total": batch_total,
        "section_paths": [list(path) for path in _section_paths(evidence)],
        "evidence": [_evidence_payload(item) for item in evidence],
    }
    return estimate_model_tokens(_json(payload))


def knowledge_analysis_merge_prompt(
    document_name: str,
    analyses_or_descriptions: tuple[DesktopKnowledgeAnalysis | str, ...],
    *,
    node_id: str = "merge",
    conflicts: tuple[str, ...] = (),
    input_budget_tokens: int = 8_000,
) -> str:
    """Build a bounded description/conflict input without exact candidate payloads."""
    descriptions = tuple(
        value.document_description if isinstance(value, DesktopKnowledgeAnalysis) else value
        for value in analyses_or_descriptions
    )
    payload: dict[str, object] = {
        "schema_version": "openkb.knowledge-analysis-description-merge.v1",
        "document_name": Path(document_name).name,
        "merge_node_id": node_id,
        "descriptions": list(descriptions),
        "semantic_conflicts": list(conflicts),
    }
    content = _json(payload)
    if estimate_model_tokens(content) > input_budget_tokens:
        raise DesktopImportError(
            "model_response_invalid",
            "Knowledge Analysis descriptions exceed the pinned merge input budget.",
            suggested_action=(
                "Use shorter batch descriptions or a model with a larger context capacity."
            ),
        )
    return content


def _sections_cover_evidence(
    sections: tuple[tuple[tuple[str, DocumentIRBlock], ...], ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> bool:
    return (
        bool(sections)
        and all(sections)
        and tuple(item for section in sections for item in section) == evidence
    )


def _natural_sections(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[tuple[tuple[str, DocumentIRBlock], ...], ...]:
    sections: list[list[tuple[str, DocumentIRBlock]]] = []
    current: list[tuple[str, DocumentIRBlock]] = []
    current_path: tuple[str, ...] | None = None
    for item in evidence:
        path = item[1].heading_path
        if current and path != current_path:
            sections.append(current)
            current = []
        current_path = path
        current.append(item)
    if current:
        sections.append(current)
    return tuple(tuple(section) for section in sections)


def _batch_fits(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    *,
    document_name: str,
    input_budget_tokens: int,
    max_evidence: int | None,
    max_prompt_characters: int | None,
) -> bool:
    if max_evidence is not None and len(evidence) > max_evidence:
        return False
    content = _json(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
            "document_name": Path(document_name).name,
            "batch_ordinal": len(evidence),
            "batch_total": len(evidence),
            "section_paths": [list(path) for path in _section_paths(evidence)],
            "evidence": [_evidence_payload(item) for item in evidence],
        }
    )
    if max_prompt_characters is not None and len(content) > max_prompt_characters:
        return False
    return estimate_model_tokens(content) <= input_budget_tokens


def _evidence_payload(item: tuple[str, DocumentIRBlock]) -> dict[str, object]:
    evidence_id, block = item
    return {
        "evidence_id": evidence_id,
        "kind": block.kind,
        "section": " / ".join(block.heading_path),
        "locator": block.locator or {"line_start": block.line_start, "line_end": block.line_end},
        "text": block.text[:_MAX_EVIDENCE_TEXT_CHARACTERS],
    }


def _section_paths(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(dict.fromkeys(block.heading_path for _evidence_id, block in evidence))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_error(message: str) -> DesktopImportError:
    return DesktopImportError("desktop_import_state_invalid", message)
