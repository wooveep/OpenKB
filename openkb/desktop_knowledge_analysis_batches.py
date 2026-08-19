"""Natural-section batching and durable checkpoints for Knowledge Analysis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_types import (
    DesktopKnowledgeAnalysisProgress,
    DesktopModelCall,
)
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    KNOWLEDGE_ANALYSIS_PROMPT_DIGEST,
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisScope,
    knowledge_analysis_prompt,
    knowledge_analysis_provenance_from_checkpoint,
    knowledge_analysis_provenance_json,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_batch_store import (
    DesktopKnowledgeAnalysisBatchStore,
    KnowledgeAnalysisBatch,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_model_gateway import DesktopModelCallError, DesktopModelResult
from openkb.desktop_page_tree import PageTreeGeneration, page_tree_analysis_sections

__all__ = ["DesktopKnowledgeAnalysisBatchStore"]

KNOWLEDGE_ANALYSIS_BATCH_SYSTEM_PROMPT = """Analyze one ordered natural document section batch.
Return exactly one JSON object and no prose or Markdown fence. The object must contain:
- schema_version: "openkb.knowledge-analysis.v1"
- analysis_scope: "batch"
- document_description: a concise description of this batch
- concepts: an array of Concept candidates
- entities: an array of Entity candidates
Each candidate must contain title, aliases, tags, and claims. Entity candidates may include
subtype. Each claim must contain text and source_evidence_ids. Use only supplied Evidence IDs.
Do not invent facts, graph edges, PageTree nodes, retrieval plans, or answer text.
"""
KNOWLEDGE_ANALYSIS_MERGE_SYSTEM_PROMPT = """Merge validated Knowledge Analysis batches
into one document result.
Return exactly one JSON object and no prose or Markdown fence. The object must contain:
- schema_version: "openkb.knowledge-analysis.v1"
- analysis_scope: "document"
- document_description: a concise document-level description
- concepts: the merged Concept candidates
- entities: the merged Entity candidates
Preserve supplied Evidence IDs, merge duplicate identities and claims, and do not invent facts.
Each candidate must retain title, aliases, tags, and claims; Entity candidates may include subtype.
"""
KNOWLEDGE_ANALYSIS_BATCH_PROMPT_DIGEST = hashlib.sha256(
    KNOWLEDGE_ANALYSIS_BATCH_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()
KNOWLEDGE_ANALYSIS_MERGE_PROMPT_DIGEST = hashlib.sha256(
    KNOWLEDGE_ANALYSIS_MERGE_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()
KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST = hashlib.sha256(
    (KNOWLEDGE_ANALYSIS_BATCH_PROMPT_DIGEST + ":" + KNOWLEDGE_ANALYSIS_MERGE_PROMPT_DIGEST).encode(
        "utf-8"
    )
).hexdigest()

MAX_BATCH_EVIDENCE = 12
MAX_BATCH_PROMPT_CHARACTERS = 48_000
MAX_MERGE_PROMPT_CHARACTERS = 120_000
_MAX_EVIDENCE_TEXT_CHARACTERS = 12_000
_BATCH_PROMPT_RESERVED_CHARACTERS = 4_096


@dataclass(frozen=True)
class KnowledgeAnalysisRun:
    """One complete document-level result and its safe Stage checkpoint."""

    analysis: DesktopKnowledgeAnalysis
    provenance_json: str
    checkpoint: dict[str, object]


def run_knowledge_analysis(
    *,
    store: DesktopKnowledgeAnalysisBatchStore,
    job_id: str,
    stage_run_id: str,
    document_name: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    page_tree: PageTreeGeneration | None = None,
    provider: str,
    model: str,
    engine_version: str,
    analyze: Callable[[str, str], DesktopModelResult],
    honor_control: Callable[[], None],
    on_batch_completed: Callable[[int, int], None],
) -> KnowledgeAnalysisRun:
    """Execute a direct analysis or resume a persisted long-document batch plan."""
    natural_sections = (
        page_tree_analysis_sections(page_tree, evidence) if page_tree is not None else ()
    )
    planned_batches = plan_knowledge_analysis_batches(
        evidence, natural_sections=natural_sections or None
    )
    batches = store.load_or_create(
        job_id=job_id,
        stage_run_id=stage_run_id,
        evidence=evidence,
        planned_batches=planned_batches,
    )
    if not batches:
        result = analyze("knowledge_analysis", knowledge_analysis_prompt(document_name, evidence))
        analysis = _parse_result(result)
        checkpoint = _result_checkpoint(
            analysis,
            result,
            provider=provider,
            model=model,
            prompt_digest=KNOWLEDGE_ANALYSIS_PROMPT_DIGEST,
            engine_version=engine_version,
        )
        return KnowledgeAnalysisRun(
            analysis,
            knowledge_analysis_provenance_json(
                provider=provider,
                model=model,
                prompt_digest=KNOWLEDGE_ANALYSIS_PROMPT_DIGEST,
                engine_version=engine_version,
            ),
            checkpoint,
        )

    analyses: list[DesktopKnowledgeAnalysis] = []
    batch_checkpoints: list[dict[str, object]] = []
    for batch in batches:
        if batch.status == "completed":
            assert batch.checkpoint is not None
            completed_analysis = parse_batch_checkpoint(batch.checkpoint)
            _validate_batch_sources(completed_analysis, batch)
            analyses.append(completed_analysis)
            batch_checkpoints.append(batch.checkpoint)
            continue
        honor_control()
        store.start_batch(batch.batch_id)
        try:
            result = analyze(
                "knowledge_analysis_batch",
                knowledge_analysis_batch_prompt(document_name, batch, batch_total=len(batches)),
            )
            analysis = _parse_result(result, scope=KNOWLEDGE_ANALYSIS_BATCH_SCOPE)
            _validate_batch_sources(analysis, batch, result=result)
        except DesktopModelCallError as error:
            store.fail_batch(batch.batch_id, error.failure.code)
            raise
        except DesktopImportError as error:
            store.fail_batch(batch.batch_id, error.code)
            raise
        checkpoint = _result_checkpoint(
            analysis,
            result,
            provider=provider,
            model=model,
            prompt_digest=KNOWLEDGE_ANALYSIS_BATCH_PROMPT_DIGEST,
            engine_version=engine_version,
            extra={
                "batch_id": batch.batch_id,
                "batch_ordinal": batch.ordinal,
                "batch_total": len(batches),
                "section_paths": [list(path) for path in batch.section_paths],
                "evidence_ids": [item[0] for item in batch.evidence],
            },
        )
        store.complete_batch(batch.batch_id, checkpoint)
        analyses.append(analysis)
        batch_checkpoints.append(checkpoint)
        on_batch_completed(len(analyses), len(batches))

    merged_checkpoint = store.merge_checkpoint(job_id)
    if merged_checkpoint is not None:
        merged = _analysis_from_document_checkpoint(merged_checkpoint)
        _validate_merge_sources(merged, tuple(analyses))
    else:
        honor_control()
        store.start_merge(job_id)
        try:
            result = analyze(
                "knowledge_analysis_merge",
                knowledge_analysis_merge_prompt(document_name, tuple(analyses)),
            )
            merged = _parse_result(result, aggregate=True)
            _validate_merge_sources(merged, tuple(analyses), result=result)
        except DesktopModelCallError as error:
            store.fail_merge(job_id, error.failure.code)
            raise
        except DesktopImportError as error:
            store.fail_merge(job_id, error.code)
            raise
        merged_checkpoint = _result_checkpoint(
            merged,
            result,
            provider=provider,
            model=model,
            prompt_digest=KNOWLEDGE_ANALYSIS_MERGE_PROMPT_DIGEST,
            engine_version=engine_version,
            extra={
                "batch_count": len(batches),
                "analysis_prompt_digest": KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST,
                "batch_checkpoint_sha256s": [
                    hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
                    for value in batch_checkpoints
                ],
            },
        )
        store.complete_merge(job_id, merged_checkpoint)

    return KnowledgeAnalysisRun(
        merged,
        knowledge_analysis_provenance_from_checkpoint(merged_checkpoint),
        merged_checkpoint,
    )


def plan_knowledge_analysis_batches(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    *,
    natural_sections: tuple[tuple[tuple[str, DocumentIRBlock], ...], ...] | None = None,
    max_evidence: int = MAX_BATCH_EVIDENCE,
    max_prompt_characters: int = MAX_BATCH_PROMPT_CHARACTERS,
) -> tuple[tuple[tuple[str, DocumentIRBlock], ...], ...]:
    """Pack ordered natural sections, splitting an oversized section only at IR blocks."""
    if not evidence or max_evidence < 1 or max_prompt_characters < 1:
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
            (*current, *section), max_evidence=max_evidence, maximum=max_prompt_characters
        ):
            current.extend(section)
            continue
        if current:
            batches.append(tuple(current))
            current = []
        if _batch_fits(section, max_evidence=max_evidence, maximum=max_prompt_characters):
            current.extend(section)
            continue
        for item in section:
            if current and not _batch_fits(
                (*current, item), max_evidence=max_evidence, maximum=max_prompt_characters
            ):
                batches.append(tuple(current))
                current = []
            current.append(item)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _sections_cover_evidence(
    sections: tuple[tuple[tuple[str, DocumentIRBlock], ...], ...],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> bool:
    return (
        bool(sections)
        and all(sections)
        and tuple(item for section in sections for item in section) == evidence
    )


def knowledge_analysis_batch_prompt(
    document_name: str,
    batch: KnowledgeAnalysisBatch,
    *,
    batch_total: int,
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
    if len(content) > MAX_BATCH_PROMPT_CHARACTERS:
        raise _state_error("Knowledge Analysis batch exceeds its prompt bound.")
    return content


def knowledge_analysis_merge_prompt(
    document_name: str,
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
) -> str:
    """Build one bounded merge input that either includes every batch or fails."""
    payload: dict[str, object] = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": "document_merge",
        "document_name": Path(document_name).name,
        "batch_count": len(analyses),
        "batch_results": [analysis.as_dict() for analysis in analyses],
    }
    content = _json(payload)
    if len(content) > MAX_MERGE_PROMPT_CHARACTERS:
        raise DesktopImportError(
            "model_response_invalid",
            "Validated Knowledge Analysis batches exceed the document merge bound.",
            suggested_action=(
                "Import a smaller document or use a model that returns more concise claims."
            ),
        )
    return content


def parse_batch_checkpoint(checkpoint: object) -> DesktopKnowledgeAnalysis:
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("normalized_result"), dict
    ):
        raise _state_error("Knowledge Analysis batch checkpoint is invalid.")
    return parse_knowledge_analysis(
        _json(checkpoint["normalized_result"]), expected_scope=KNOWLEDGE_ANALYSIS_BATCH_SCOPE
    )


def _analysis_from_document_checkpoint(checkpoint: object) -> DesktopKnowledgeAnalysis:
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("normalized_result"), dict
    ):
        raise _state_error("Knowledge Analysis merge checkpoint is invalid.")
    return parse_knowledge_analysis(_json(checkpoint["normalized_result"]), aggregate=True)


def _parse_result(
    result: DesktopModelResult,
    *,
    scope: KnowledgeAnalysisScope = "document",
    aggregate: bool = False,
) -> DesktopKnowledgeAnalysis:
    try:
        return parse_knowledge_analysis(result.content, expected_scope=scope, aggregate=aggregate)
    except DesktopImportError as error:
        error.attempt_count = result.attempt_count
        raise


def _validate_batch_sources(
    analysis: DesktopKnowledgeAnalysis,
    batch: KnowledgeAnalysisBatch,
    *,
    result: DesktopModelResult | None = None,
) -> None:
    allowed = {evidence_id for evidence_id, _block in batch.evidence}
    if any(
        evidence_id not in allowed
        for candidate in (*analysis.concepts, *analysis.entities)
        for claim in candidate.claims
        for evidence_id in claim.source_evidence_ids
    ):
        raise _invalid_model_result(
            "Knowledge Analysis batch referenced Evidence outside its input.", result
        )


def _validate_merge_sources(
    merged: DesktopKnowledgeAnalysis,
    batches: tuple[DesktopKnowledgeAnalysis, ...],
    *,
    result: DesktopModelResult | None = None,
) -> None:
    allowed: dict[tuple[str, str, str], set[str]] = {}
    allowed_identities: set[tuple[str, str]] = set()
    for analysis in batches:
        for candidate in (*analysis.concepts, *analysis.entities):
            title = normalize_knowledge_title(candidate.title)[1]
            allowed_identities.add((candidate.kind, title))
            for claim in candidate.claims:
                allowed.setdefault((candidate.kind, title, claim.text), set()).update(
                    claim.source_evidence_ids
                )
    actual: dict[tuple[str, str, str], set[str]] = {}
    actual_identities: set[tuple[str, str]] = set()
    for candidate in (*merged.concepts, *merged.entities):
        title = normalize_knowledge_title(candidate.title)[1]
        actual_identities.add((candidate.kind, title))
        for claim in candidate.claims:
            actual.setdefault((candidate.kind, title, claim.text), set()).update(
                claim.source_evidence_ids
            )
    if actual != allowed or actual_identities != allowed_identities:
        raise _invalid_model_result(
            "Knowledge Analysis merge must preserve every validated claim and source.", result
        )


def _invalid_model_result(message: str, result: DesktopModelResult | None) -> DesktopImportError:
    error = DesktopImportError(
        "model_response_invalid",
        message,
        suggested_action="Retry with a model that follows the Knowledge Analysis schema.",
    )
    if result is not None:
        error.attempt_count = result.attempt_count
    return error


def _result_checkpoint(
    analysis: DesktopKnowledgeAnalysis,
    result: DesktopModelResult,
    *,
    provider: str,
    model: str,
    prompt_digest: str,
    engine_version: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    checkpoint: dict[str, object] = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": analysis.analysis_scope,
        "provider": provider,
        "model": model,
        "prompt_digest": prompt_digest,
        "engine_version": engine_version,
        "attempt_metadata": {
            "call_id": result.call_id,
            "attempt_count": result.attempt_count,
        },
        "response_sha256": hashlib.sha256(result.content.encode("utf-8")).hexdigest(),
        "normalized_result": analysis.as_dict(),
    }
    if extra:
        checkpoint.update(extra)
    return checkpoint


def knowledge_analysis_progress_in(
    connection: sqlite3.Connection,
    job_id: str,
    model_calls: tuple[DesktopModelCall, ...],
) -> DesktopKnowledgeAnalysisProgress | None:
    rows = connection.execute(
        """
        SELECT batch_ordinal, status
        FROM knowledge_analysis_batches
        WHERE job_id = ?
        ORDER BY batch_ordinal
        """,
        (job_id,),
    ).fetchall()
    if not rows:
        return None
    statuses = [str(row[1]) for row in rows]
    merge_row = connection.execute(
        "SELECT status FROM knowledge_analysis_merges WHERE job_id = ?", (job_id,)
    ).fetchone()
    merge_status = str(merge_row[0]) if merge_row is not None else "pending"
    current = next(
        (int(row[0]) + 1 for row in rows if str(row[1]) in {"running", "failed", "pending"}),
        None,
    )
    phase = (
        "completed"
        if merge_status == "completed"
        else ("merge" if all(status == "completed" for status in statuses) else "batches")
    )
    latest = model_calls[-1] if model_calls else None
    waiting = latest is not None and latest.status in {"running", "retry_wait"}
    timeout_seconds = latest.timeout_seconds if waiting and latest is not None else None
    remaining_seconds = latest.remaining_seconds if waiting and latest is not None else None
    return DesktopKnowledgeAnalysisProgress(
        total=len(rows),
        completed=statuses.count("completed"),
        active=statuses.count("running"),
        failed=statuses.count("failed"),
        current_batch=current,
        phase=phase,
        current_timeout_seconds=timeout_seconds,
        remaining_seconds=remaining_seconds,
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
    max_evidence: int,
    maximum: int,
) -> bool:
    if len(evidence) > max_evidence:
        return False
    payload = {"evidence": [_evidence_payload(item) for item in evidence]}
    usable_characters = max(1, maximum - _BATCH_PROMPT_RESERVED_CHARACTERS)
    return len(_json(payload)) <= usable_characters


def _evidence_payload(item: tuple[str, DocumentIRBlock]) -> dict[str, object]:
    evidence_id, block = item
    return {
        "evidence_id": evidence_id,
        "kind": block.kind,
        "section": " / ".join(block.heading_path),
        "locator": block.locator or {"line_start": block.line_start, "line_end": block.line_end},
        "text": block.text[:_MAX_EVIDENCE_TEXT_CHARACTERS],
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_error(message: str) -> DesktopImportError:
    return DesktopImportError("desktop_import_state_invalid", message)
