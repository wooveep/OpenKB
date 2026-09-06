//! Typed Knowledge Reanalysis projections for the private Desktop bridge.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DocumentAnalysisState {
    Current,
    AnalysisOutdated,
    Missing,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReanalysisStatus {
    Pending,
    Running,
    Completed,
    PartialFailure,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReanalysisJobStatus {
    Pending,
    Running,
    Completed,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReanalysisPhase {
    Pending,
    Batches,
    Merge,
    Reconciliation,
    Completed,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReanalysisMode {
    Single,
    Bulk,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DocumentAnalysisStatus {
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub state: DocumentAnalysisState,
    #[serde(alias = "schema_version")]
    pub schema_version: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    #[serde(alias = "prompt_digest")]
    pub prompt_digest: Option<String>,
    #[serde(alias = "engine_version")]
    pub engine_version: Option<String>,
    #[serde(alias = "analyzed_at")]
    pub analyzed_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReanalysisJob {
    #[serde(alias = "job_id")]
    pub job_id: String,
    #[serde(alias = "run_id")]
    pub run_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub status: KnowledgeReanalysisJobStatus,
    pub phase: KnowledgeReanalysisPhase,
    pub progress: super::ImportProgress,
    pub provider: String,
    pub model: String,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    pub reason: Option<String>,
    #[serde(alias = "batch_total")]
    pub batch_total: u32,
    #[serde(alias = "batch_completed")]
    pub batch_completed: u32,
    #[serde(alias = "current_batch")]
    pub current_batch: Option<u32>,
    #[serde(alias = "attempt_count")]
    pub attempt_count: Option<u32>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    #[serde(alias = "completed_at")]
    pub completed_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReanalysisRun {
    #[serde(alias = "run_id")]
    pub run_id: String,
    pub mode: KnowledgeReanalysisMode,
    pub status: KnowledgeReanalysisStatus,
    pub total: u32,
    pub completed: u32,
    pub failed: u32,
    pub jobs: Vec<KnowledgeReanalysisJob>,
    #[serde(alias = "created_at")]
    pub created_at: String,
    #[serde(alias = "completed_at")]
    pub completed_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReanalysisOverview {
    pub documents: Vec<DocumentAnalysisStatus>,
    pub runs: Vec<KnowledgeReanalysisRun>,
}

#[cfg(test)]
mod tests {
    use super::{DocumentAnalysisState, KnowledgeReanalysisOverview, KnowledgeReanalysisStatus};
    use serde_json::json;

    #[test]
    fn overview_accepts_python_snake_case_fields() {
        let payload = json!({
            "documents": [{
                "document_id": "doc-1", "document_name": "guide.md",
                "state": "analysis_outdated", "schema_version": "v1",
                "provider": "openai", "model": "gpt",
                "prompt_digest": "digest", "engine_version": "1.0",
                "analyzed_at": "2026-08-20T00:00:00+00:00"
            }],
            "runs": [{
                "run_id": "run-1", "mode": "bulk", "status": "partial_failure",
                "total": 1, "completed": 0, "failed": 1,
                "created_at": "2026-08-20T00:00:00+00:00", "completed_at": null,
                "jobs": [{
                    "job_id": "job-1", "run_id": "run-1", "document_id": "doc-1",
                    "document_name": "guide.md", "status": "failed", "phase": "failed",
                    "progress": 20, "provider": "openai", "model": "gpt",
                    "error_code": "model_provider_failure", "reason": "explicit provider error",
                    "batch_total": 2, "batch_completed": 1, "current_batch": 2,
                    "attempt_count": 4, "created_at": "now", "completed_at": "now"
                }]
            }]
        });

        let parsed: KnowledgeReanalysisOverview =
            serde_json::from_value(payload).expect("Python payload should deserialize");

        assert!(matches!(
            parsed.documents[0].state,
            DocumentAnalysisState::AnalysisOutdated
        ));
        assert!(matches!(
            parsed.runs[0].status,
            KnowledgeReanalysisStatus::PartialFailure
        ));
        let encoded = serde_json::to_value(parsed).expect("overview should serialize");
        assert!(encoded["runs"][0]["jobs"][0]
            .get("timeoutSeconds")
            .is_none());
        assert!(encoded["runs"][0]["jobs"][0]
            .get("remainingSeconds")
            .is_none());
    }

    #[test]
    fn overview_rejects_invalid_job_state_shapes() {
        let job = json!({
            "job_id": "job-1", "run_id": "run-1", "document_id": "doc-1",
            "document_name": "guide.md", "status": "partial_failure", "phase": "unknown",
            "progress": 101, "provider": "openai", "model": "gpt",
            "error_code": null, "reason": null, "batch_total": 0,
            "batch_completed": 0, "current_batch": null, "attempt_count": null,
            "created_at": "now", "completed_at": null
        });
        let payload = json!({
            "documents": [],
            "runs": [{
                "run_id": "run-1", "mode": "bulk", "status": "running",
                "total": 1, "completed": 0, "failed": 0, "jobs": [job],
                "created_at": "now", "completed_at": null
            }]
        });

        assert!(serde_json::from_value::<KnowledgeReanalysisOverview>(payload).is_err());
    }
}
