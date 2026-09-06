"""Release evaluation through the production import, graph, page, and answer services."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from evaluation.semantic_quality.definition import EvaluationCase, LiveEvaluationProfile
from openkb.answers.grounded import DesktopGroundedAnswerService
from openkb.documents.version_scope import RetrievalRequest, VersionFilter
from openkb.importing.runner import DesktopTextImportService
from openkb.knowledge.corpus.knowledge_pipeline import CorpusKnowledgeSynthesisPipeline
from openkb.knowledge.corpus.review_store import CorpusReviewService
from openkb.knowledge.graph.tasks import DesktopKnowledgeGraphExtractionTasks
from openkb.models.gateway import DesktopModelGateway
from openkb.models.provider_adapter import named_provider_adapter_for
from openkb.retrieval.catalog_store import rebuild_pending_catalog
from openkb.workspace.paths import desktop_state_database_path
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime

PIPELINE_STAGES = (
    "document_import",
    "candidate_admission",
    "knowledge_graph",
    "knowledge_page_planning",
    "query_planning",
    "grounded_answer",
    "citation_postconditions",
    "restart_readback",
)


def execute_full_pipeline(
    case: EvaluationCase,
    *,
    repetition: int,
    profile: LiveEvaluationProfile,
    client,
    output_dir: Path,
) -> dict[str, object]:
    """Use fresh KBs and model-derived candidates; supplied page claims are never injected."""
    attempts: list[dict[str, object]] = []
    adapter = named_provider_adapter_for(profile.provider)
    stages = {stage: False for stage in PIPELINE_STAGES}
    result: dict[str, object] = {}

    def transport(request, _connect_timeout):
        pinned = replace(
            request,
            model_role="answer" if request.operation == "grounded_answer" else "analysis",
            model_name=profile.model,
            reasoning_effort="off",
            provider_adapter=adapter.identity,
            provider_adapter_version=adapter.version,
            structured_output_mode=profile.structured_output_mode
            if request.response_schema
            else None,
        )
        attempt: dict[str, object] = {"operation": pinned.operation, "output": None}
        attempts.append(attempt)
        content = client.complete(pinned)
        attempt["output"] = content
        return content

    gateway = DesktopModelGateway(
        transport, provider_name=profile.provider, model_name=profile.model
    )
    kb = output_dir / "knowledge"
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        DesktopKnowledgeBaseRuntime().create(kb)
        documents = defaultdict(list)
        for evidence in case.evidence:
            documents[evidence.document_name].append(evidence)
        imported = []
        for index, (name, evidence) in enumerate(documents.items()):
            source = output_dir / f"source-{index + 1}.md"
            source.write_text(
                f"# {name}\n\n"
                + "\n\n".join(f"## {item.section}\n\n{item.excerpt}" for item in evidence),
                encoding="utf-8",
            )
            imported.append(
                DesktopTextImportService(
                    kb, model_gateway=gateway, require_model_analysis=True
                ).import_text(source)
            )
        stages["document_import"] = all(item.job.status == "completed" for item in imported)
        with closing(sqlite3.connect(desktop_state_database_path(kb))) as db:
            admitted = db.execute(
                "SELECT COUNT(*) FROM knowledge_candidate_generation_candidates "
                "WHERE admission_state = 'admit'"
            ).fetchone()[0]
            result["admitted_candidate_count"] = admitted
            stages["candidate_admission"] = admitted > 0
        graph = DesktopKnowledgeGraphExtractionTasks(kb)
        stages["knowledge_graph"] = all(
            graph.run_document(item.document.document_id, gateway, should_stop=lambda: False)
            for item in imported
        )
        corpus = CorpusKnowledgeSynthesisPipeline(kb).run_generation(
            gateway=gateway, preferred_language=case.language, force_generation=True
        )
        stages["knowledge_page_planning"] = (
            corpus.status == "active"
            and bool(corpus.pages)
            and all(page.status == "ready" for page in corpus.pages)
        )
        result["pages"] = [
            {
                "identity_id": page.identity_id,
                "status": page.status,
                "claim_snapshot_digest": page.claim_snapshot_digest,
            }
            for page in corpus.pages
        ]
        with closing(sqlite3.connect(desktop_state_database_path(kb))) as db:
            result["rendered_pages"] = [
                {"identity_id": row[0], "title": row[1], "content_markdown": row[2]}
                for row in db.execute(
                    "SELECT identity_id, title, content_markdown FROM knowledge_generation_items "
                    "WHERE generation_id = ? ORDER BY item_key",
                    (corpus.generation_id,),
                )
            ]
        result["reviews"] = CorpusReviewService(kb).list_items()
        rebuild_pending_catalog(kb)
        selected_ids = tuple(item.document.document_id for item in imported)
        request = RetrievalRequest(
            case.question,
            version_filter=VersionFilter(
                mode="compare" if len(selected_ids) > 1 else "exact",
                document_ids=selected_ids,
            ),
        )
        answer = DesktopGroundedAnswerService(kb, model_gateway=gateway).answer(request)
        result["answer"] = answer.as_dict()
        stages["query_planning"] = (
            "query_planning" in {a["operation"] for a in attempts}
            and answer.retrieval_trace.semantic_structure_state == "known"
            and not any("query_planning" in code for code in answer.degradations)
        )
        stages["grounded_answer"] = (
            answer.status == "completed"
            and bool(answer.answer_text)
            and any(a["operation"] == "grounded_answer" for a in attempts)
        )
        with closing(sqlite3.connect(desktop_state_database_path(kb))) as db:
            stages["citation_postconditions"] = bool(answer.citations) and all(
                db.execute(
                    "SELECT 1 FROM evidence_occurrences WHERE document_id = ? AND evidence_id = ?",
                    (ref.document_id, ref.evidence_id),
                ).fetchone()
                is not None
                for ref in answer.citations
            )
        before = len(attempts)
        DesktopKnowledgeBaseRuntime().open(kb)
        with closing(sqlite3.connect(desktop_state_database_path(kb))) as db:
            persisted = db.execute(
                "SELECT answer_text FROM grounded_answers WHERE answer_id = ?", (answer.answer_id,)
            ).fetchone()
        stages["restart_readback"] = (
            persisted is not None and persisted[0] == answer.answer_text and len(attempts) == before
        )
    except Exception as error:
        # Provider errors can contain credentials; persist only the safe exception category.
        result["failure_kind"] = type(error).__name__
    return {
        "schema_version": "openkb.semantic-quality-output.v1",
        "suite_id": case.suite_id,
        "case_id": case.case_id,
        "domain": case.domain,
        "language": case.language,
        "repetition": repetition,
        "operation": "full_pipeline",
        "valid": all(stages.values()),
        **(
            {}
            if all(stages.values())
            else {"failure_kind": result.get("failure_kind", "pipeline_stage_incomplete")}
        ),
        "repaired": any(item["operation"] == "structured_output_repair" for item in attempts),
        "attempts": attempts,
        "stages": stages,
        "validated_result": result,
        "source_format": "curated_markdown_excerpts",
    }
