use super::import_observability::ImportProgressStage;
use super::{
    ImportJobStatus, ImportProgressStep, ImportTask, LegacyModelRecovery, ModelActivity,
    ModelUsageRecord, RecoveryOverride,
};
use serde_json::json;

#[test]
fn current_import_task_has_truthful_activity_without_timeout_budgets() {
    let task: ImportTask = serde_json::from_value(json!({
        "job": {
            "job_id": "job-1",
            "source_name": "release-notes.docx",
            "status": "running",
            "progress": 75,
            "document_id": null,
            "deduplicated": false,
            "deduplication": null
        },
        "document": null,
        "stages": [],
        "model_calls": [{
            "call_id": "call-1",
            "stage_run_id": "stage-1",
            "operation": "knowledge_analysis_batch",
            "status": "running",
            "attempt_count": 1,
            "error_code": null,
            "reason": null,
            "suggested_action": null,
            "attempts": [{
                "attempt": 1,
                "status": "running",
                "error_code": null,
                "reason": null
            }]
        }],
        "quarantine": null,
        "knowledge_analysis": {
            "total": 4,
            "completed": 1,
            "active": 1,
            "failed": 0,
            "current_batch": 2,
            "phase": "batches"
        },
        "import_progress": [{
            "stage": "knowledge_analysis_batches",
            "status": "running",
            "runtime_kind": "model",
            "source_stage_run_id": "stage-1",
            "error_code": null,
            "completed": 1,
            "total": 4
        }],
        "model_usage": [],
        "model_usage_aggregate": {
            "call_count": 1,
            "attempt_count": 1,
            "failure_count": 0,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "total_cost": null
        },
        "model_activity": {
            "operation": "knowledge_analysis_batch",
            "model_role": "analysis",
            "provider": "custom",
            "model": "analysis-model",
            "call_id": "call-1",
            "attempt": 1,
            "attempt_id": "call-1:1",
            "batch_id": "batch-2",
            "execution_lane": "background",
            "status": "awaiting_first_result",
            "failure_code": null,
            "elapsed_seconds": 181.0,
            "long_wait_advisory": false,
            "long_wait_threshold_seconds": 300.0,
            "available_actions": ["cancel"]
        },
        "legacy_model_recovery": null
    }))
    .expect("current import task should deserialize");

    assert_eq!(task.model_calls[0].attempt_count, 1);
    assert!(matches!(
        task.import_progress[0].stage,
        ImportProgressStage::KnowledgeAnalysisBatches
    ));
    assert_eq!(
        task.model_activity
            .as_ref()
            .expect("activity")
            .elapsed_seconds,
        181.0
    );
    let encoded = serde_json::to_value(task).expect("task should serialize");
    assert_eq!(encoded["importProgress"][0]["sourceStageRunId"], "stage-1");
    assert_eq!(encoded["modelActivity"]["elapsedSeconds"], 181.0);
    assert!(encoded["modelActivity"].get("elapsed_seconds").is_none());
}

#[test]
fn recovery_override_has_context_and_explicit_legacy_choice_only() {
    let recovery: RecoveryOverride = serde_json::from_value(json!({
        "model": "analysis-model",
        "contextCapacity": 32768,
        "legacyRecoveryChoice": "restart_current_plan"
    }))
    .expect("current recovery override should deserialize");

    let encoded = serde_json::to_value(recovery).expect("recovery override should serialize");
    assert_eq!(encoded["contextCapacity"], 32768);
    assert_eq!(encoded["legacyRecoveryChoice"], "restart_current_plan");
    assert!(encoded.get("initialTimeoutSeconds").is_none());
}

#[test]
fn import_job_accepts_awaiting_model_configuration_state() {
    let status: ImportJobStatus = serde_json::from_value(json!("awaiting_model_configuration"))
        .expect("awaiting-model-configuration should deserialize");

    assert!(matches!(
        status,
        ImportJobStatus::AwaitingModelConfiguration
    ));
}

#[test]
fn import_observability_rejects_unknown_closed_values() {
    let progress = json!({
        "stage": "invented_stage",
        "status": "running",
        "source_stage_run_id": "stage-1",
        "error_code": null,
        "runtime_kind": "model"
    });
    assert!(serde_json::from_value::<ImportProgressStep>(progress).is_err());

    let activity = json!({
        "operation": "knowledge_analysis_batch",
        "model_role": "analysis",
        "provider": "custom",
        "model": "analysis-model",
        "call_id": "call-1",
        "attempt": 1,
        "attempt_id": "call-1:1",
        "batch_id": null,
        "execution_lane": "background",
        "status": "timed_out",
        "failure_code": null,
        "elapsed_seconds": 1.0,
        "long_wait_advisory": false,
        "long_wait_threshold_seconds": 300.0,
        "available_actions": ["cancel"]
    });
    assert!(serde_json::from_value::<ModelActivity>(activity).is_err());

    let usage = json!({
        "call_id": "call-1",
        "attempt": 1,
        "attempt_id": "call-1:1",
        "operation": "knowledge_analysis_batch",
        "model_role": "analysis",
        "provider": "custom",
        "model": "analysis-model",
        "job_id": null,
        "stage_run_id": null,
        "batch_id": null,
        "execution_lane": "unbounded",
        "lifecycle_status": "completed",
        "failure_code": null,
        "queue_seconds": null,
        "connect_seconds": null,
        "first_output_seconds": null,
        "total_seconds": 1.0,
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "token_usage_source": "provider_reported",
        "input_cost": null,
        "output_cost": null,
        "total_cost": null,
        "provider_request_id": null,
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:01Z"
    });
    assert!(serde_json::from_value::<ModelUsageRecord>(usage).is_err());

    let recovery = json!({
        "kind": "legacy_model_deadline",
        "compatible": true,
        "compatibility_reason": "known_prompt",
        "previous_prompt_digest": null,
        "provider": null,
        "model": null,
        "completed_batches": 0,
        "total_batches": 1,
        "choices": {
            "continue_compatible": {
                "allowed": true,
                "estimated_remaining_calls": 1,
                "estimated_input_tokens": 10
            },
            "restart_current_plan": {
                "allowed": true,
                "estimated_remaining_calls": 1,
                "estimated_input_tokens": 10
            }
        },
        "recommended_choice": "invented_choice",
        "selected_choice": null,
        "starts_automatically": false
    });
    assert!(serde_json::from_value::<LegacyModelRecovery>(recovery).is_err());
}
