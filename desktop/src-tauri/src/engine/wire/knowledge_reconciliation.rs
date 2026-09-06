//! Typed knowledge-reconciliation review values for the Desktop bridge.

use super::KnowledgePageKind;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReconciliationBaselineKind {
    PublishedGeneration,
    UserRevision,
    UnpublishedPage,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReconciliationDecision {
    PublishIncoming,
    KeepCurrent,
    KeepDraft,
    ApplyIncoming,
    ReplaceDraft,
    ManualMerge,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReconciliationMode {
    TwoWay,
    ThreeWay,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReconciliationConflict {
    #[serde(alias = "candidate_id")]
    pub candidate_id: String,
    #[serde(alias = "document_id")]
    pub document_id: String,
    #[serde(alias = "document_name")]
    pub document_name: String,
    pub kind: KnowledgePageKind,
    pub title: String,
    #[serde(alias = "content_markdown")]
    pub content_markdown: String,
    #[serde(alias = "baseline_kind")]
    pub baseline_kind: KnowledgeReconciliationBaselineKind,
    #[serde(alias = "baseline_title")]
    pub baseline_title: String,
    #[serde(alias = "baseline_content_markdown")]
    pub baseline_content_markdown: String,
    #[serde(alias = "observed_generation_id")]
    pub observed_generation_id: Option<u64>,
    #[serde(alias = "reconciliation_mode")]
    pub reconciliation_mode: KnowledgeReconciliationMode,
    #[serde(alias = "target_page_id")]
    pub target_page_id: Option<String>,
    #[serde(alias = "working_draft_title")]
    pub working_draft_title: Option<String>,
    #[serde(alias = "working_draft_content_markdown")]
    pub working_draft_content_markdown: Option<String>,
    #[serde(alias = "working_draft_updated_at")]
    pub working_draft_updated_at: Option<String>,
    #[serde(alias = "staged_decision")]
    pub staged_decision: Option<KnowledgeReconciliationDecision>,
    #[serde(alias = "staged_content_markdown")]
    pub staged_content_markdown: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReconciliationConflictsResult {
    pub conflicts: Vec<KnowledgeReconciliationConflict>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReconciliationCommit {
    #[serde(alias = "published_generation_id")]
    pub published_generation_id: Option<u64>,
    #[serde(alias = "published_count")]
    pub published_count: u32,
    #[serde(alias = "draft_updated_count")]
    pub draft_updated_count: u32,
    #[serde(alias = "kept_count")]
    pub kept_count: u32,
    #[serde(alias = "resolved_candidate_ids")]
    pub resolved_candidate_ids: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::{
        KnowledgeReconciliationBaselineKind, KnowledgeReconciliationConflictsResult,
        KnowledgeReconciliationDecision, KnowledgeReconciliationMode,
    };
    use serde_json::json;

    #[test]
    fn three_way_conflict_accepts_python_snake_case_fields() {
        let payload = json!({
            "conflicts": [{
                "candidate_id": "candidate-1",
                "document_id": "document-1",
                "document_name": "source.md",
                "kind": "concept",
                "title": "Retrieval",
                "content_markdown": "Incoming",
                "baseline_kind": "unpublished_page",
                "baseline_title": "",
                "baseline_content_markdown": "",
                "observed_generation_id": null,
                "reconciliation_mode": "three_way",
                "target_page_id": "page-1",
                "working_draft_title": "Retrieval",
                "working_draft_content_markdown": "Draft",
                "working_draft_updated_at": "2026-08-19T00:00:00+00:00",
                "staged_decision": "manual_merge",
                "staged_content_markdown": "Merged"
            }]
        });

        let parsed: KnowledgeReconciliationConflictsResult =
            serde_json::from_value(payload).expect("three-way payload should deserialize");

        assert!(matches!(
            parsed.conflicts[0].reconciliation_mode,
            KnowledgeReconciliationMode::ThreeWay
        ));
        assert!(matches!(
            parsed.conflicts[0].baseline_kind,
            KnowledgeReconciliationBaselineKind::UnpublishedPage
        ));
        assert!(matches!(
            parsed.conflicts[0].staged_decision,
            Some(KnowledgeReconciliationDecision::ManualMerge)
        ));
    }
}
