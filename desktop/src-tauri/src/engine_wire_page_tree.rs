//! Typed deterministic PageTree rebuild Task Center projection.

use serde::{Deserialize, Serialize};

use super::import_observability::ModelActivity;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PageTreeRebuildStatus {
    Pending,
    Running,
    Failed,
    Completed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PageTreeRebuildTask {
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub status: PageTreeRebuildStatus,
    pub reason: String,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u32,
    #[serde(alias = "provider_kind")]
    pub provider_kind: String,
    #[serde(alias = "provider_version")]
    pub provider_version: String,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "completed_at")]
    pub completed_at: Option<String>,
    #[serde(alias = "current_generation_id")]
    pub current_generation_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PageTreeEnrichmentStatus {
    Pending,
    Running,
    Failed,
    Completed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PageTreeEnrichmentTask {
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub status: PageTreeEnrichmentStatus,
    pub reason: String,
    pub provider: String,
    pub model: String,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u32,
    #[serde(alias = "model_attempt")]
    pub model_attempt: u32,
    #[serde(alias = "call_id")]
    pub call_id: Option<String>,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(alias = "error_reason")]
    pub error_reason: Option<String>,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "completed_at")]
    pub completed_at: Option<String>,
    #[serde(alias = "base_generation_id")]
    pub base_generation_id: String,
    #[serde(alias = "current_enrichment_generation_id")]
    pub current_enrichment_generation_id: Option<String>,
    #[serde(alias = "model_activity")]
    pub model_activity: Option<ModelActivity>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PageTreeEnrichmentControlResult {
    #[serde(alias = "document_id")]
    pub document_id: String,
    pub accepted: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rebuild_task_accepts_python_snake_case_fields() {
        let task: PageTreeRebuildTask = serde_json::from_value(serde_json::json!({
            "document_id": "document-1",
            "document_name": "guide.md",
            "status": "running",
            "reason": "provider_update",
            "error_code": null,
            "attempt_count": 2,
            "provider_kind": "openkb_deterministic",
            "provider_version": "2",
            "updated_at": "2026-08-20T00:00:00Z",
            "completed_at": null,
            "current_generation_id": "generation-1"
        }))
        .expect("valid task");
        assert_eq!(task.status, PageTreeRebuildStatus::Running);
        assert_eq!(task.current_generation_id.as_deref(), Some("generation-1"));
    }

    #[test]
    fn enrichment_task_accepts_python_snake_case_fields() {
        let task: PageTreeEnrichmentTask = serde_json::from_value(serde_json::json!({
            "document_id": "document-1",
            "document_name": "guide.md",
            "status": "running",
            "reason": "model_update",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "attempt_count": 2,
            "model_attempt": 1,
            "call_id": "call-1",
            "error_code": null,
            "error_reason": null,
            "updated_at": "2026-08-20T00:00:00Z",
            "completed_at": null,
            "base_generation_id": "base-1",
            "current_enrichment_generation_id": null,
            "model_activity": null
        }))
        .expect("valid enrichment task");
        assert_eq!(task.status, PageTreeEnrichmentStatus::Running);
        assert_eq!(task.model_attempt, 1);
        let encoded = serde_json::to_value(task).expect("task should serialize");
        assert!(encoded.get("timeoutSeconds").is_none());
        assert!(encoded.get("remainingSeconds").is_none());
    }

    #[test]
    fn enrichment_control_accepts_python_snake_case_fields() {
        let result: PageTreeEnrichmentControlResult = serde_json::from_value(serde_json::json!({
            "document_id": "document-1",
            "accepted": true
        }))
        .expect("valid control result");
        assert_eq!(result.document_id, "document-1");
        assert!(result.accepted);
    }
}
