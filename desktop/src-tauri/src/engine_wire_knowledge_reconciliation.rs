//! Typed knowledge-reconciliation review values for the Desktop bridge.

use super::KnowledgePageKind;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeReconciliationBaselineKind {
    PublishedGeneration,
    UserRevision,
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
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeReconciliationConflictsResult {
    pub conflicts: Vec<KnowledgeReconciliationConflict>,
}
