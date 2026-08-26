"""Natural-section batching and durable checkpoints for Knowledge Analysis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_types import (
    DesktopKnowledgeAnalysisProgress,
    DesktopModelCall,
)
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    knowledge_analysis_prompt,
    knowledge_analysis_provenance_from_checkpoint,
    knowledge_analysis_provenance_json,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_batch_planning import (
    estimate_knowledge_analysis_batch_tokens,
    knowledge_analysis_batch_prompt,
    knowledge_analysis_merge_prompt,
    plan_knowledge_analysis_batches,
)
from openkb.desktop_knowledge_analysis_batch_store import (
    DesktopKnowledgeAnalysisBatchStore,
    KnowledgeAnalysisBatch,
)
from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisMergeNodePlan,
    KnowledgeAnalysisPlan,
    build_knowledge_analysis_plan,
    knowledge_analysis_input_budget,
    prompt_snapshot_for_operation,
)
from openkb.desktop_knowledge_analysis_requests import (
    analysis_pipeline_digest as _analysis_pipeline_digest,
)
from openkb.desktop_knowledge_analysis_requests import (
    plan_prompt_digest as _prompt_contract_digest,
)
from openkb.desktop_knowledge_analysis_requests import (
    prompt_snapshot_digest as _snapshot_digest,
)
from openkb.desktop_knowledge_analysis_requests import (
    request_pinned_to_plan as _request_pinned_to_plan,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_model_capabilities import (
    DesktopModelCapabilityProfile,
    model_capability_profile,
)
from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.desktop_page_tree import PageTreeGeneration, page_tree_analysis_sections
from openkb.desktop_parallel import parallel_map_ordered
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)

__all__ = ["DesktopKnowledgeAnalysisBatchStore"]

_BATCH_CONTRACT = prompt_contract_for("knowledge_analysis_batch")
_MERGE_CONTRACT = prompt_contract_for("knowledge_analysis_merge")
KNOWLEDGE_ANALYSIS_BATCH_SYSTEM_PROMPT = _BATCH_CONTRACT.instructions
KNOWLEDGE_ANALYSIS_MERGE_SYSTEM_PROMPT = _MERGE_CONTRACT.instructions
KNOWLEDGE_ANALYSIS_BATCH_PROMPT_DIGEST = _BATCH_CONTRACT.digest
KNOWLEDGE_ANALYSIS_MERGE_PROMPT_DIGEST = _MERGE_CONTRACT.digest
KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST = hashlib.sha256(
    (KNOWLEDGE_ANALYSIS_BATCH_PROMPT_DIGEST + ":" + KNOWLEDGE_ANALYSIS_MERGE_PROMPT_DIGEST).encode(
        "utf-8"
    )
).hexdigest()


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
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    honor_control: Callable[[], None],
    on_batch_completed: Callable[[int, int], None],
    max_parallel_batches: int = 1,
    capability_profile: DesktopModelCapabilityProfile | None = None,
    execution_profile: DesktopModelExecutionProfile | None = None,
) -> KnowledgeAnalysisRun:
    """Execute a direct analysis or resume a persisted long-document batch plan."""
    natural_sections = (
        page_tree_analysis_sections(page_tree, evidence) if page_tree is not None else ()
    )
    capability = capability_profile or model_capability_profile(model)
    contract = prompt_contract_for("knowledge_analysis_batch")
    input_budget_tokens = (
        execution_profile.document_input_budget_tokens
        if execution_profile is not None
        else knowledge_analysis_input_budget(capability, contract)
    )
    planned_batches = plan_knowledge_analysis_batches(
        evidence,
        natural_sections=natural_sections or None,
        document_name=document_name,
        input_budget_tokens=input_budget_tokens,
    )
    estimated_batch_tokens = tuple(
        estimate_knowledge_analysis_batch_tokens(
            document_name,
            batch,
            batch_ordinal=ordinal,
            batch_total=len(planned_batches),
        )
        for ordinal, batch in enumerate(planned_batches)
    )
    proposed_plan = build_knowledge_analysis_plan(
        evidence=evidence,
        planned_batches=planned_batches,
        provider=provider,
        model=model,
        capability=capability,
        contract=contract,
        estimated_batch_tokens=estimated_batch_tokens,
        execution_profile=execution_profile,
    )
    plan, batches = store.load_or_create(
        job_id=job_id,
        stage_run_id=stage_run_id,
        evidence=evidence,
        planned_batches=planned_batches,
        proposed_plan=proposed_plan,
    )
    if not batches:
        prompt = knowledge_analysis_prompt(document_name, evidence)
        validated = _validated_analysis_call(
            operation="knowledge_analysis",
            document_name=document_name,
            prompt=prompt,
            analyze=analyze,
            validate=lambda content: _validated_document_analysis(content, evidence),
            plan=plan,
            batch_id="document",
        )
        result, analysis = validated
        checkpoint = _result_checkpoint(
            analysis,
            result,
            plan=plan,
            provider=provider,
            model=model,
            prompt_operation="knowledge_analysis",
            engine_version=engine_version,
        )
        return KnowledgeAnalysisRun(
            analysis,
            knowledge_analysis_provenance_json(
                provider=provider,
                model=model,
                prompt_digest=_prompt_contract_digest(plan, "knowledge_analysis"),
                engine_version=engine_version,
            ),
            checkpoint,
        )

    analyses_by_ordinal: dict[int, DesktopKnowledgeAnalysis] = {}
    checkpoints_by_ordinal: dict[int, dict[str, object]] = {}
    pending_batches: list[KnowledgeAnalysisBatch] = []
    for batch in batches:
        if batch.status == "completed":
            assert batch.checkpoint is not None
            completed_analysis = parse_batch_checkpoint(batch.checkpoint)
            _validate_batch_sources(completed_analysis, batch)
            analyses_by_ordinal[batch.ordinal] = completed_analysis
            checkpoints_by_ordinal[batch.ordinal] = batch.checkpoint
            continue
        pending_batches.append(batch)

    def execute_batch(
        batch: KnowledgeAnalysisBatch,
    ) -> tuple[DesktopKnowledgeAnalysis, dict[str, object]]:
        honor_control()
        store.start_batch(batch.batch_id)
        try:
            prompt = knowledge_analysis_batch_prompt(
                document_name,
                batch,
                batch_total=len(batches),
                input_budget_tokens=plan.input_budget_tokens,
            )
            result, analysis = _validated_analysis_call(
                operation="knowledge_analysis_batch",
                document_name=document_name,
                prompt=prompt,
                analyze=analyze,
                validate=lambda content: _validated_batch_analysis(content, batch),
                plan=plan,
                batch_id=batch.batch_id,
            )
        except DesktopModelCallError as error:
            store.fail_batch(batch.batch_id, error.failure.code)
            raise
        except DesktopImportError as error:
            store.fail_batch(batch.batch_id, error.code)
            raise
        checkpoint = _result_checkpoint(
            analysis,
            result,
            plan=plan,
            provider=provider,
            model=model,
            prompt_operation="knowledge_analysis_batch",
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
        return analysis, checkpoint

    completed_count = len(analyses_by_ordinal)

    def completed() -> None:
        nonlocal completed_count
        completed_count += 1
        on_batch_completed(completed_count, len(batches))

    pending_results = parallel_map_ordered(
        pending_batches,
        execute_batch,
        maximum=max_parallel_batches,
        on_completed=completed,
    )
    for batch, (analysis, checkpoint) in zip(pending_batches, pending_results, strict=True):
        analyses_by_ordinal[batch.ordinal] = analysis
        checkpoints_by_ordinal[batch.ordinal] = checkpoint
    analyses = [analyses_by_ordinal[batch.ordinal] for batch in batches]
    batch_checkpoints = [checkpoints_by_ordinal[batch.ordinal] for batch in batches]

    merged_checkpoint = store.merge_checkpoint(job_id)
    if merged_checkpoint is not None:
        merged = _analysis_from_document_checkpoint(merged_checkpoint)
        _validate_merge_sources(merged, tuple(analyses))
    else:
        honor_control()
        store.start_merge(job_id)
        deterministic = deterministic_merge_knowledge(tuple(analyses))
        try:
            description, result, merge_node_checkpoints = _run_hierarchical_description_merge(
                store=store,
                plan=plan,
                job_id=job_id,
                document_name=document_name,
                analyze=analyze,
                analyses=tuple(analyses),
                honor_control=honor_control,
                max_parallel_batches=max_parallel_batches,
            )
            merged = DesktopKnowledgeAnalysis(
                description,
                deterministic.concepts,
                deterministic.entities,
            )
            _validate_merge_sources(merged, tuple(analyses))
        except DesktopModelCallError as error:
            store.fail_merge(job_id, error.failure.code)
            raise
        except DesktopImportError as error:
            store.fail_merge(job_id, error.code)
            raise
        merged_checkpoint = _result_checkpoint(
            merged,
            result,
            plan=plan,
            provider=provider,
            model=model,
            prompt_operation="knowledge_analysis_merge",
            engine_version=engine_version,
            extra={
                "batch_count": len(batches),
                "analysis_prompt_digest": _analysis_pipeline_digest(plan),
                "batch_checkpoint_sha256s": [
                    hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
                    for value in batch_checkpoints
                ],
                "merge_node_checkpoint_sha256s": [
                    hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
                    for value in merge_node_checkpoints
                ],
            },
        )
        store.complete_merge(job_id, merged_checkpoint)

    return KnowledgeAnalysisRun(
        merged,
        knowledge_analysis_provenance_from_checkpoint(merged_checkpoint),
        merged_checkpoint,
    )


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


def _validated_analysis_call(
    *,
    operation: str,
    document_name: str,
    prompt: str,
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    validate: Callable[[str], DesktopKnowledgeAnalysis],
    plan: KnowledgeAnalysisPlan,
    batch_id: str,
) -> tuple[DesktopModelResult, DesktopKnowledgeAnalysis]:
    try:
        output = run_structured_output(
            operation=operation,
            document_name=document_name,
            source_material=prompt,
            invoke=lambda request: analyze(
                _request_pinned_to_plan(request, plan, batch_id=batch_id)
            ),
            validate=validate,
            contract_snapshot=prompt_snapshot_for_operation(plan, operation),
            repair_contract_snapshot=prompt_snapshot_for_operation(
                plan,
                "structured_output_repair",
            ),
        )
    except DesktopStructuredOutputInvalidError as error:
        invalid = _invalid_model_result(str(error), None)
        invalid.attempt_count = error.attempt_count
        raise invalid from error
    return output.result, output.value


def _validated_document_analysis(
    content: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> DesktopKnowledgeAnalysis:
    # Unresolved source IDs become Missing Source review work; they must not
    # discard valid sibling claims or make the parsed document unavailable.
    del evidence
    return parse_knowledge_analysis(content)


def _validated_batch_analysis(
    content: str,
    batch: KnowledgeAnalysisBatch,
) -> DesktopKnowledgeAnalysis:
    analysis = parse_knowledge_analysis(
        content,
        expected_scope=KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    )
    _validate_batch_sources(analysis, batch)
    return analysis


def _run_hierarchical_description_merge(
    *,
    store: DesktopKnowledgeAnalysisBatchStore,
    plan: KnowledgeAnalysisPlan,
    job_id: str,
    document_name: str,
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
    honor_control: Callable[[], None],
    max_parallel_batches: int,
) -> tuple[str, DesktopModelResult, tuple[dict[str, object], ...]]:
    if not plan.merge_topology:
        description = _deterministic_description(analyses)
        return (
            description,
            DesktopModelResult("deterministic", _json({"document_description": description}), 0),
            (),
        )
    descriptions = {
        f"batch:{ordinal}": analysis.document_description
        for ordinal, analysis in enumerate(analyses)
    }
    checkpoints: dict[str, dict[str, object]] = {}
    results: dict[str, DesktopModelResult] = {}

    def execute_node(
        node: KnowledgeAnalysisMergeNodePlan,
    ) -> tuple[str, DesktopModelResult, dict[str, object]]:
        child_descriptions = tuple(descriptions[child_id] for child_id in node.child_ids)
        prompt = knowledge_analysis_merge_prompt(
            document_name,
            child_descriptions,
            node_id=node.node_id,
            input_budget_tokens=plan.input_budget_tokens,
        )
        honor_control()
        store.start_merge_node(job_id, node.node_id)
        try:
            result, description = _validated_description_merge_call(
                document_name=document_name,
                prompt=prompt,
                analyze=analyze,
                plan=plan,
                batch_id=node.node_id,
            )
        except DesktopModelCallError as error:
            store.fail_merge_node(job_id, node.node_id, error.failure.code)
            raise
        except DesktopImportError as error:
            store.fail_merge_node(job_id, node.node_id, error.code)
            raise
        checkpoint = _merge_node_checkpoint(node, result, description, plan=plan)
        store.complete_merge_node(job_id, node.node_id, checkpoint)
        return description, result, checkpoint

    for level in dict.fromkeys(node.level for node in plan.merge_topology):
        pending: list[KnowledgeAnalysisMergeNodePlan] = []
        for node in (candidate for candidate in plan.merge_topology if candidate.level == level):
            checkpoint = store.merge_node_checkpoint(job_id, node.node_id)
            if checkpoint is None:
                pending.append(node)
                continue
            stored_description = checkpoint.get("document_description")
            if not isinstance(stored_description, str):
                raise _state_error("Knowledge Analysis merge-node checkpoint is invalid.")
            descriptions[node.node_id] = stored_description
            checkpoints[node.node_id] = checkpoint
            results[node.node_id] = _result_from_merge_node_checkpoint(
                checkpoint, stored_description
            )
        completed = parallel_map_ordered(
            pending,
            execute_node,
            maximum=max_parallel_batches,
            on_completed=lambda: None,
        )
        for node, (description, result, checkpoint) in zip(pending, completed, strict=True):
            descriptions[node.node_id] = description
            results[node.node_id] = result
            checkpoints[node.node_id] = checkpoint
    root = plan.merge_topology[-1].node_id
    if root not in results:
        raise _state_error("Knowledge Analysis merge topology has no completed root.")
    ordered_checkpoints = tuple(checkpoints[node.node_id] for node in plan.merge_topology)
    return descriptions[root], results[root], ordered_checkpoints


def _validated_description_merge_call(
    *,
    document_name: str,
    prompt: str,
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    plan: KnowledgeAnalysisPlan,
    batch_id: str,
) -> tuple[DesktopModelResult, str]:
    try:
        output = run_structured_output(
            operation="knowledge_analysis_merge",
            document_name=document_name,
            source_material=prompt,
            invoke=lambda request: analyze(
                _request_pinned_to_plan(request, plan, batch_id=batch_id)
            ),
            validate=_parse_merged_description,
            contract_snapshot=prompt_snapshot_for_operation(
                plan,
                "knowledge_analysis_merge",
            ),
            repair_contract_snapshot=prompt_snapshot_for_operation(
                plan,
                "structured_output_repair",
            ),
        )
    except DesktopStructuredOutputInvalidError as error:
        invalid = _invalid_model_result(str(error), None)
        invalid.attempt_count = error.attempt_count
        raise invalid from error
    return output.result, output.value


def _parse_merged_description(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise _invalid_model_result(
            "Knowledge Analysis merge returned invalid JSON.", None
        ) from error
    if not isinstance(payload, dict):
        raise _invalid_model_result("Knowledge Analysis merge must return one object.", None)
    if set(payload) == {"document_description"}:
        description = payload.get("document_description")
    elif (
        set(payload)
        == {
            "schema_version",
            "analysis_scope",
            "document_description",
            "concepts",
            "entities",
        }
        and payload.get("concepts") == []
        and payload.get("entities") == []
    ):
        # Pre-plan fixtures remain readable, but model-produced knowledge fields stay forbidden.
        description = payload.get("document_description")
    else:
        raise _invalid_model_result(
            "Knowledge Analysis merge may return only document_description.",
            None,
        )
    if not isinstance(description, str) or len(description) > 4_000:
        raise _invalid_model_result("Knowledge Analysis merge description is invalid.", None)
    return description.strip()


def deterministic_merge_knowledge(
    analyses: tuple[DesktopKnowledgeAnalysis, ...],
) -> DesktopKnowledgeAnalysis:
    """Normalize and deduplicate exact knowledge without asking a model to reproduce it."""
    accumulators: dict[
        tuple[str, str, str | None],
        dict[str, object],
    ] = {}
    for analysis in analyses:
        for candidate in (*analysis.concepts, *analysis.entities):
            normalized_title = normalize_knowledge_title(candidate.title)[1]
            subtype = _normalized_text(candidate.subtype) if candidate.subtype else None
            key = (candidate.kind, normalized_title, subtype)
            current = accumulators.setdefault(
                key,
                {
                    "candidate": candidate,
                    "aliases": [],
                    "tags": [],
                    "claims": {},
                },
            )
            _extend_unique(current["aliases"], candidate.aliases)
            _extend_unique(current["tags"], candidate.tags)
            claims = current["claims"]
            assert isinstance(claims, dict)
            for claim in candidate.claims:
                text = _normalized_text(claim.text)
                claim_state = claims.setdefault(text, [claim.text.strip(), []])
                _extend_unique(claim_state[1], claim.source_evidence_ids)
    concepts: list[KnowledgeAnalysisCandidate] = []
    entities: list[KnowledgeAnalysisCandidate] = []
    for (kind, _title, subtype), current in accumulators.items():
        original = current["candidate"]
        assert isinstance(original, KnowledgeAnalysisCandidate)
        claims = current["claims"]
        assert isinstance(claims, dict)
        merged = KnowledgeAnalysisCandidate(
            kind=kind,  # type: ignore[arg-type]
            title=original.title.strip(),
            aliases=tuple(current["aliases"]),  # type: ignore[arg-type]
            tags=tuple(current["tags"]),  # type: ignore[arg-type]
            claims=tuple(
                KnowledgeAnalysisClaim(str(value[0]), tuple(value[1])) for value in claims.values()
            ),
            subtype=subtype,
        )
        (concepts if kind == "concept" else entities).append(merged)
    return DesktopKnowledgeAnalysis(
        _deterministic_description(analyses),
        tuple(concepts),
        tuple(entities),
    )


def _deterministic_description(analyses: tuple[DesktopKnowledgeAnalysis, ...]) -> str:
    values: list[str] = []
    _extend_unique(values, (analysis.document_description for analysis in analyses))
    return " ".join(values)[:4_000]


def _extend_unique(target: object, values) -> None:
    assert isinstance(target, list)
    seen = {_normalized_text(str(value)) for value in target}
    for value in values:
        normalized = _normalized_text(str(value))
        if normalized and normalized not in seen:
            target.append(str(value).strip())
            seen.add(normalized)


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _merge_node_checkpoint(
    node: KnowledgeAnalysisMergeNodePlan,
    result: DesktopModelResult,
    description: str,
    *,
    plan: KnowledgeAnalysisPlan,
) -> dict[str, object]:
    snapshot = prompt_snapshot_for_operation(plan, "knowledge_analysis_merge")
    return {
        "node_id": node.node_id,
        "level": node.level,
        "node_ordinal": node.ordinal,
        "child_ids": list(node.child_ids),
        "document_description": description,
        "prompt_contract_snapshot": snapshot,
        "prompt_digest": _snapshot_digest(snapshot),
        "attempt_metadata": {
            "call_id": result.call_id,
            "attempt_count": result.attempt_count,
        },
        "response_sha256": hashlib.sha256(result.content.encode("utf-8")).hexdigest(),
    }


def _result_from_merge_node_checkpoint(
    checkpoint: dict[str, object],
    description: str,
) -> DesktopModelResult:
    metadata = checkpoint.get("attempt_metadata")
    if not isinstance(metadata, dict):
        raise _state_error("Knowledge Analysis merge-node metadata is invalid.")
    call_id = metadata.get("call_id")
    attempt_count = metadata.get("attempt_count")
    if not isinstance(call_id, str) or not isinstance(attempt_count, int):
        raise _state_error("Knowledge Analysis merge-node metadata is invalid.")
    return DesktopModelResult(
        call_id,
        _json({"document_description": description}),
        attempt_count,
    )


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
    expected = deterministic_merge_knowledge(batches)
    if (
        merged.concepts != expected.concepts
        or merged.entities != expected.entities
        or merged.analysis_scope != expected.analysis_scope
    ):
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
    plan: KnowledgeAnalysisPlan,
    provider: str,
    model: str,
    prompt_operation: str,
    engine_version: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    prompt_snapshot = prompt_snapshot_for_operation(plan, prompt_operation)
    checkpoint: dict[str, object] = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": analysis.analysis_scope,
        "provider": provider,
        "model": model,
        "prompt_digest": _snapshot_digest(prompt_snapshot),
        "prompt_contract_snapshot": prompt_snapshot,
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
    return DesktopKnowledgeAnalysisProgress(
        total=len(rows),
        completed=statuses.count("completed"),
        active=statuses.count("running"),
        failed=statuses.count("failed"),
        current_batch=current,
        phase=phase,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_error(message: str) -> DesktopImportError:
    return DesktopImportError("desktop_import_state_invalid", message)
