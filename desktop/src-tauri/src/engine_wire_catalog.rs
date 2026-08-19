//! Typed persisted corpus Catalog rebuild projection.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CatalogRebuildStatus {
    Pending,
    Running,
    Failed,
    Completed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CatalogRebuildTask {
    pub status: CatalogRebuildStatus,
    pub reason: String,
    #[serde(alias = "requested_source_revision")]
    pub requested_source_revision: u64,
    #[serde(alias = "attempt_count")]
    pub attempt_count: u32,
    #[serde(alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(alias = "error_reason")]
    pub error_reason: Option<String>,
    #[serde(alias = "updated_at")]
    pub updated_at: String,
    #[serde(alias = "completed_at")]
    pub completed_at: Option<String>,
    #[serde(alias = "current_generation_id")]
    pub current_generation_id: Option<String>,
    #[serde(alias = "stale_serving")]
    pub stale_serving: bool,
    #[serde(alias = "node_count")]
    pub node_count: u32,
    #[serde(alias = "link_count")]
    pub link_count: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_accepts_python_snake_case_fields() {
        let task: CatalogRebuildTask = serde_json::from_value(serde_json::json!({
            "status": "failed",
            "reason": "source_map_change",
            "requested_source_revision": 7,
            "attempt_count": 2,
            "error_code": "knowledge_catalog_build_failed",
            "error_reason": "fault",
            "updated_at": "2026-08-20T00:00:00Z",
            "completed_at": "2026-08-20T00:00:01Z",
            "current_generation_id": "catalog-1",
            "stale_serving": true,
            "node_count": 9,
            "link_count": 1
        }))
        .expect("valid task");
        assert_eq!(task.status, CatalogRebuildStatus::Failed);
        assert!(task.stale_serving);
    }
}
