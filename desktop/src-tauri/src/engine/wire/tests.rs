use super::retrieval::{FacetCoverageState, SemanticStructureState};
use super::{
    BridgeEvent, EngineEvent, EngineHealthWire, GroundedAnswer, KnowledgeAnalysisPhase,
    KnowledgeAnalysisProgress, ParserResourceState, ParserRuntimeState,
};
use serde_json::json;

#[test]
fn engine_health_accepts_python_parser_readiness_fields() {
    let health: EngineHealthWire = serde_json::from_value(json!({
        "status": "ready",
        "protocol_version": 1,
        "parser_readiness": {
            "pdf_ocr": {
                "family": "pdf_ocr",
                "formats": ["pdf"],
                "resource_state": "resources_ready",
                "runtime_state": "not_loaded",
                "diagnostic": "parser_resources_ready"
            }
        }
    }))
    .expect("Engine health should accept Python snake_case parser readiness fields");

    let readiness = health
        .parser_readiness
        .get("pdf_ocr")
        .expect("PDF OCR readiness should be present");
    assert!(matches!(
        readiness.resource_state,
        ParserResourceState::ResourcesReady
    ));
    assert!(matches!(
        readiness.runtime_state,
        ParserRuntimeState::NotLoaded
    ));

    let frontend = serde_json::to_value(readiness)
        .expect("Parser readiness should serialize for the frontend");
    assert_eq!(frontend["resourceState"], "resources_ready");
    assert_eq!(frontend["runtimeState"], "not_loaded");
    assert!(frontend.get("resource_state").is_none());
    assert!(frontend.get("runtime_state").is_none());
}

#[test]
fn knowledge_analysis_progress_accepts_python_snake_case_fields() {
    let progress: KnowledgeAnalysisProgress = serde_json::from_value(json!({
        "total": 3,
        "completed": 1,
        "active": 1,
        "failed": 0,
        "current_batch": 2,
        "phase": "batches"
    }))
    .expect("Knowledge Analysis progress should deserialize");

    assert_eq!(progress.current_batch, Some(2));
    assert!(matches!(progress.phase, KnowledgeAnalysisPhase::Batches));
}

#[test]
fn knowledge_reanalysis_event_accepts_python_snake_case_fields() {
    let event: BridgeEvent = serde_json::from_value(json!({
        "sequence": 7,
        "kind": "knowledge_reanalysis.updated",
        "data": {"run_id": "run-1", "job_id": "job-1"}
    }))
    .expect("Knowledge Reanalysis event should deserialize");

    assert!(matches!(
        event.event,
        EngineEvent::KnowledgeReanalysisUpdated(data)
            if data.run_id == "run-1" && data.job_id == "job-1"
    ));
}

#[test]
fn model_lifecycle_event_accepts_python_snake_case_fields() {
    let event: BridgeEvent = serde_json::from_value(json!({
        "sequence": 8,
        "kind": "model.call_lifecycle",
        "data": {
            "request_id": "connection-check-1",
            "call_id": "call-1",
            "attempt": 1,
            "status": "awaiting_model_result",
            "elapsed_seconds": 180.0,
            "failure_code": null,
            "reason": null,
            "retry_after_seconds": null,
            "operation": "grounded_answer",
            "model_role": "answer",
            "provider": "custom",
            "model_name": "answer-model",
            "execution_lane": "interactive",
            "attempt_id": "call-1:1",
            "long_wait_threshold_seconds": 300.0
        }
    }))
    .expect("Model lifecycle event should deserialize");

    assert!(matches!(
        event.event,
        EngineEvent::ModelCallLifecycle(data)
            if data.request_id == "connection-check-1"
                && data.call_id == "call-1"
                && data.operation == "grounded_answer"
                && data.attempt_id == "call-1:1"
                && matches!(
                    data.status,
                    super::model_lifecycle::ModelCallLifecycleStatus::AwaitingModelResult
                )
                && data.elapsed_seconds == 180.0
    ));
}

#[test]
fn model_lifecycle_accepts_validating_state() {
    let status: super::model_lifecycle::ModelCallLifecycleStatus =
        serde_json::from_value(json!("validating")).expect("validating should deserialize");

    assert!(matches!(
        status,
        super::model_lifecycle::ModelCallLifecycleStatus::Validating
    ));
}

#[test]
fn grounded_answer_accepts_python_retrieval_trace_fields() {
    let answer: GroundedAnswer = serde_json::from_value(json!({
        "answer_id": "answer-1",
        "question": "Compare Alpha and Beta",
        "answer_text": "They are related. [1]",
        "retrieval_plan": {"query": "Compare Alpha and Beta", "terms": ["alpha", "beta"], "source": "model"},
        "citations": [{
            "evidence_id": "evidence-1",
            "document_id": "document-1",
            "document_name": "alpha.md",
            "section": "Overview",
            "locator": {},
            "excerpt": "Alpha evidence.",
            "channels": ["fts"],
            "version_label": "v2.0",
            "version_side": "target"
        }],
        "degradations": [],
        "status": "completed",
        "created_at": "2026-08-20T00:00:00+00:00",
        "retrieval_trace": {
            "catalog_generation_ids": ["catalog-1"],
            "page_tree_generation_ids": ["tree-1"],
            "channels": [{
                "channel": "document_page_tree",
                "candidate_count": 2,
                "trigger_reasons": ["multi_hop"],
                "degradation_reasons": []
            }],
            "trigger_reasons": ["multi_hop"],
            "degradation_reasons": [],
            "selected_node_ids": ["node-1"],
            "canonical_evidence_ids": ["evidence-1"],
            "fusion_policy_version": "openkb.rrf-protected-baseline.v1",
            "semantic_structure_state": "known",
            "question_goal": "Explain the relationship between Alpha and Beta",
            "question_facets": [{
                "facet_id": "relationship",
                "label": "Relationship",
                "description": "How the two identities relate",
                "importance": "required"
            }],
            "question_facet_plan_digest": "facet-plan-1",
            "query_planning_prompt_contract_digest": "prompt-1",
            "query_planning_execution_profile_json": "{}",
            "query_planning_execution_profile_digest": "profile-1",
            "facet_coverage": [{
                "facet_id": "relationship",
                "state": "covered",
                "evidence_ids": ["evidence-1"]
            }],
            "coverage_gate_state": "covered",
            "version_navigation_snapshot_id": "version-snapshot-1",
            "version_catalog_revision_id": "version-catalog-1",
            "version_catalog_digest": "catalog-digest-1",
            "version_scope_mode": "comparison",
            "version_scope_status": "resolved",
            "version_scope_lineage_ids": ["lineage-1"],
            "version_scope_labels": ["v1.0", "v2.0"],
            "version_scope_document_ids": ["document-0", "document-1"],
            "version_scope_selection_reason": "explicit_comparison",
            "version_scope_degradation_reason": ""
        }
    }))
    .expect("Retrieval Trace should deserialize");

    assert_eq!(answer.retrieval_trace.page_tree_generation_ids, ["tree-1"]);
    assert_eq!(answer.retrieval_trace.channels[0].candidate_count, 2);
    assert!(matches!(
        answer.retrieval_trace.semantic_structure_state,
        SemanticStructureState::Known
    ));
    assert_eq!(
        answer.retrieval_trace.question_facets[0].facet_id,
        "relationship"
    );
    assert!(matches!(
        answer.retrieval_trace.facet_coverage[0].state,
        FacetCoverageState::Covered
    ));
    assert_eq!(answer.retrieval_trace.version_scope_mode, "comparison");
    assert_eq!(
        answer.retrieval_trace.version_scope_labels,
        ["v1.0", "v2.0"]
    );
    assert_eq!(answer.citations[0].version_label.as_deref(), Some("v2.0"));
}

#[test]
fn grounded_answer_rejects_invalid_or_missing_semantic_trace_fields() {
    let base = json!({
        "answer_id": "answer-1",
        "question": "How are Alpha and Beta related?",
        "answer_text": "Alpha supports Beta [1].",
        "retrieval_plan": {"query": "Alpha Beta", "terms": ["Alpha", "Beta"], "source": "model"},
        "citations": [],
        "source_images": [],
        "retrieval_trace": {
            "semantic_structure_state": "known",
            "question_goal": "Explain the relationship",
            "question_facets": [{
                "facet_id": "relationship", "label": "Relationship",
                "description": "How they relate", "importance": "required"
            }],
            "question_facet_plan_digest": "plan-1",
            "query_planning_prompt_contract_digest": "prompt-1",
            "query_planning_execution_profile_json": "{}",
            "query_planning_execution_profile_digest": "profile-1",
            "facet_coverage": [{
                "facet_id": "relationship", "state": "covered", "evidence_ids": ["evidence-1"]
            }],
            "coverage_gate_state": "covered"
        },
        "degradations": [],
        "status": "completed",
        "interruption_code": null,
        "interruption_reason": null,
        "created_at": "2026-09-06T00:00:00Z"
    });

    let mut invalid_importance = base.clone();
    invalid_importance["retrieval_trace"]["question_facets"][0]["importance"] = json!("primary");
    assert!(serde_json::from_value::<GroundedAnswer>(invalid_importance).is_err());

    let mut invalid_coverage = base.clone();
    invalid_coverage["retrieval_trace"]["facet_coverage"][0]["state"] = json!("not_applicable");
    assert!(serde_json::from_value::<GroundedAnswer>(invalid_coverage).is_err());

    let mut missing_state = base;
    missing_state["retrieval_trace"]
        .as_object_mut()
        .expect("Retrieval Trace should be an object")
        .remove("semantic_structure_state");
    assert!(serde_json::from_value::<GroundedAnswer>(missing_state).is_err());
}
