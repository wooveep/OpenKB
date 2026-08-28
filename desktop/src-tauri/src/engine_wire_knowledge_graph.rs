//! Durable optional Knowledge Graph extraction Task Center projection.

use serde::{Deserialize, Serialize};

use super::import_observability::ModelActivity;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeGraphExtractionStatus {
    Pending,
    Running,
    Failed,
    Completed,
    CompletedEmpty,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeGraphExtractionTask {
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub status: KnowledgeGraphExtractionStatus,
    #[serde(default, alias = "node_count")]
    pub node_count: u32,
    #[serde(default, alias = "edge_count")]
    pub edge_count: u32,
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
    #[serde(alias = "model_activity")]
    pub model_activity: Option<ModelActivity>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeGraphExtractionControlResult {
    #[serde(alias = "document_id")]
    pub document_id: String,
    pub accepted: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn graph_task_accepts_python_snake_case_fields() {
        let task: KnowledgeGraphExtractionTask = serde_json::from_value(serde_json::json!({
            "document_id": "document-1",
            "document_name": "guide.md",
            "status": "running",
            "node_count": 0,
            "edge_count": 0,
            "reason": "initial",
            "provider": "custom",
            "model": "analysis-model",
            "attempt_count": 1,
            "model_attempt": 1,
            "call_id": "call-1",
            "error_code": null,
            "error_reason": null,
            "updated_at": "2026-08-23T00:00:00Z",
            "completed_at": null,
            "model_activity": null
        }))
        .expect("valid graph task");
        assert_eq!(task.status, KnowledgeGraphExtractionStatus::Running);
        assert_eq!(task.call_id.as_deref(), Some("call-1"));
    }

    #[test]
    fn graph_task_accepts_a_valid_completed_empty_result() {
        let task: KnowledgeGraphExtractionTask = serde_json::from_value(serde_json::json!({
            "document_id": "document-1", "document_name": "guide.md",
            "status": "completed_empty", "node_count": 0, "edge_count": 0,
            "reason": "initial", "provider": "custom", "model": "analysis-model",
            "attempt_count": 1, "model_attempt": 1, "call_id": "call-1",
            "error_code": null, "error_reason": null,
            "updated_at": "2026-08-23T00:00:00Z",
            "completed_at": "2026-08-23T00:00:00Z", "model_activity": null
        }))
        .expect("valid empty graph should cross the typed Bridge");
        assert_eq!(task.status, KnowledgeGraphExtractionStatus::CompletedEmpty);
        assert_eq!(task.node_count, 0);
    }
}
